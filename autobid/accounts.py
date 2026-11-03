"""Аккаунты Telegram: один клиент Telethon на файл сессии, прокси — из config.PROXIES.
Перенесено из cryptobot-p2p-bot (accounts.py); сессии теперь лежат в SESSIONS_DIR."""
import os
import sqlite3
from urllib.parse import urlsplit, unquote

from telethon import TelegramClient

from . import settings


class NotAuthorized(RuntimeError):
    pass


class SessionBusy(RuntimeError):
    """Файл сессии уже открыт другим процессом рекордера (SQLite-замок Telethon)."""


def session_path(name):
    """Путь сессии без расширения. Если сессия осталась в корне проекта (как в старом боте) — берём её."""
    folder = settings.sessions_dir()
    path = os.path.join(folder, name)
    old = os.path.join(settings.ROOT, name)
    if not os.path.exists(path + ".session") and os.path.exists(old + ".session"):
        return old
    os.makedirs(folder, exist_ok=True)
    return path


def list_sessions():
    """Имена существующих файлов сессий (в SESSIONS_DIR и в корне проекта)."""
    names = set()
    for folder in (settings.sessions_dir(), settings.ROOT):
        try:
            names.update(f[:-8] for f in os.listdir(folder) if f.endswith(".session"))
        except OSError:
            pass
    return sorted(names)


def session_exists(name):
    return os.path.exists(session_path(name) + ".session")


def _lock_session(name):
    """Эксклюзивная блокировка файла сессии: два рекордера на один аккаунт рушат друг друга через SQLite.
    Дескриптор надо держать открытым, пока жив клиент; при выходе процесса ОС снимет замок сама."""
    lock_file = open(session_path(name) + ".lock", "w")
    try:
        if os.name == "nt":
            import msvcrt
            lock_file.write("lock")
            lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        lock_file.close()
        raise SessionBusy(f"Сессия «{name}» уже открыта в другом окне рекордера — "
                          f"закончи там запись (!q или закрой окно) и запусти снова") from e
    return lock_file


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
    """Подключённый клиент сессии; если вход не выполнен — понятная ошибка с подсказкой.
    Новые файлы сессий заводит только qr_login — опечатка в имени не создаёт пустую сессию."""
    if not session_exists(name):
        have = list_sessions()
        hint = f"Есть сессии: {', '.join(have)}. " if have else ""
        raise NotAuthorized(f"Нет сессии «{name}». {hint}Войди: python qr_login.py {name}")
    lock = _lock_session(name)
    client = new_client(name)
    try:
        await client.connect()
    except sqlite3.OperationalError as e:
        lock.close()
        raise SessionBusy(f"Сессия «{name}» занята другим процессом — закрой другое окно рекордера") from e
    if not await client.is_user_authorized():
        await client.disconnect()
        lock.close()
        raise NotAuthorized(f"Сессия «{name}» не авторизована — сначала: python qr_login.py {name}")
    client._autobid_lock = lock   # держим блокировку, пока жив клиент
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
