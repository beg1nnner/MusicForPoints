"""Источники музыки: Яндекс Музыка, YouTube и ссылки Spotify.

Spotify не отдаёт звук (он защищён DRM), поэтому по ссылке на трек Spotify мы читаем название и исполнителя
со страницы трека и ищем тот же трек в Яндекс Музыке или на YouTube. Звук всегда приходит из этих двух источников.
"""
import asyncio
import html
import importlib.metadata
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

import config
import netssl
from ym import TRACK_URL_RE as YANDEX_URL_RE
from ym import YandexMusic

log = logging.getLogger("sources")

YT_URL_RE = re.compile(
    r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?(?:\S*?&)?v=|shorts/|live/|embed/|v/))([\w-]{11})"
)
SPOTIFY_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?track/|spotify:track:)([A-Za-z0-9]{22})")
ANY_URL_RE = re.compile(r"https?://|www\.", re.I)

# Насколько длительность найденного трека может отличаться от длительности трека в Spotify
YANDEX_TOLERANCE_MS = 10_000
YOUTUBE_TOLERANCE_MS = 20_000  # у клипов бывают вступления и титры

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


class SourceError(Exception):
    """Ошибка источника. reason показывается зрителю, detail только стримеру (журнал и панель)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass
class Track:
    source: str          # "yandex" | "youtube"
    id: str
    title: str
    artists: str
    cover: str
    duration_ms: int
    raw: Any = field(repr=False, default=None)

    @property
    def uid(self) -> str:
        """Уникальный номер трека. У Яндекса остаётся прежним, чтобы старый чёрный список работал."""
        return self.id if self.source == "yandex" else f"yt:{self.id}"


# ---------------------------------------------------------------- YouTube
_NOISE_RE = re.compile(
    r"\s*[\(\[][^\)\]]*(official|lyric|audio|video|visuali[sz]er|hd|4k|клип|текст|премьера)[^\)\]]*[\)\]]",
    re.I,
)


class YouTube:
    """Поиск и загрузка через yt-dlp. Всё блокирующее выполняется в потоках."""

    @staticmethod
    def ffmpeg() -> str | None:
        found = shutil.which("ffmpeg")
        if found:
            return found
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None

    @staticmethod
    def js_runtime_path() -> tuple[str, str] | None:
        """yt-dlp для YouTube нужен внешний JavaScript-рантайм (рекомендуется Deno).

        Ищем сами и передаём yt-dlp полный путь: окно, запущенное двойным щелчком, может не видеть
        свежий PATH сразу после установки Deno.
        """
        exe = ".exe" if os.name == "nt" else ""
        deno_spots = [
            config.BASE_DIR / f"deno{exe}",                         # рядом с программой
            Path.home() / ".deno" / "bin" / f"deno{exe}",           # официальный установщик
        ]
        found = shutil.which("deno")
        if found:
            return "deno", found
        for spot in deno_spots:
            if spot.is_file():
                return "deno", str(spot)
        for name in ("node", "bun"):
            found = shutil.which(name)
            if found:
                return name, found
        return None

    @classmethod
    def js_runtime(cls) -> str | None:
        found = cls.js_runtime_path()
        return found[0] if found else None

    @staticmethod
    def _import():
        try:
            import yt_dlp
        except ImportError:
            raise SourceError(
                "YouTube сейчас недоступен",
                detail='Не установлен yt-dlp: pip install -U "yt-dlp[default]"',
            ) from None
        return yt_dlp

    def _opts(self, **extra) -> dict:
        opts = {
            "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "socket_timeout": 20, "retries": 3, "fragment_retries": 3, **extra,
        }
        found = self.js_runtime_path()
        if found:
            name, path = found
            opts["js_runtimes"] = {name: {"path": path}}
        return opts

    @staticmethod
    def _names(info: dict) -> tuple[str, str]:
        artist, track = info.get("artist") or info.get("creator"), info.get("track")
        if artist and track:
            return str(artist), str(track)
        channel = re.sub(r"\s*-\s*Topic$", "", info.get("channel") or info.get("uploader") or "").strip()
        title = _NOISE_RE.sub("", info.get("title") or "").strip() or "Без названия"
        m = re.match(r"^(.{1,60}?)\s+[-–—]\s+(.+)$", title)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return channel or "YouTube", title

    def _track(self, info: dict) -> Track:
        vid = info["id"]
        artists, title = self._names(info)
        return Track(
            "youtube", vid, title, artists, f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            int(float(info.get("duration") or 0) * 1000),
            raw=f"https://www.youtube.com/watch?v={vid}",
        )

    def _extract(self, target: str, flat: bool = False) -> dict:
        yt_dlp = self._import()
        opts = self._opts(skip_download=True)
        if flat:
            opts["extract_flat"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(target, download=False) or {}
        except yt_dlp.utils.DownloadError as e:
            raise SourceError("Не удалось получить видео с YouTube", detail=str(e)[:400]) from None

    @staticmethod
    def _is_live(info: dict) -> bool:
        return bool(info.get("is_live")) or info.get("live_status") in ("is_live", "is_upcoming", "post_live")

    async def video(self, video_id: str) -> Track:
        info = await asyncio.to_thread(self._extract, f"https://www.youtube.com/watch?v={video_id}")
        if not info.get("id"):
            raise SourceError("Видео не найдено")
        if self._is_live(info):
            raise SourceError("Прямые трансляции заказывать нельзя")
        return self._track(info)

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        info = await asyncio.to_thread(self._extract, f"ytsearch{limit}:{query}", True)
        out = []
        for entry in info.get("entries") or []:
            if not entry or not entry.get("id") or not entry.get("duration") or self._is_live(entry):
                continue
            out.append(self._track(entry))
        return out

    def download_sync(self, video_id: str, dest_dir: Path) -> Path:
        yt_dlp = self._import()
        ffmpeg = self.ffmpeg()
        if not ffmpeg:
            raise RuntimeError("Не найден ffmpeg: pip install imageio-ffmpeg (или установите ffmpeg в систему)")
        opts = self._opts(
            format="bestaudio/best",
            outtmpl=str(dest_dir / "yt_%(id)s.%(ext)s"),
            ffmpeg_location=ffmpeg,
            postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        path = dest_dir / f"yt_{video_id}.mp3"
        if not path.exists():
            raise RuntimeError("yt-dlp не создал файл с звуком")
        return path


# ---------------------------------------------------------------- Spotify
@dataclass
class SpotifyMeta:
    artists: str
    title: str
    duration_ms: int
    cover: str


def _meta_tags(page: str) -> dict:
    tags: dict = {}
    for m in re.finditer(r"<meta\s+([^>]+?)/?>", page, re.I):
        attrs = dict(re.findall(r'([\w:-]+)\s*=\s*"([^"]*)"', m.group(1)))
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs:
            tags.setdefault(key, html.unescape(attrs["content"]))
    return tags


_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def _from_embed(page: str) -> SpotifyMeta | None:
    """Данные трека со страницы для встраивания (open.spotify.com/embed/track/ID).

    Обычная страница трека из России приходит пустой оболочкой веб-плеера, а эта отдаёт данные сразу.
    """
    m = _NEXT_DATA_RE.search(page)
    if not m:
        return None
    try:
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (ValueError, KeyError, TypeError):
        return None
    title = str(entity.get("name") or entity.get("title") or "").strip()
    artists = ", ".join(str(a.get("name", "")).strip() for a in entity.get("artists") or [] if a.get("name"))
    if not title or not artists:
        return None
    images = ((entity.get("visualIdentity") or {}).get("image") or [])
    images = [i for i in images if i.get("url")]
    fit = [i for i in images if (i.get("maxWidth") or 0) <= 640]
    cover = max(fit or images, key=lambda i: i.get("maxWidth") or 0)["url"] if images else ""
    try:
        duration_ms = int(entity.get("duration") or 0)
    except (TypeError, ValueError):
        duration_ms = 0
    return SpotifyMeta(artists, title, duration_ms, cover)


def _from_track_page(page: str) -> SpotifyMeta | None:
    """Данные из тегов обычной страницы трека (работает не во всех странах)."""
    tags = _meta_tags(page)
    title = tags.get("og:title", "").strip()
    artists = tags.get("music:musician_description", "").strip()
    if not artists:
        first = tags.get("og:description", "").split(" · ")[0].strip()
        artists = first if " · " in tags.get("og:description", "") else ""
    if not title or not artists:
        return None
    try:
        duration_ms = int(float(tags.get("music:duration", "0"))) * 1000
    except ValueError:
        duration_ms = 0
    return SpotifyMeta(artists, title, duration_ms, tags.get("og:image", ""))


class Spotify:
    """Читает название, исполнителя и длительность трека со страниц Spotify. Ключи Spotify API не нужны."""

    async def _get(self, session, url: str, statuses: list) -> str | None:
        async with session.get(url) as r:
            statuses.append(r.status)
            if r.status == 404:
                raise SourceError("Трек Spotify не найден")
            if r.status != 200:
                return None
            return await r.text()

    async def meta(self, track_id: str) -> SpotifyMeta:
        headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.8"}
        tried, statuses = [], []
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=15),
                                             connector=aiohttp.TCPConnector(ssl=netssl.context())) as s:
                for url, parse in ((f"https://open.spotify.com/embed/track/{track_id}", _from_embed),
                                   (f"https://open.spotify.com/track/{track_id}", _from_track_page)):
                    page = await self._get(s, url, statuses)
                    tried.append(url.split("/")[3])
                    meta = parse(page) if page else None
                    if meta:
                        return meta
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise SourceError("Не удалось открыть ссылку Spotify", detail=str(e)) from None
        if 200 not in statuses:
            raise SourceError("Не удалось открыть ссылку Spotify", detail="HTTP " + ", ".join(map(str, statuses)))
        raise SourceError(
            "Не удалось прочитать трек Spotify",
            detail=f"На страницах Spotify ({', '.join(tried)}) нет названия или исполнителя (возможно, Spotify их изменил)",
        )


# ---------------------------------------------------------------- все источники вместе
class Sources:
    def __init__(self, ym: YandexMusic) -> None:
        self.ym = ym
        self.yt = YouTube()
        self.spotify = Spotify()

    # ---- вспомогательное ----
    def _yandex_ready(self) -> bool:
        return config.SRC_YANDEX and self.ym.client is not None

    def _order(self) -> list[str]:
        """Где искать по тексту и по ссылкам Spotify, в порядке приоритета."""
        order = ["yandex", "youtube"] if config.SEARCH_FIRST == "yandex" else ["youtube", "yandex"]
        return [s for s in order if (s == "youtube" and config.SRC_YOUTUBE) or (s == "yandex" and self._yandex_ready())]

    def _yandex_track(self, raw) -> Track | None:
        if raw is None:
            return None
        return Track("yandex", str(raw.id), raw.title or "Без названия", self.ym.artists(raw),
                     self.ym.cover_url(raw), raw.duration_ms or 0, raw)

    @staticmethod
    def _enabled_names() -> str:
        pairs = (("Яндекс Музыка", config.SRC_YANDEX), ("YouTube", config.SRC_YOUTUBE), ("Spotify", config.SRC_SPOTIFY))
        return ", ".join(name for name, on in pairs if on) or "нет"

    # ---- поиск ----
    async def find(self, query: str) -> Track | None:
        """Находит трек по ссылке или тексту. None, если ничего не найдено. SourceError с причиной для зрителя."""
        query = query.strip()

        m = SPOTIFY_RE.search(query)
        if m:
            if not config.SRC_SPOTIFY:
                raise SourceError("Ссылки Spotify не принимаются")
            return await self._from_spotify(m.group(1))

        m = YT_URL_RE.search(query)
        if m:
            if not config.SRC_YOUTUBE:
                raise SourceError("Заказы с YouTube отключены")
            return await self.yt.video(m.group(1))

        if YANDEX_URL_RE.search(query):
            if not config.SRC_YANDEX:
                raise SourceError("Заказы из Яндекс Музыки отключены")
            if self.ym.client is None:
                raise SourceError("Яндекс Музыка сейчас недоступна")
            return self._yandex_track(await self.ym.find(query))

        if ANY_URL_RE.search(query):
            raise SourceError(f"Ссылка не поддерживается. Нужна ссылка на отдельный трек ({self._enabled_names()})")

        return await self._search_text(query)

    async def _search_text(self, query: str) -> Track | None:
        order = self._order()
        if not order:
            raise SourceError("Нет доступных источников музыки")
        last_error: SourceError | None = None
        for source in order:
            try:
                if source == "yandex":
                    found = await self.ym.search_many(query, 1)
                    track = self._yandex_track(found[0] if found else None)
                else:
                    cands = await self.yt.search(query)
                    limit_ms = config.MAX_TRACK_SECONDS * 1000
                    fit = [t for t in cands if t.duration_ms <= limit_ms]
                    track = (fit or cands or [None])[0]
            except SourceError as e:
                last_error = e
                continue
            except Exception as e:
                log.warning("Поиск в %s не удался: %s", source, e)
                last_error = SourceError("Ошибка поиска", detail=str(e))
                continue
            if track:
                return track
        if last_error:
            raise last_error
        return None

    async def _from_spotify(self, spotify_id: str) -> Track:
        meta = await self.spotify.meta(spotify_id)
        query = f"{meta.artists} {meta.title}"
        order = self._order()
        if not order:
            raise SourceError("Нет доступных источников для воспроизведения")
        last_error: SourceError | None = None
        for source in order:
            try:
                if source == "yandex":
                    cands = [self._yandex_track(t) for t in await self.ym.search_many(query, 5)]
                    tolerance = YANDEX_TOLERANCE_MS
                else:
                    cands = await self.yt.search(query, 5)
                    tolerance = YOUTUBE_TOLERANCE_MS
            except SourceError as e:
                last_error = e
                continue
            except Exception as e:
                log.warning("Поиск в %s не удался: %s", source, e)
                last_error = SourceError("Ошибка поиска", detail=str(e))
                continue
            for t in cands:
                if meta.duration_ms and abs(t.duration_ms - meta.duration_ms) > tolerance:
                    continue
                if t.source == "youtube":  # названия на YouTube шумные, берём чистые из Spotify
                    t.title, t.artists = meta.title, meta.artists
                    if meta.cover:
                        t.cover = meta.cover
                return t
        if last_error and last_error.detail:
            raise last_error
        raise SourceError("Не нашли этот трек из Spotify ни в Яндекс Музыке, ни на YouTube")

    # ---- загрузка ----
    async def download(self, track: Track, cache_dir: Path) -> Path:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if track.source == "youtube":
            return await asyncio.to_thread(self.yt.download_sync, track.id, cache_dir)
        return await self.ym.download(track.raw, cache_dir / f"{track.id}.mp3")

    def youtube_problems(self) -> list[str]:
        """Быстрая проверка без сети: чего не хватает для YouTube."""
        try:
            importlib.metadata.version("yt-dlp")
        except importlib.metadata.PackageNotFoundError:
            return ['не установлен yt-dlp: pip install -U "yt-dlp[default]"']
        problems = []
        if not self.yt.ffmpeg():
            problems.append("не найден ffmpeg: pip install imageio-ffmpeg")
        if not self.yt.js_runtime():
            problems.append("не найден JS-рантайм (Deno или Node.js): без него загрузка с YouTube может не работать")
        return problems

    # ---- проверка YouTube для панели ----
    async def diagnose_youtube(self) -> tuple[bool, str]:
        parts: list[str] = []
        ok = True

        def add(good: bool, text: str) -> None:
            nonlocal ok
            ok = ok and good
            parts.append(("✓ " if good else "✗ ") + text)

        try:
            add(True, "yt-dlp " + importlib.metadata.version("yt-dlp"))
        except importlib.metadata.PackageNotFoundError:
            return False, '✗ yt-dlp не установлен: pip install -U "yt-dlp[default]"'
        try:
            importlib.metadata.version("yt-dlp-ejs")
            add(True, "yt-dlp-ejs")
        except importlib.metadata.PackageNotFoundError:
            add(False, 'нет yt-dlp-ejs: pip install -U "yt-dlp[default]"')
        ffmpeg = self.yt.ffmpeg()
        add(bool(ffmpeg), "ffmpeg" if ffmpeg else "нет ffmpeg: pip install imageio-ffmpeg")
        found = self.yt.js_runtime_path()
        add(bool(found), f"JS-рантайм {found[0]} ({found[1]})" if found else "нет JS-рантайма: установите Deno или Node.js")
        try:
            track = await self.yt.video("dQw4w9WgXcQ")
            add(True, f"тестовое видео открылось ({track.duration_ms // 1000} с)")
        except SourceError as e:
            add(False, "тестовое видео не открылось: " + (e.detail or e.reason)[:200])
        return ok, " · ".join(parts)
