"""Настройки: значения по умолчанию -> .env (если есть) -> settings.json (из веб-панели)."""
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

FROZEN = getattr(sys, "frozen", False)  # запущено как .exe (PyInstaller)
# Данные пользователя (настройки, токены, ключ) лежат рядом с .exe, а не во временной папке
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
# Ресурсы, вшитые в .exe (оверлей), PyInstaller распаковывает в _MEIPASS
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
load_dotenv(BASE_DIR / ".env", encoding="utf-8")

SETTINGS_FILE = BASE_DIR / "settings.json"
AUTH_PORT = 4343  # redirect URI: http://localhost:4343/callback
CACHE_DIR = BASE_DIR / "cache"
NOW_PLAYING_FILE = BASE_DIR / "now_playing.txt"
OVERLAY_DIR = RESOURCE_DIR / "overlay"


def _f(key, group, label, type_, default, help="", **extra):
    return {"key": key, "group": group, "label": label, "type": type_,
            "default": default, "help": help, **extra}


FONTS = [
    {"value": "segoe", "label": "Segoe UI"},
    {"value": "arial", "label": "Arial"},
    {"value": "verdana", "label": "Verdana"},
    {"value": "trebuchet", "label": "Trebuchet MS"},
    {"value": "georgia", "label": "Georgia"},
    {"value": "consolas", "label": "Consolas"},
]
POSITIONS = [
    {"value": "bottom-left", "label": "Снизу слева"},
    {"value": "bottom-right", "label": "Снизу справа"},
    {"value": "top-left", "label": "Сверху слева"},
    {"value": "top-right", "label": "Сверху справа"},
]

FIELDS = [
    # ---- Заказы ----
    _f("REWARD_TITLE", "order", "Название награды", "str", "Заказать музыку",
       "Так награда называется у зрителей. До 45 символов.",
       section="Награда за баллы", required=True, max_len=45),
    _f("REWARD_COST", "order", "Цена в баллах", "int", 500, section="Награда за баллы", min=1, max=10_000_000),
    _f("REWARD_PROMPT", "order", "Подсказка для зрителя", "textarea",
       "Напиши «Исполнитель - Название» или вставь ссылку на трек: Яндекс Музыка, YouTube, Spotify",
       "Показывается в окне выкупа. До 200 символов.", section="Награда за баллы", max_len=200),
    _f("SRC_YANDEX", "order", "Яндекс Музыка", "bool", True,
       "Ссылки music.yandex.ru и поиск по названию. Нужен токен на вкладке «Подключения».",
       section="Откуда можно заказывать"),
    _f("SRC_YOUTUBE", "order", "YouTube", "bool", True,
       "Ссылки youtube.com, youtu.be, music.youtube.com и поиск по названию. Нужны yt-dlp, ffmpeg и Deno или Node.js, "
       "проверить можно на вкладке «Подключения».", section="Откуда можно заказывать"),
    _f("SRC_SPOTIFY", "order", "Ссылки Spotify", "bool", True,
       "Spotify не отдаёт звук, поэтому по ссылке ищется тот же трек в Яндекс Музыке или на YouTube.",
       section="Откуда можно заказывать"),
    _f("SEARCH_FIRST", "order", "Где искать сначала (по названию и для ссылок Spotify)", "select", "yandex",
       "Если там ничего не нашлось, поиск продолжается в другом включённом источнике.",
       section="Откуда можно заказывать",
       options=[{"value": "yandex", "label": "Яндекс Музыка"}, {"value": "youtube", "label": "YouTube"}]),
    _f("PER_USER_LIMIT", "order", "Сколько треков зритель может держать в очереди", "int", 2,
       section="Ограничения", min=1, max=50),
    _f("MAX_QUEUE", "order", "Максимальная длина очереди", "int", 15, section="Ограничения", min=1, max=200),
    _f("MAX_TRACK_MINUTES", "order", "Максимальная длина трека, минут", "int", 8,
       section="Ограничения", min=1, max=120),
    _f("BLOCKED_WORDS", "order", "Запрещённые слова", "textarea", "",
       "Через запятую или с новой строки. Если слово есть в запросе, названии или имени исполнителя — "
       "заказ отклоняется, баллы возвращаются.", section="Ограничения", max_len=3000),
    _f("CHAT_ENABLED", "order", "Писать в чат Twitch", "bool", True,
       "Сообщения отправляются от имени вашего аккаунта. Нужен доступ к чату: если его ещё нет, "
       "на вкладке «Подключения» нажмите «Войти в Twitch заново».", section="Сообщения в чате"),
    _f("CHAT_ON_ACCEPTED", "order", "Сообщать о принятом заказе", "bool", True, section="Сообщения в чате"),
    _f("CHAT_MSG_ACCEPTED", "order", "Текст при принятом заказе", "str",
       "✅ @{user}, заказ принят: {artists} — {title}. Место в очереди: {position}",
       "Подстановки: {user}, {artists}, {title}, {position}.", section="Сообщения в чате", max_len=300),
    _f("CHAT_ON_REJECTED", "order", "Сообщать, почему заказ не принят", "bool", True,
       "Для зрителей из чёрного списка сообщение не отправляется.", section="Сообщения в чате"),
    _f("CHAT_MSG_REJECTED", "order", "Текст при отказе", "str",
       "❌ @{user}, заказ не принят: {reason}. Баллы возвращены.",
       "Подстановки: {user}, {reason}.", section="Сообщения в чате", max_len=300),
    # ---- Плеер (меняется на вкладке «Плеер») ----
    _f("VOLUME", "player", "Громкость", "range", 40, min=0, max=100),
    # ---- Подключения ----
    _f("TWITCH_CLIENT_ID", "connect", "Client ID", "str", "", section="Twitch", max_len=100),
    _f("TWITCH_CLIENT_SECRET", "connect", "Client Secret", "secret", "", section="Twitch", max_len=100),
    _f("YANDEX_MUSIC_TOKEN", "connect", "Токен", "secret", "",
       "Применяется сразу после сохранения.", section="Яндекс Музыка", max_len=200),
    _f("OVERLAY_HOST", "connect", "Кто может открыть эту панель и оверлей", "select", "127.0.0.1",
       "«Вся локальная сеть» открывает панель с вашими настройками всем устройствам в сети. "
       "Выбирайте только если OBS запущен на другом компьютере.",
       section="Адрес сервера", options=[
           {"value": "127.0.0.1", "label": "Только этот компьютер"},
           {"value": "0.0.0.0", "label": "Вся локальная сеть"}]),
    _f("OVERLAY_PORT", "connect", "Порт", "int", 8080, section="Адрес сервера", min=1024, max=65535),
    # ---- Оверлей ----
    _f("OV_ACCENT", "overlay", "Акцент (прогресс, ник зрителя)", "color", "#8fa8ff", section="Цвета"),
    _f("OV_BG", "overlay", "Фон карточки", "color", "#15131c", section="Цвета"),
    _f("OV_TEXT", "overlay", "Текст", "color", "#f3f1f8", section="Цвета"),
    _f("OV_BG_OPACITY", "overlay", "Непрозрачность фона, %", "range", 100, section="Цвета", min=0, max=100, step=5),
    _f("OV_POSITION", "overlay", "Положение", "select", "bottom-left", section="Размер и положение",
       options=POSITIONS),
    _f("OV_WIDTH", "overlay", "Ширина карточки, px", "range", 440, section="Размер и положение",
       min=280, max=700, step=10),
    _f("OV_SCALE", "overlay", "Масштаб, %", "range", 100, section="Размер и положение",
       min=50, max=200, step=5),
    _f("OV_RADIUS", "overlay", "Скругление углов, px", "range", 14, section="Размер и положение", min=0, max=32),
    _f("OV_FONT", "overlay", "Шрифт", "select", "segoe", section="Размер и положение", options=FONTS),
    _f("NOWPLAYING_TEMPLATE", "overlay", "Текст в файле", "str", "{artists} — {title}",
       f"Файл для текстового источника OBS: {NOW_PLAYING_FILE}. "
       "Подстановки: {artists} — исполнитель, {title} — название, {user} — кто заказал.",
       section="Текстовый файл для OBS", max_len=200),
    _f("NOWPLAYING_KEEP_LAST", "overlay", "Оставлять последний трек в файле, когда музыка закончилась", "bool", True,
       section="Текстовый файл для OBS"),
    _f("NOWPLAYING_IDLE_TEXT", "overlay", "Текст, когда музыка ещё не играла (или не оставляем трек)", "str", "",
       "Пусто — файл будет пустым.", section="Текстовый файл для OBS", max_len=200),
    _f("OV_QUEUE", "overlay", "Сколько треков очереди показывать", "range", 3,
       "0 — скрыть очередь.", section="Что показывать", min=0, max=10),
    _f("OV_SHOW_COVER", "overlay", "Показывать обложку", "bool", True, section="Что показывать"),
    _f("OV_SHOW_USER", "overlay", "Показывать, кто заказал", "bool", True, section="Что показывать"),
    _f("OV_SHOW_TIME", "overlay", "Показывать время трека", "bool", True, section="Что показывать"),
    _f("OV_SHOW_TOAST", "overlay", "Показывать уведомления о заказах и отказах", "bool", True,
       section="Что показывать"),
]
BY_KEY = {f["key"]: f for f in FIELDS}

REWARD_KEYS = {"REWARD_TITLE", "REWARD_COST", "REWARD_PROMPT"}
TWITCH_KEYS = {"TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"}
RESTART_KEYS = {"OVERLAY_HOST", "OVERLAY_PORT"}

VALUES: dict = {}
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def coerce(field: dict, raw):
    t = field["type"]
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "да")
    if t in ("int", "range"):
        try:
            value = int(float(str(raw).strip()))
        except ValueError:
            raise ValueError("Нужно целое число")
        if field.get("min") is not None and value < field["min"]:
            raise ValueError(f"Не меньше {field['min']}")
        if field.get("max") is not None and value > field["max"]:
            raise ValueError(f"Не больше {field['max']}")
        return value
    text = str(raw if raw is not None else "").strip()
    if t == "color":
        if not _COLOR_RE.match(text):
            raise ValueError("Цвет в формате #rrggbb")
        return text.lower()
    if t == "select":
        if text not in [o["value"] for o in field["options"]]:
            raise ValueError("Недопустимое значение")
        return text
    if field.get("required") and not text:
        raise ValueError("Не может быть пустым")
    if field.get("max_len") and len(text) > field["max_len"]:
        raise ValueError(f"Не длиннее {field['max_len']} символов")
    return text


def load() -> None:
    for f in FIELDS:
        value = f["default"]
        env = os.getenv(f["key"])
        if env not in (None, ""):
            try:
                value = coerce(f, env)
            except ValueError:
                pass
        VALUES[f["key"]] = value
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for key, raw in data.items():
            field = BY_KEY.get(key)
            if field:
                try:
                    VALUES[key] = coerce(field, raw)
                except ValueError:
                    pass
    _apply()


def _apply() -> None:
    g = globals()
    g.update(VALUES)  # config.REWARD_TITLE, config.MAX_QUEUE и т.д.
    g["YM_TOKEN"] = VALUES["YANDEX_MUSIC_TOKEN"]
    g["MAX_TRACK_SECONDS"] = VALUES["MAX_TRACK_MINUTES"] * 60
    g["BLOCKED"] = [w.strip().lower() for w in re.split(r"[,\n]", VALUES["BLOCKED_WORDS"]) if w.strip()]


def save(updates: dict) -> tuple[list[str], dict]:
    """Проверяет и сохраняет. Возвращает (изменённые ключи, ошибки по ключам)."""
    clean, errors = {}, {}
    for key, raw in updates.items():
        field = BY_KEY.get(key)
        if not field:
            continue
        if field["type"] == "secret" and not str(raw or "").strip():
            continue  # пустой секрет = «оставить как есть»
        try:
            clean[key] = coerce(field, raw)
        except ValueError as e:
            errors[key] = str(e)
    if errors:
        return [], errors
    changed = [k for k, v in clean.items() if VALUES.get(k) != v]
    if changed:
        VALUES.update(clean)
        SETTINGS_FILE.write_text(json.dumps(VALUES, ensure_ascii=False, indent=2), encoding="utf-8")
        _apply()
    return changed, {}


def public_values() -> tuple[dict, dict]:
    """Значения для панели: секреты не отдаются, только признак «задано»."""
    values = {k: ("" if BY_KEY[k]["type"] == "secret" else v) for k, v in VALUES.items()}
    secrets = {k: bool(VALUES[k]) for k in VALUES if BY_KEY[k]["type"] == "secret"}
    return values, secrets


def overlay_style() -> dict:
    return {k[3:].lower(): v for k, v in VALUES.items() if k.startswith("OV_")}


def missing_settings(need_twitch: bool) -> list[str]:
    names = {"YANDEX_MUSIC_TOKEN": "токен Яндекс Музыки",
             "TWITCH_CLIENT_ID": "Twitch Client ID",
             "TWITCH_CLIENT_SECRET": "Twitch Client Secret"}
    need = (["YANDEX_MUSIC_TOKEN"] if VALUES.get("SRC_YANDEX") else []) + (
        ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"] if need_twitch else [])
    return [names[k] for k in need if not VALUES.get(k)]


load()
