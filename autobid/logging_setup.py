"""
Логирование — требование ТЗ §7: «логировать все изменения курса и действия бота».

setup() поднимает логгер "autobid": вывод в консоль и в файл logs/autobid.log с ротацией.
TradeLog — отдельный машиночитаемый журнал смен курса (logs/trades.jsonl, по строке на действие):
по нему потом строится «История операций» в интерфейсе (ТЗ §6) и разбор, что бот делал ночью.
"""
import json
import logging
import logging.handlers
import os
import time

from . import settings

_READY = False


def _dir():
    d = settings.logs_dir()
    os.makedirs(d, exist_ok=True)
    return d


def setup(level=None):
    """Настроить логгер "autobid" один раз. Возвращает его же при повторных вызовах."""
    global _READY
    log = logging.getLogger("autobid")
    if _READY:
        return log
    lvl = str(level or settings.get("LOG_LEVEL") or "INFO").upper()
    log.setLevel(getattr(logging, lvl, logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname).1s %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)

    try:
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(_dir(), "autobid.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception as e:                       # лог в файл не критичен — консоль всё равно работает
        log.warning("не удалось открыть файл лога: %s", e)

    log.propagate = False
    _READY = True
    return log


class TradeLog:
    """JSONL-журнал смен курса. record(market=..., offer=..., old=..., new=..., reason=...)."""

    def __init__(self, path=None):
        self.path = path or os.path.join(_dir(), "trades.jsonl")

    def record(self, **fields):
        fields.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(fields, ensure_ascii=False) + "\n")
        except Exception:                        # журнал не должен ронять торговлю
            pass
