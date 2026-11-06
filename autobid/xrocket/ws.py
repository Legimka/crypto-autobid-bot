"""
WebSocket-поток стакана xRocket — быстрее REST-опроса (ТЗ п.2/§7: «использовать WebSocket при
возможности», быстрая реакция). Поддерживает один публичный канал orderbook на символ: сервер шлёт
снимок, затем дельты (snapshot=false); размер 0 на уровне = уровень убрать. Пинг каждые 30 с (сервер
рвёт idle через 60 с). При обрыве — переподключение и новый снимок.

OrderBookStream.run() держит соединение; book() отдаёт текущий стакан в том же виде, что
api.parse_orderbook (bids лучшими сверху, asks лучшими сверху). Разбор дельт — чистые функции (тесты).
"""
import asyncio
import json
from decimal import Decimal

try:
    import websockets
except ImportError:                      # пакет ставится из requirements.txt
    websockets = None

WS_PROD = "wss://exchange.app-api.xrocket.exchange/"
WS_TESTNET = "wss://exchange.app-api.testnet.xrocket.exchange/"
PING_EVERY = 30


def apply_levels(side_map, levels):
    """Применить уровни [[price,size],…] к {Decimal price: Decimal size}: size 0 — удалить уровень."""
    for lvl in levels or []:
        try:
            price, size = Decimal(str(lvl[0])), Decimal(str(lvl[1]))
        except (IndexError, TypeError, ArithmeticError):
            continue
        if size == 0:
            side_map.pop(price, None)
        else:
            side_map[price] = size


def book_from_maps(bids_map, asks_map, depth=None):
    """{price:size} → {'bids','asks'}: bids по убыванию, asks по возрастанию (лучшие сверху)."""
    bids = sorted(bids_map.items(), key=lambda kv: kv[0], reverse=True)
    asks = sorted(asks_map.items(), key=lambda kv: kv[0])
    if depth:
        bids, asks = bids[:depth], asks[:depth]
    return {"bids": [(p, s) for p, s in bids], "asks": [(p, s) for p, s in asks]}


class OrderBookStream:
    def __init__(self, symbol, testnet=False, depth=20, precision=None, log=print):
        self.symbol = symbol
        self.testnet = testnet
        self.depth = depth
        self.precision = precision
        self.log = log
        self._bids = {}
        self._asks = {}
        self._seq = None
        self._ready = asyncio.Event()

    @property
    def ready(self):
        return self._ready.is_set()

    def book(self):
        return {**book_from_maps(self._bids, self._asks, self.depth), "sequence": self._seq}

    def _reset(self):
        self._bids, self._asks, self._seq = {}, {}, None
        self._ready.clear()

    def _snapshot(self, obj):
        self._bids, self._asks = {}, {}
        apply_levels(self._bids, obj.get("bids"))
        apply_levels(self._asks, obj.get("asks"))
        self._seq = obj.get("sequence")
        self._ready.set()

    def _delta(self, obj):
        apply_levels(self._bids, obj.get("bids"))
        apply_levels(self._asks, obj.get("asks"))
        if obj.get("sequence") is not None:
            self._seq = obj.get("sequence")
        self._ready.set()

    def handle(self, msg):
        """Разобрать одно сообщение WS. Возвращает True, если стакан обновился."""
        if not isinstance(msg, dict):
            return False
        res = msg.get("result")
        if isinstance(res, dict) and ("bids" in res or "asks" in res):    # ответ на subscribe — снимок
            self._snapshot(res)
            return True
        if msg.get("method") == "subscription":
            data = (msg.get("params") or {}).get("data") or {}
            if data.get("snapshot"):
                self._snapshot(data)
            else:
                self._delta(data)
            return True
        return False

    def _subscribe_msg(self):
        params = {"channel": "orderbook", "symbol": self.symbol, "depth": self.depth}
        if self.precision:
            params["precision"] = self.precision
        return json.dumps({"id": "sub", "method": "subscribe", "params": params})

    async def _pinger(self, ws):
        try:
            while True:
                await asyncio.sleep(PING_EVERY)
                await ws.send(json.dumps({"id": "ping", "method": "ping"}))
        except (asyncio.CancelledError, Exception):
            return

    async def run(self, stop_event=None):
        if websockets is None:
            raise RuntimeError("не установлен пакет websockets (pip install websockets)")
        url = WS_TESTNET if self.testnet else WS_PROD
        while stop_event is None or not stop_event.is_set():
            try:
                async with websockets.connect(url, ping_interval=None, open_timeout=15) as ws:
                    self._reset()
                    await ws.send(self._subscribe_msg())
                    ping = asyncio.create_task(self._pinger(ws))
                    try:
                        while stop_event is None or not stop_event.is_set():
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                            except asyncio.TimeoutError:
                                continue
                            try:
                                self.handle(json.loads(raw))
                            except (ValueError, TypeError):
                                continue
                    finally:
                        ping.cancel()
            except Exception as e:
                self.log(f"[ws {self.symbol}] обрыв: {type(e).__name__}: {str(e)[:150]} — переподключение")
            if stop_event is not None and stop_event.is_set():
                break
            await asyncio.sleep(2)
