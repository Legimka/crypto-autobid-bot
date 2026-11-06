"""
Настройки из config.py в корне проекта (его создают из config.example.py, в git он не попадает).
Модуль читает config.py лениво: скрипты с --help и тесты работают и без него.
"""
import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DEFAULTS = {
    "API_ID": 0,
    "API_HASH": "",
    "MY_NICKNAME": "",
    "SESSIONS_DIR": "sessions",       # где лежат *.session; можно абсолютный путь вне проекта
    "RECORDINGS_DIR": "recordings",   # куда рекордеры пишут записи
    "PROXIES": {},                    # прокси по имени сессии: {"observer1": "socks5://..."}
    "OBSERVER_PROXIES": {},           # старое имя из cryptobot-p2p-bot, тоже учитывается
    "MARKETS": {},                    # рынки P2P (перебивка): см. config.example.py
    "EXCHANGE": {},                   # рынки биржи (перебивка): см. config.example.py
    "LOGS_DIR": "logs",               # куда писать логи (autobid.log) и журнал смен курса (trades.jsonl)
    "STATE_DIR": "state",             # состояние между перезапусками (cooldown, последние курсы)
    "LOG_LEVEL": "INFO",              # DEBUG / INFO / WARNING
}


class ConfigError(RuntimeError):
    pass


_cfg = None


def _load():
    global _cfg
    if _cfg is None:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        try:
            _cfg = importlib.import_module("config")
        except ModuleNotFoundError as e:
            if e.name != "config":
                raise
            _cfg = False
    return _cfg


def get(name):
    cfg = _load()
    if cfg and hasattr(cfg, name):
        return getattr(cfg, name)
    return _DEFAULTS[name]


def _path(value):
    return value if os.path.isabs(value) else os.path.join(ROOT, value)


def sessions_dir():
    return _path(get("SESSIONS_DIR"))


def recordings_dir():
    return _path(get("RECORDINGS_DIR"))


def logs_dir():
    return _path(get("LOGS_DIR"))


def state_dir():
    return _path(get("STATE_DIR"))


def require_api():
    """API_ID/API_HASH приложения Telegram (my.telegram.org) — без них Telethon не подключится."""
    if _load() is False:
        raise ConfigError("Нет файла config.py. Скопируй config.example.py в config.py "
                          "и впиши API_ID и API_HASH (можно взять из config.py старого бота).")
    if not get("API_ID") or not get("API_HASH"):
        raise ConfigError("В config.py не заполнены API_ID и API_HASH "
                          "(my.telegram.org → API development tools или из config.py старого бота).")
    return int(get("API_ID")), str(get("API_HASH"))
