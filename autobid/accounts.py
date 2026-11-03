"""Аккаунты Telegram: один клиент Telethon на файл сессии, прокси — из config.PROXIES.
Перенесено из cryptobot-p2p-bot (accounts.py); сессии теперь лежат в SESSIONS_DIR."""
import os
from urllib.parse import urlsplit, unquote

from telethon import TelegramClient

from . import settings


class NotAuthorized(RuntimeError):
    pass


def session_path(name):
    """Путь сессии без расширения. Если сессия осталась в корне проекта (как в старом боте) — берём её."""
    folder = settings.sessions_dir()
    path = os.path.join(folder, name)
    old = os.path.join(settings.ROOT, name)
    if not os.path.exists(path + ".session") and os.path.exists(old + ".session"):
        return old
    os.makedirs(folder, exist_ok=True)
    return path


def proxy_url(name):
    return settings.get("PROXIES").get(name) or settings.get("OBSERVER_PROXIES").get(name) or None


def telethon_proxy(url):
    """'socks5://user:pass@host:port' → формат прокси Telethon (нужен python-socks, ставится с aiohttp-socks)."""
    if not url:
        return None
    u = urlsplit(url)
    kind = u.scheme.lower().replace("socks5h", "socks5")
    if kind not in ("socks5", "socks4", "http"):
        raise ValueError(f"прокси {url}: поддерживаются socks5://, socks4://, http://")
    return {"proxy_type": kind, "addr": u.hostname, "port": u.port, "rdns": True,
            "username": unquote(u.username) if u.username else None,
            "password": unquote(u.password) if u.password else None}


def new_client(name):
    api_id, api_hash = settings.require_api()
    return TelegramClient(session_path(name), api_id, api_hash, proxy=telethon_proxy(proxy_url(name)))


async def connect_authorized(name):
    """Подключённый клиент сессии; если вход не выполнен — понятная ошибка с подсказкой."""
    client = new_client(name)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise NotAuthorized(f"Сессия «{name}» не авторизована — сначала: python qr_login.py {name}")
    return client


_clients = {}


async def get_tg(name):
    """Общий подключённый клиент сессии (наблюдатель и перебивающий могут совпадать в тесте)."""
    if name not in _clients:
        _clients[name] = await connect_authorized(name)
    return _clients[name]


async def drop_tg(name):
    c = _clients.pop(name, None)
    if c:
        await c.disconnect()
