"""HTTP/WebSocket-сервер: оверлей для OBS (/), панель управления и настроек (/control)."""
import asyncio
import json
import logging
from typing import Awaitable, Callable
from urllib.parse import urlparse

from aiohttp import web

import blocklist
import config
from radio import Radio

log = logging.getLogger("overlay")

SaveHook = Callable[[set], Awaitable[list]]


@web.middleware
async def guard(request: web.Request, handler):
    """Защита панели от чужих сайтов: проверяем Origin, Host и тип тела запроса."""
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:
        return web.json_response({"ok": False, "error": "forbidden origin"}, status=403)
    hostname = request.host.rsplit(":", 1)[0]
    if config.OVERLAY_HOST == "127.0.0.1" and hostname not in ("127.0.0.1", "localhost"):
        return web.json_response({"ok": False, "error": "forbidden host"}, status=403)
    if request.method == "POST" and request.can_read_body and request.content_type != "application/json":
        return web.json_response({"ok": False, "error": "json required"}, status=415)
    return await handler(request)


class OverlayServer:
    def __init__(self, radio: Radio) -> None:
        self.radio = radio
        self.clients: set[web.WebSocketResponse] = set()
        self.on_save: list[SaveHook] = []
        self.twitch = None  # TwitchClient, назначается из main
        self._bg: set = set()
        radio.listeners.append(self.push)
        self.runner: web.AppRunner | None = None

    async def push(self, payload: dict) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_str(text)
            except Exception:
                self.clients.discard(ws)

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.clients.add(ws)
        await ws.send_str(json.dumps(self.radio.state(), ensure_ascii=False))
        try:
            async for _ in ws:
                pass
        finally:
            self.clients.discard(ws)
        return ws

    # ---------- настройки ----------
    async def _get_settings(self, _: web.Request) -> web.Response:
        values, secrets = config.public_values()
        return web.json_response({"fields": config.FIELDS, "values": values, "secrets": secrets})

    async def _post_settings(self, request: web.Request) -> web.Response:
        body = await request.json()
        changed, errors = config.save(body.get("values", {}))
        if errors:
            return web.json_response({"ok": False, "errors": errors}, status=400)

        changed_set = set(changed)
        warnings: list[str] = []
        if "VOLUME" in changed_set:
            await self.radio.set_volume(config.VOLUME)
        if any(k.startswith("NOWPLAYING_") for k in changed_set):
            self.radio.write_now_playing()
        for hook in self.on_save:
            try:
                warnings += await hook(changed_set) or []
            except Exception as e:
                log.exception("Ошибка применения настроек")
                warnings.append(f"Не удалось применить: {e}")

        twitch_live = self.radio.status.get("twitch") == "connected"
        restart = bool(changed_set & config.RESTART_KEYS) or (bool(changed_set & config.TWITCH_KEYS) and twitch_live)
        await self.radio.emit(self.radio.state())
        return web.json_response(
            {"ok": True, "changed": sorted(changed_set), "restart_needed": restart, "warnings": warnings}
        )

    # ---------- сборка приложения ----------
    async def start(self) -> None:
        r = self.radio

        def page(name: str):
            async def handler(_: web.Request) -> web.StreamResponse:
                resp = web.FileResponse(config.OVERLAY_DIR / name)
                resp.headers["Cache-Control"] = "no-store"
                return resp
            return handler

        def action(fn):
            async def handler(request: web.Request) -> web.Response:
                body = await request.json() if request.can_read_body else {}
                result = await fn(body)
                return web.json_response({"ok": True, **(result or {})})
            return handler

        async def volume(body):
            await r.set_volume(int(body.get("value", r.volume)), persist=bool(body.get("save")))

        async def test_request(body):
            query = str(body.get("query", "")).strip()
            if not query:
                return {"message": "Пустой запрос"}
            try:
                _, message = await r.request("Тест", query)
            except Exception as e:
                log.exception("Ошибка тестового заказа")
                message = f"Ошибка: {e}"
            return {"message": message}

        async def test_toast(_):
            await r.emit({"type": "added", "user": "Зритель", "title": "Название трека", "artists": "Исполнитель"})

        async def twitch_logout(_):
            (config.BASE_DIR / "tokens.json").unlink(missing_ok=True)

        async def get_blocklist(_: web.Request) -> web.Response:
            return web.json_response(blocklist.snapshot())

        async def block_user(body):
            return await r.block_user(str(body.get("name", "")))

        async def unblock_user(body):
            return await r.unblock_user(str(body.get("name", "")))

        async def block_track(body):
            if str(body.get("query", "")).strip():
                return await r.block_track_query(str(body["query"]).strip())
            return await r.block_track(str(body.get("id", "")), str(body.get("title", "")), str(body.get("artists", "")))

        async def unblock_track(body):
            return await r.unblock_track(str(body.get("id", "")))

        async def twitch_relogin(_):
            tw = self.twitch
            if tw is None:
                return {"done": False, "message": "Twitch ещё не запущен"}
            task = asyncio.create_task(tw.relogin())
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
            return {"done": True, "message": "Откройте браузер и подтвердите доступ к Twitch"}

        async def chat_test(_):
            tw = self.twitch
            if tw is None or not tw.user_id:
                return {"done": False, "message": "Twitch ещё не подключён"}
            ok, detail = await tw.send_now("🎵 Проверка: бот заказа музыки подключён к чату")
            return {"done": ok, "message": "Сообщение отправлено в чат" if ok else f"Не отправлено: {detail}"}

        async def youtube_check(_):
            ok, text = await r.sources.diagnose_youtube()
            return {"done": ok, "message": "YouTube: " + text}

        app = web.Application(middlewares=[guard])
        app.router.add_get("/", page("index.html"))
        app.router.add_get("/control", page("control.html"))
        app.router.add_get("/settings", page("control.html"))
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/api/settings", self._get_settings)
        app.router.add_post("/api/settings", self._post_settings)
        app.router.add_post("/api/skip", action(lambda _: r.skip()))
        app.router.add_post("/api/pause", action(lambda _: r.toggle_pause()))
        app.router.add_post("/api/clear", action(lambda _: r.clear()))
        app.router.add_post("/api/remove", action(lambda b: r.remove(int(b.get("index", -1)))))
        app.router.add_post("/api/volume", action(volume))
        app.router.add_get("/api/blocklist", get_blocklist)
        app.router.add_post("/api/block-user", action(block_user))
        app.router.add_post("/api/unblock-user", action(unblock_user))
        app.router.add_post("/api/block-track", action(block_track))
        app.router.add_post("/api/unblock-track", action(unblock_track))
        app.router.add_post("/api/request", action(test_request))
        app.router.add_post("/api/test-toast", action(test_toast))
        app.router.add_post("/api/twitch-logout", action(twitch_logout))
        app.router.add_post("/api/twitch-relogin", action(twitch_relogin))
        app.router.add_post("/api/chat-test", action(chat_test))
        app.router.add_post("/api/youtube-check", action(youtube_check))

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, config.OVERLAY_HOST, config.OVERLAY_PORT).start()
        log.info("Оверлей для OBS:    http://127.0.0.1:%d/", config.OVERLAY_PORT)
        log.info("Панель и настройки: http://127.0.0.1:%d/control", config.OVERLAY_PORT)
