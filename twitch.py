"""Twitch: OAuth, создание награды за баллы, EventSub (WebSocket), статусы выкупов."""
import asyncio
import json
import logging
import time
import webbrowser
from typing import Awaitable, Callable
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

import config

log = logging.getLogger("twitch")

TOKENS_FILE = config.BASE_DIR / "tokens.json"
CHAT_SCOPE = "user:write:chat"
SCOPES = f"channel:read:redemptions channel:manage:redemptions {CHAT_SCOPE}"
CHAT_INTERVAL = 1.2  # пауза между сообщениями, чтобы не упереться в лимиты чата
REDIRECT_URI = f"http://localhost:{config.AUTH_PORT}/callback"
EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws"
HELIX = "https://api.twitch.tv/helix"

RedeemHandler = Callable[[dict], Awaitable[None]]


class TwitchClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.tokens: dict = {}
        self.user_id = ""
        self.login = ""
        self.reward_id = ""
        self.on_status = None  # callable(str) для панели
        self.on_error = None   # callable(str): сообщения об ошибках чата для панели
        self._chat_queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._chat_task: asyncio.Task | None = None
        self._last_error = ("", 0.0)
        self._tasks: set[asyncio.Task] = set()

    # ---------- токены ----------
    def _save(self) -> None:
        TOKENS_FILE.write_text(json.dumps(self.tokens), encoding="utf-8")

    async def _token_request(self, data: dict) -> None:
        payload = {
            "client_id": config.TWITCH_CLIENT_ID,
            "client_secret": config.TWITCH_CLIENT_SECRET,
            **data,
        }
        async with self.session.post("https://id.twitch.tv/oauth2/token", data=payload) as r:
            body = await r.json()
            if r.status != 200:
                raise RuntimeError(f"OAuth: {body}")
        self.tokens = {"access_token": body["access_token"], "refresh_token": body["refresh_token"]}
        self._save()

    async def _refresh(self) -> None:
        await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": self.tokens["refresh_token"]}
        )

    async def authorize(self) -> None:
        refreshed = False
        if TOKENS_FILE.exists():
            self.tokens = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
            try:
                await self._refresh()
                log.info("Twitch: токен обновлён")
                refreshed = True
            except Exception as e:
                log.warning("Не удалось обновить токен (%s), нужна новая авторизация", e)
        if not refreshed:
            await self._browser_login()
            return
        if config.CHAT_ENABLED and not await self.has_scope(CHAT_SCOPE):
            log.info("Для сообщений в чате нужен дополнительный доступ. Войдите в Twitch ещё раз.")
            await self._browser_login()

    async def has_scope(self, scope: str) -> bool:
        headers = {"Authorization": f"OAuth {self.tokens['access_token']}"}
        try:
            async with self.session.get("https://id.twitch.tv/oauth2/validate", headers=headers) as r:
                if r.status != 200:
                    return False
                data = await r.json()
        except aiohttp.ClientError:
            return True  # нет сети: не заставляем входить заново
        return scope in data.get("scopes", [])

    async def relogin(self) -> None:
        """Повторный вход (чтобы выдать доступ к чату или сменить аккаунт)."""
        try:
            await self._browser_login()
            log.info("Twitch: новый вход выполнен")
        except Exception as e:
            log.error("Twitch: вход не удался: %s", e)
            self._report(f"Вход в Twitch не удался: {e}")

    async def _browser_login(self) -> None:
        loop = asyncio.get_running_loop()
        got_code: asyncio.Future = loop.create_future()

        async def callback(request: web.Request) -> web.Response:
            code = request.query.get("code")
            if code and not got_code.done():
                got_code.set_result(code)
            return web.Response(
                text="<h2>Готово! Эту вкладку можно закрыть.</h2>",
                content_type="text/html",
                charset="utf-8",
            )

        app = web.Application()
        app.router.add_get("/callback", callback)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "localhost", config.AUTH_PORT).start()

        url = "https://id.twitch.tv/oauth2/authorize?" + urlencode(
            {
                "client_id": config.TWITCH_CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": SCOPES,
            }
        )
        print("\nОткрываю браузер для входа в Twitch. Если не открылся — перейдите по ссылке:\n" + url + "\n")
        webbrowser.open(url)
        try:
            code = await asyncio.wait_for(got_code, timeout=300)
        finally:
            await runner.cleanup()

        await self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
        )
        log.info("Twitch: авторизация выполнена")

    # ---------- Helix API ----------
    async def helix(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(2):
            headers = {
                "Client-Id": config.TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {self.tokens['access_token']}",
            }
            async with self.session.request(method, HELIX + path, headers=headers, **kwargs) as r:
                if r.status == 401 and attempt == 0:
                    await self._refresh()
                    continue
                text = await r.text()
                if r.status >= 400:
                    raise RuntimeError(f"Helix {method} {path} -> {r.status}: {text}")
                return json.loads(text) if text else {}
        return {}

    async def ensure_reward(self) -> None:
        me = (await self.helix("GET", "/users"))["data"][0]
        self.user_id, self.login = me["id"], me["login"]

        params = {"broadcaster_id": self.user_id}
        rewards = await self.helix(
            "GET", "/channel_points/custom_rewards", params={**params, "only_manageable_rewards": "true"}
        )
        for reward in rewards["data"]:
            if reward["title"] == config.REWARD_TITLE:
                self.reward_id = reward["id"]
                log.info("Награда «%s» найдена", config.REWARD_TITLE)
                return

        try:
            created = await self.helix(
                "POST",
                "/channel_points/custom_rewards",
                params=params,
                json={
                    "title": config.REWARD_TITLE,
                    "cost": config.REWARD_COST,
                    "prompt": config.REWARD_PROMPT,
                    "is_user_input_required": True,
                    "should_redemptions_skip_request_queue": False,
                },
            )
        except RuntimeError as e:
            if "DUPLICATE_REWARD" in str(e):
                raise RuntimeError(
                    f"Награда «{config.REWARD_TITLE}» уже есть на канале, но создана не этим приложением. "
                    "Удалите её в настройках наград Twitch или смените название на вкладке «Заказы»."
                ) from None
            raise
        self.reward_id = created["data"][0]["id"]
        log.info("Награда «%s» создана (%d баллов)", config.REWARD_TITLE, config.REWARD_COST)

    async def update_reward(self) -> None:
        """Применяет название/цену/подсказку из настроек к награде на Twitch."""
        if not self.reward_id:
            return
        await self.helix(
            "PATCH",
            "/channel_points/custom_rewards",
            params={"broadcaster_id": self.user_id, "id": self.reward_id},
            json={"title": config.REWARD_TITLE, "cost": config.REWARD_COST, "prompt": config.REWARD_PROMPT},
        )

    def _notify(self, status: str) -> None:
        if self.on_status:
            self.on_status(status)

    # ---------- сообщения в чат ----------
    def _report(self, text: str) -> None:
        """Сообщает панели об ошибке, не чаще раза в минуту для одного и того же текста."""
        last_text, last_time = self._last_error
        if text == last_text and time.monotonic() - last_time < 60:
            return
        self._last_error = (text, time.monotonic())
        if self.on_error:
            self.on_error(text)

    async def send_now(self, text: str) -> tuple[bool, str]:
        """Отправляет сообщение в чат канала от имени стримера. Возвращает (успех, причина ошибки)."""
        try:
            res = await self.helix(
                "POST",
                "/chat/messages",
                json={"broadcaster_id": self.user_id, "sender_id": self.user_id, "message": text[:450]},
            )
        except RuntimeError as e:
            detail = str(e)
            if "scope" in detail.lower() or "401" in detail or "403" in detail:
                detail = "нет доступа к чату. Нажмите «Войти в Twitch заново» на вкладке «Подключения»"
            return False, detail
        info = (res.get("data") or [{}])[0]
        if info.get("is_sent"):
            return True, ""
        drop = info.get("drop_reason") or {}
        return False, drop.get("message") or "Twitch отклонил сообщение"

    def say(self, text: str) -> None:
        """Ставит сообщение в очередь на отправку (не блокирует обработку заказов)."""
        try:
            self._chat_queue.put_nowait(text)
        except asyncio.QueueFull:
            log.warning("Очередь сообщений в чат переполнена, сообщение пропущено")

    async def _chat_worker(self) -> None:
        while True:
            text = await self._chat_queue.get()
            ok, detail = await self.send_now(text)
            if not ok:
                log.warning("Чат: %s", detail)
                self._report(f"Сообщение в чат не отправлено: {detail}")
            await asyncio.sleep(CHAT_INTERVAL)

    async def set_status(self, redemption_id: str, status: str) -> None:
        """status: FULFILLED (принято) или CANCELED (баллы возвращаются зрителю)."""
        await self.helix(
            "PATCH",
            "/channel_points/custom_rewards/redemptions",
            params={
                "id": redemption_id,
                "broadcaster_id": self.user_id,
                "reward_id": self.reward_id,
            },
            json={"status": status},
        )

    # ---------- EventSub ----------
    async def _subscribe(self, session_id: str) -> None:
        await self.helix(
            "POST",
            "/eventsub/subscriptions",
            json={
                "type": "channel.channel_points_custom_reward_redemption.add",
                "version": "1",
                "condition": {"broadcaster_user_id": self.user_id, "reward_id": self.reward_id},
                "transport": {"method": "websocket", "session_id": session_id},
            },
        )
        log.info("EventSub: подписка на выкупы активна. Ждём заказы…")
        self._notify("connected")

    async def listen(self, on_redeem: RedeemHandler) -> None:
        if self._chat_task is None:
            self._chat_task = asyncio.create_task(self._chat_worker())
        url, fresh = EVENTSUB_URL, True
        while True:
            try:
                url, fresh = await self._run_ws(url, on_redeem, fresh)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("EventSub: %s. Переподключение через 5 с…", e)
                self._notify("reconnecting")
                url, fresh = EVENTSUB_URL, True
                await asyncio.sleep(5)

    async def _run_ws(self, url: str, on_redeem: RedeemHandler, fresh: bool):
        timeout = 40.0
        async with self.session.ws_connect(url) as ws:
            while True:
                msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise ConnectionError("соединение закрыто")
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue

                data = json.loads(msg.data)
                kind = data["metadata"]["message_type"]

                if kind == "session_welcome":
                    session = data["payload"]["session"]
                    timeout = (session.get("keepalive_timeout_seconds") or 10) + 10
                    if fresh:
                        await self._subscribe(session["id"])
                elif kind == "session_reconnect":
                    return data["payload"]["session"]["reconnect_url"], False
                elif kind == "notification":
                    event = data["payload"]["event"]
                    task = asyncio.create_task(on_redeem(event))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                elif kind == "revocation":
                    raise RuntimeError("подписка отозвана Twitch")
