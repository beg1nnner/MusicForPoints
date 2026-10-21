"""Обёртка над неофициальным API Яндекс Музыки (библиотека yandex-music)."""
import logging
import re
from pathlib import Path

from yandex_music import ClientAsync

import config

log = logging.getLogger("ym")

# music.yandex.ru/album/123/track/456  или  music.yandex.ru/track/456
TRACK_URL_RE = re.compile(r"music\.yandex\.[a-z]+/(?:album/(\d+)/)?track/(\d+)")


class YandexMusic:
    def __init__(self) -> None:
        self.client: ClientAsync | None = None

    async def init(self) -> None:
        self.client = await ClientAsync(config.YM_TOKEN).init()
        acc = self.client.me.account
        has_plus = bool(self.client.me.plus and self.client.me.plus.has_plus)
        log.info("Яндекс Музыка: %s (Plus: %s)", acc.login, "да" if has_plus else "нет")
        if not has_plus:
            log.warning("Без подписки Яндекс Плюс треки могут играть только фрагментом.")

    async def find(self, query: str):
        """Возвращает объект Track из yandex-music или None."""
        match = TRACK_URL_RE.search(query)
        if match:
            album_id, track_id = match.groups()
            key = f"{track_id}:{album_id}" if album_id else track_id
            tracks = await self.client.tracks([key])
            return tracks[0] if tracks and tracks[0].available else None

        found = await self.search_many(query, 1)
        return found[0] if found else None

    async def search_many(self, query: str, limit: int = 5) -> list:
        """Доступные треки по тексту запроса, лучшие первыми."""
        result = await self.client.search(query, type_="track")
        if not result or not result.tracks or not result.tracks.results:
            return []
        return [t for t in result.tracks.results if t.available][:limit]

    async def download(self, track, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await track.download_async(str(dest), codec="mp3", bitrate_in_kbps=192)
        return dest

    @staticmethod
    def cover_url(track, size: str = "400x400") -> str:
        uri = getattr(track, "cover_uri", None)
        return f"https://{uri.replace('%%', size)}" if uri else ""

    @staticmethod
    def artists(track) -> str:
        return ", ".join(a.name for a in track.artists) or "Неизвестный исполнитель"
