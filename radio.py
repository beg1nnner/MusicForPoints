"""Очередь треков + локальное воспроизведение + рассылка состояния в оверлей."""
import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402

import blocklist  # noqa: E402
import config  # noqa: E402
from sources import SourceError, Sources  # noqa: E402

log = logging.getLogger("radio")


@dataclass
class Item:
    track_id: str
    title: str
    artists: str
    cover: str
    duration_ms: int
    user: str
    login: str = ""
    source: str = "yandex"
    raw: Any = field(repr=False, default=None)

    def public(self) -> dict:
        return {
            "id": self.track_id,
            "title": self.title,
            "artists": self.artists,
            "cover": self.cover,
            "duration_ms": self.duration_ms,
            "user": self.user,
            "login": self.login,
            "source": self.source,
        }


@dataclass
class Result:
    ok: bool
    message: str                 # для консоли и журнала
    reason: str = ""             # причина отказа (для чата)
    item: "Item | None" = None   # добавленный трек
    position: int = 0            # место в очереди (1 — следующий)
    quiet: bool = False          # не сообщать публично (зритель в чёрном списке)


Listener = Callable[[dict], Awaitable[None]]


class Radio:
    def __init__(self, sources: Sources) -> None:
        self.sources = sources
        self._dl: dict[str, asyncio.Task] = {}  # загрузки треков: номер трека -> задача
        self.queue: list[Item] = []
        self.current: Item | None = None
        self.last_played: Item | None = None
        self.paused = False
        self.volume = config.VOLUME
        self.listeners: list[Listener] = []
        self._wake = asyncio.Event()
        self._skip = asyncio.Event()
        self.status = {"twitch": "off", "yandex": "off", "reward": ""}

    # ---------- события ----------
    async def emit(self, payload: dict) -> None:
        for listener in list(self.listeners):
            try:
                await listener(payload)
            except Exception:
                log.exception("Ошибка слушателя")

    def state(self) -> dict:
        pos = max(0, pygame.mixer.music.get_pos()) if self.current else 0
        return {
            "type": "state",
            "playing": self.current.public() if self.current else None,
            "position_ms": pos,
            "paused": self.paused,
            "volume": self.volume,
            "queue": [i.public() for i in self.queue],
            "style": config.overlay_style(),
            "status": self.status,
        }

    async def broadcast(self) -> None:
        self.write_now_playing()
        await self.emit(self.state())

    def _now_text(self) -> str:
        item = self.current or (self.last_played if config.NOWPLAYING_KEEP_LAST else None)
        if item is None:
            return config.NOWPLAYING_IDLE_TEXT
        return (config.NOWPLAYING_TEMPLATE
                .replace("{artists}", item.artists)
                .replace("{title}", item.title)
                .replace("{user}", item.user))

    def write_now_playing(self) -> None:
        """Текст для источника «Текст (из файла)» в OBS."""
        try:
            config.NOW_PLAYING_FILE.write_text(self._now_text(), encoding="utf-8")
        except OSError as e:
            log.warning("Не удалось записать %s: %s", config.NOW_PLAYING_FILE, e)

    # ---------- заказ ----------
    async def request(self, user: str, query: str, login: str = "") -> tuple[bool, str]:
        """Пытается добавить трек. Возвращает (успех, сообщение)."""
        result = await self.request_ex(user, query, login)
        return result.ok, result.message

    async def request_ex(self, user: str, query: str, login: str = "") -> Result:
        """То же, что request, но с подробным результатом (причина отказа, место в очереди)."""

        async def reject(reason: str, quiet: bool = False) -> Result:
            # quiet: в OBS уведомление не показываем (чтобы не позорить зрителя на стриме)
            await self.emit({"type": "rejected", "user": user, "login": login, "reason": reason, "quiet": quiet})
            return Result(False, reason, reason=reason, quiet=quiet)

        if blocklist.is_user_blocked(user, login):
            return await reject("Зритель в чёрном списке", quiet=True)

        def limit_reason() -> str | None:
            if sum(1 for i in self.queue if i.user == user) >= config.PER_USER_LIMIT:
                return f"Лимит: не больше {config.PER_USER_LIMIT} треков в очереди"
            if len(self.queue) >= config.MAX_QUEUE:
                return "Очередь заполнена"
            return None

        reason = limit_reason()
        if reason:
            return await reject(reason)

        try:
            track = await self.sources.find(query)
        except SourceError as e:
            if e.detail:
                log.warning("%s: %s", e.reason, e.detail)
                await self.emit({"type": "error", "message": f"{e.reason}: {e.detail}"})
            return await reject(e.reason)
        except Exception:
            log.exception("Ошибка поиска трека")
            return await reject("Ошибка поиска, попробуйте позже")
        if track is None:
            return await reject("Трек не найден или недоступен")

        reason = limit_reason()  # пока искали (YouTube бывает долгим), очередь могла измениться
        if reason:
            return await reject(reason)

        duration_ms = track.duration_ms or 0
        if duration_ms > config.MAX_TRACK_SECONDS * 1000:
            return await reject(f"Трек длиннее {config.MAX_TRACK_SECONDS // 60} мин.")

        if blocklist.is_track_blocked(track.uid):
            return await reject("Этот трек в чёрном списке")

        haystack = f"{query} {track.title} {track.artists}".lower()
        if any(word in haystack for word in config.BLOCKED):
            return await reject("В запросе есть запрещённое слово")

        track_id = track.uid
        playing_ids = {i.track_id for i in self.queue}
        if self.current:
            playing_ids.add(self.current.track_id)
        if track_id in playing_ids:
            return await reject("Этот трек уже в очереди")

        item = Item(
            track_id=track_id,
            title=track.title,
            artists=track.artists,
            cover=track.cover,
            duration_ms=duration_ms,
            user=user,
            login=login,
            source=track.source,
            raw=track,
        )
        self.queue.append(item)
        self._wake.set()
        await self.emit({"type": "added", "user": user, "login": login, "title": item.title, "artists": item.artists})
        await self.broadcast()
        return Result(True, f"Добавлено: {item.artists} — {item.title}", item=item, position=len(self.queue))

    # ---------- управление ----------
    async def skip(self) -> None:
        self._skip.set()

    async def toggle_pause(self) -> None:
        if not self.current:
            return
        self.paused = not self.paused
        if self.paused:
            pygame.mixer.music.pause()
        else:
            pygame.mixer.music.unpause()
        await self.broadcast()

    async def set_volume(self, value: int, persist: bool = False) -> None:
        self.volume = max(0, min(100, value))
        pygame.mixer.music.set_volume(self.volume / 100)
        if persist:
            config.save({"VOLUME": self.volume})
        await self.emit(self.state())

    async def clear(self) -> None:
        self.queue.clear()
        await self.broadcast()

    async def remove(self, index: int) -> None:
        if 0 <= index < len(self.queue):
            self.queue.pop(index)
            await self.broadcast()

    # ---------- чёрный список ----------
    async def block_user(self, name: str) -> dict:
        key = blocklist.add_user(name)
        if not key:
            return {"done": False, "message": "Введите ник зрителя"}
        before = len(self.queue)
        self.queue[:] = [i for i in self.queue if not blocklist.is_user_blocked(i.user, i.login)]
        removed = before - len(self.queue)
        if self.current and blocklist.is_user_blocked(self.current.user, self.current.login):
            self._skip.set()
        await self.broadcast()
        await self.emit({"type": "blocklist"})
        return {"done": True, "message": f"Зритель {key} заблокирован. Убрано из очереди: {removed}"}

    async def unblock_user(self, name: str) -> dict:
        done = blocklist.remove_user(name)
        await self.emit({"type": "blocklist"})
        return {"done": done, "message": "Зритель разблокирован" if done else "Такого зрителя нет в списке"}

    async def block_track(self, track_id: str, title: str = "", artists: str = "") -> dict:
        if not track_id:
            return {"done": False, "message": "Не указан трек"}
        blocklist.add_track(track_id, title, artists)
        before = len(self.queue)
        self.queue[:] = [i for i in self.queue if i.track_id != str(track_id)]
        removed = before - len(self.queue)
        if self.current and self.current.track_id == str(track_id):
            self._skip.set()
        await self.broadcast()
        await self.emit({"type": "blocklist"})
        name = f"{artists} — {title}" if title else str(track_id)
        return {"done": True, "message": f"Трек заблокирован: {name}. Убрано из очереди: {removed}"}

    async def block_track_query(self, query: str) -> dict:
        try:
            track = await self.sources.find(query)
        except SourceError as e:
            return {"done": False, "message": e.reason}
        except Exception as e:
            return {"done": False, "message": f"Ошибка поиска: {e}"}
        if track is None:
            return {"done": False, "message": "Трек не найден"}
        return await self.block_track(track.uid, track.title, track.artists)

    async def unblock_track(self, track_id: str) -> dict:
        done = blocklist.remove_track(track_id)
        await self.emit({"type": "blocklist"})
        return {"done": done, "message": "Трек разблокирован" if done else "Такого трека нет в списке"}

    async def set_status(self, key: str, value: str, detail: str = "") -> None:
        self.status[key] = value
        self.status[key + "_error"] = detail
        await self.emit(self.state())

    # ---------- загрузка и предзагрузка ----------
    @staticmethod
    def _cache_path(track_id: str) -> Path:
        return config.CACHE_DIR / f"{track_id.replace(':', '_')}.mp3"

    def _fetch(self, item: Item) -> asyncio.Task:
        """Запускает загрузку трека, если она ещё не идёт. Следующий трек грузится, пока играет текущий."""
        task = self._dl.get(item.track_id)
        if task is None:
            task = asyncio.create_task(self.sources.download(item.raw, config.CACHE_DIR))
            task.add_done_callback(lambda t: t.cancelled() or t.exception())  # без «exception was never retrieved»
            self._dl[item.track_id] = task
        return task

    def _discard(self, track_id: str) -> None:
        task = self._dl.pop(track_id, None)
        if task is not None and not task.done():
            task.cancel()
        try:
            self._cache_path(track_id).unlink(missing_ok=True)
        except OSError:
            pass

    def _gc_downloads(self) -> None:
        """Убирает загрузки треков, которых уже нет ни в очереди, ни в эфире (убрали, очистили, заблокировали)."""
        keep = {i.track_id for i in self.queue}
        if self.current:
            keep.add(self.current.track_id)
        for track_id in [t for t in self._dl if t not in keep]:
            self._discard(track_id)

    # ---------- главный цикл ----------
    async def run(self) -> None:
        pygame.mixer.init()
        pygame.mixer.music.set_volume(self.volume / 100)
        config.CACHE_DIR.mkdir(exist_ok=True)
        for leftover in config.CACHE_DIR.glob("*"):  # остатки прошлого запуска
            try:
                leftover.unlink()
            except OSError:
                pass
        self.write_now_playing()
        log.info("Файл для текстового источника OBS: %s", config.NOW_PLAYING_FILE)

        while True:
            self._gc_downloads()
            if not self.queue:
                self.current = None
                self._wake.clear()
                await self.broadcast()
                await self._wake.wait()
                continue

            item = self.queue[0]
            try:
                path = await self._fetch(item)
            except Exception as e:
                log.exception("Не удалось скачать %s — пропускаю", item.title)
                await self.emit({"type": "error", "message": f"Не удалось скачать «{item.title}»: {e}"})
                self._discard(item.track_id)
                if item in self.queue:
                    self.queue.remove(item)
                await self.broadcast()
                continue

            if item not in self.queue:  # очередь очистили во время загрузки
                self._discard(item.track_id)
                continue
            self.queue.remove(item)
            self.current = item

            try:
                await self._play(path, item)
            except Exception as e:
                log.exception("Ошибка воспроизведения %s", item.title)
                await self.emit({"type": "error", "message": f"Не удалось проиграть «{item.title}»: {e}"})
                self.current = None
                await self.broadcast()
            finally:
                try:
                    pygame.mixer.music.unload()
                except Exception:
                    pass
                self._discard(item.track_id)

    async def _play(self, path, item: Item) -> None:
        pygame.mixer.music.load(str(path))
        pygame.mixer.music.play()
        self.last_played = item
        self.paused = False
        self._skip.clear()
        log.info("Играет: %s — %s (заказал %s)", item.artists, item.title, item.user)
        await self.broadcast()

        while True:
            await asyncio.sleep(0.25)
            if self.queue:
                self._fetch(self.queue[0])  # предзагрузка следующего трека
            self._gc_downloads()
            if self._skip.is_set():
                pygame.mixer.music.stop()
                log.info("Трек пропущен")
                return
            if not self.paused and not pygame.mixer.music.get_busy():
                return
