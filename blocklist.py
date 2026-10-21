"""Чёрный список зрителей и треков (хранится в blocklist.json)."""
import json
import logging

import config

log = logging.getLogger("blocklist")
FILE = config.BASE_DIR / "blocklist.json"

users: list[str] = []          # ники в нижнем регистре
tracks: dict[str, dict] = {}   # id трека -> {"id", "title", "artists"}


def _load() -> None:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    users[:] = [norm_user(u) for u in data.get("users", []) if norm_user(u)]
    for t in data.get("tracks", []):
        if t.get("id"):
            tracks[str(t["id"])] = {"id": str(t["id"]), "title": t.get("title", ""), "artists": t.get("artists", "")}


def _save() -> None:
    FILE.write_text(json.dumps(snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")


def norm_user(name: str) -> str:
    return (name or "").strip().lstrip("@").lower()


def snapshot() -> dict:
    return {"users": sorted(users), "tracks": list(tracks.values())}


# ---- зрители ----
def is_user_blocked(*names: str) -> bool:
    """Проверяет и логин, и отображаемое имя (они могут отличаться)."""
    return any(norm_user(n) in users for n in names if n)


def add_user(name: str) -> str:
    key = norm_user(name)
    if key and key not in users:
        users.append(key)
        _save()
    return key


def remove_user(name: str) -> bool:
    key = norm_user(name)
    if key in users:
        users.remove(key)
        _save()
        return True
    return False


# ---- треки ----
def is_track_blocked(track_id: str) -> bool:
    return str(track_id) in tracks


def add_track(track_id: str, title: str = "", artists: str = "") -> None:
    tracks[str(track_id)] = {"id": str(track_id), "title": title, "artists": artists}
    _save()


def remove_track(track_id: str) -> bool:
    if tracks.pop(str(track_id), None) is not None:
        _save()
        return True
    return False


_load()
