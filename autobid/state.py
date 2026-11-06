"""
Состояние между запусками (ТЗ §5: «автоматическая работа после перезапуска»).

Простое key→value хранилище в атомарно переписываемом JSON-файле (state/<name>.json).
Нужно, чтобы перезапуск не сбрасывал защиту от частых правок (cooldown) и чтобы помнить
последний выставленный курс по каждому объявлению. Это не БД — данных мало, пишем целиком.
"""
import json
import os
import tempfile

from . import settings


def _dir():
    d = settings.state_dir()
    os.makedirs(d, exist_ok=True)
    return d


class Store:
    def __init__(self, name="state"):
        self.path = os.path.join(_dir(), name + ".json")
        self._data = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception:                        # нет файла или битый — начинаем с чистого
            self._data = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self._flush()

    def update(self, key, **fields):
        cur = dict(self._data.get(key) or {})
        cur.update(fields)
        self.set(key, cur)
        return cur

    def all(self):
        return dict(self._data)

    def _flush(self):
        try:
            d = os.path.dirname(self.path)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self.path)           # атомарная замена: файл не бьётся при сбое записи
        except Exception:
            pass
