"""Общее для рекордеров: папка записи, журнал событий (JSONL), чтение команд из консоли."""
import asyncio
import json
import os
import re
import sys
import threading
import time
from datetime import datetime

from .. import redact, settings


def utf8_console():
    """Эмодзи и кириллица в консоли Windows (cp1251/cp866) без падений."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def make_recording_dir(kind, bot_slug, label=None, base=None):
    """recordings/20261103-151203_webapp_cryptobot_birzha"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}_{kind}_{bot_slug}"
    if label:
        name += "_" + re.sub(r"[^\w-]+", "_", label, flags=re.U).strip("_")[:40]
    path = os.path.join(base or settings.recordings_dir(), name)
    n, final = 1, path
    while os.path.exists(final):
        n += 1
        final = f"{path}-{n}"
    os.makedirs(final)
    return final


class EventLog:
    """events.jsonl: одна строка — одно событие. Перед записью всё ещё раз проходит через redact —
    защита на случай, если вызывающий код что-то не замаскировал."""

    def __init__(self, path, aggressive=False):
        self.path = path
        self.aggressive = aggressive
        self.seq = 0
        self.f = open(path, "a", encoding="utf-8", buffering=1)
        self.t0 = time.time()

    def write(self, kind, **data):
        self.seq += 1
        now = time.time()
        event = {"seq": self.seq, "t": datetime.fromtimestamp(now).strftime("%H:%M:%S.%f")[:-3],
                 "ts": round(now, 3), "kind": kind}
        event.update(redact.redact_obj(data, aggressive=self.aggressive))
        if not self.f.closed:
            self.f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        return event

    def close(self):
        if not self.f.closed:
            self.f.close()


def read_events(path):
    events = []
    if not os.path.exists(path):
        return events
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
    return events


class ConsoleLines:
    """Строки, введённые в консоли, — в asyncio.Queue. Поток-демон не мешает выйти по Ctrl+C
    (обычный asyncio.to_thread(input) ждал бы Enter при выходе)."""

    def __init__(self, loop=None):
        self.loop = loop or asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                line = ""
            if line == "":   # stdin закрыт
                self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
                return
            self.loop.call_soon_threadsafe(self.queue.put_nowait, line.rstrip("\r\n"))

    async def get(self):
        return await self.queue.get()


def human_size(n):
    if n is None:
        return ""
    for unit in ("Б", "КБ", "МБ"):
        if n < 1024 or unit == "МБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} МБ"
