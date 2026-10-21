"""Заказ музыки из Яндекс Музыки за баллы канала Twitch + оверлей для OBS."""
import argparse
import asyncio
import logging
import webbrowser

import aiohttp

import config
import netssl
from overlay_server import OverlayServer
from messages import render
from radio import Radio, Result
from sources import Sources
from twitch import TwitchClient
from ym import YandexMusic

log = logging.getLogger("main")


async def console_loop(radio: Radio) -> None:
    print("\nТестовый режим без Twitch. Вводите «Исполнитель - Название» или ссылку, Enter — заказать.\n")
    while True:
        try:
            line = await asyncio.to_thread(input, "> ")
        except EOFError:
            return
        if line.strip():
            _, message = await radio.request("console", line.strip())
            print(message)


async def main(no_twitch: bool) -> None:
    ym = YandexMusic()
    sources = Sources(ym)
    radio = Radio(sources)
    radio.status["twitch"] = "off" if no_twitch else "waiting"
    radio.status["yandex"] = "waiting"

    saved = asyncio.Event()  # сигнал «настройки сохранены»
    ctx: dict = {"twitch": None, "ym_stage_done": False}
    bg: set[asyncio.Task] = set()

    async def after_save(changed: set) -> list:
        """Применяет сохранённые настройки без перезапуска."""
        saved.set()
        warnings = []
        if ctx["ym_stage_done"] and {"YANDEX_MUSIC_TOKEN", "SRC_YANDEX"} & changed:
            if not config.SRC_YANDEX:
                await radio.set_status("yandex", "off")
            elif config.YM_TOKEN:
                try:
                    await ym.init()
                    await radio.set_status("yandex", "ok")
                except Exception as e:
                    warnings.append(f"Яндекс Музыка: не удалось войти ({e})")
        twitch = ctx["twitch"]
        if twitch and twitch.reward_id and changed & config.REWARD_KEYS:
            try:
                await twitch.update_reward()
                radio.status["reward"] = f"{config.REWARD_TITLE} · {config.REWARD_COST}"
            except Exception as e:
                warnings.append(f"Twitch не принял изменения награды: {e}")
        return warnings

    overlay = OverlayServer(radio)
    overlay.on_save.append(after_save)
    await overlay.start()
    url = f"http://127.0.0.1:{config.OVERLAY_PORT}/control"

    async def wait_for_settings(reason: str) -> None:
        log.warning("%s", reason)
        log.warning("Откройте %s и устраните проблему — программа продолжит сама.", url)
        await saved.wait()
        saved.clear()

    if config.missing_settings(not no_twitch):
        webbrowser.open(url + "#connect")

    # --- Яндекс Музыка (её можно отключить и пользоваться только YouTube) ---
    while True:
        missing = config.missing_settings(not no_twitch)
        if missing:
            await wait_for_settings(f"Не заполнено: {', '.join(missing)}")
            continue
        if not config.SRC_YANDEX:
            await radio.set_status("yandex", "off")
            break
        try:
            await ym.init()
            await radio.set_status("yandex", "ok")
            break
        except Exception as e:
            await radio.set_status("yandex", "error", f"Яндекс Музыка: {e}")
            await wait_for_settings(f"Не удалось войти в Яндекс Музыку: {e}")

    ctx["ym_stage_done"] = True
    if config.SRC_YOUTUBE:
        for problem in sources.youtube_problems():
            log.warning("YouTube: %s", problem)

    player = asyncio.create_task(radio.run())

    if no_twitch:
        await asyncio.gather(player, console_loop(radio))
        return

    # --- Twitch ---
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=netssl.context())) as session:
        twitch = TwitchClient(session)

        def on_twitch_status(status: str) -> None:
            task = asyncio.create_task(radio.set_status("twitch", status))
            bg.add(task)
            task.add_done_callback(bg.discard)

        twitch.on_status = on_twitch_status
        overlay.twitch = twitch

        def on_chat_error(text: str) -> None:
            task = asyncio.create_task(radio.emit({"type": "error", "message": text}))
            bg.add(task)
            task.add_done_callback(bg.discard)

        twitch.on_error = on_chat_error

        while True:
            missing = config.missing_settings(True)
            if missing:
                await wait_for_settings(f"Не заполнено: {', '.join(missing)}")
                continue
            try:
                await radio.set_status("twitch", "connecting")
                await twitch.authorize()
                await twitch.ensure_reward()
                break
            except Exception as e:
                log.error("Twitch: %s", e)
                await radio.set_status("twitch", "error", f"Twitch: {e}")
                await wait_for_settings("Не удалось подключиться к Twitch.")

        ctx["twitch"] = twitch
        radio.status["reward"] = f"{config.REWARD_TITLE} · {config.REWARD_COST}"

        def announce(user: str, res: Result) -> None:
            """Пишет в чат, принят заказ или почему нет."""
            if not config.CHAT_ENABLED:
                return
            if res.ok and res.item and config.CHAT_ON_ACCEPTED:
                twitch.say(render(config.CHAT_MSG_ACCEPTED, user=user, artists=res.item.artists,
                                  title=res.item.title, position=res.position))
            elif not res.ok and not res.quiet and config.CHAT_ON_REJECTED:
                twitch.say(render(config.CHAT_MSG_REJECTED, user=user, reason=res.reason.rstrip(".")))

        async def on_redeem(event: dict) -> None:
            user = event["user_name"]
            query = (event.get("user_input") or "").strip()
            if not query:
                res = Result(False, "Пустой запрос", reason="не указан трек: напишите «Исполнитель - Название» или вставьте ссылку")
            else:
                try:
                    res = await radio.request_ex(user, query, event.get("user_login", ""))
                except Exception as e:
                    log.exception("Ошибка обработки заказа")
                    res = Result(False, f"Ошибка: {e}", reason="внутренняя ошибка, попробуйте позже")
            log.info("Заказ от %s «%s»: %s", user, query, res.message)
            try:
                await twitch.set_status(event["id"], "FULFILLED" if res.ok else "CANCELED")
            except Exception:
                log.exception("Не удалось обновить статус выкупа")
            announce(user, res)

        await asyncio.gather(player, twitch.listen(on_redeem))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-twitch", action="store_true", help="тест без Twitch: заказы вводятся в консоли")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(main(args.no_twitch))
    except KeyboardInterrupt:
        print("\nОстановлено.")
