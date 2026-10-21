"""Проверка HTTPS-сертификатов.

Собранная программа видит только те корневые сертификаты Windows, которые уже лежат в хранилище.
Недостающие Windows догружает сама, но только для своих программ, поэтому у некоторых сайтов
(например, open.spotify.com) проверка падала с CERTIFICATE_VERIFY_FAILED. Добавляем набор certifi.
"""
import logging
import ssl
from functools import lru_cache

log = logging.getLogger("netssl")


@lru_cache(maxsize=1)
def context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()  # системные сертификаты Windows
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception as e:  # certifi нет: остаются только системные
        log.warning("Не удалось подключить набор сертификатов certifi: %s", e)
    return ctx
