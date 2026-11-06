"""
Перебивка на бирже xRocket через официальный REST API — без эмуляции кликов и маскировки
(как требует заказчик). На каждый рынок — один свой лимитный ордер на заданной стороне.

Проход (step_once): прочитать свой активный ордер и стакан → решить (logic.decide: встать на шаг
лучше конкурента внутри коридора, не пересекая спред) → выставить/переставить. Смены цены в API нет,
поэтому «переставить» = отменить (DELETE /order) + новый ордер (POST /orders); между ними возможен
частичный фил — у отменённого ордера остаётся dealSize.

Ограничители (ТЗ §3–4, §7), общие с P2P:
  • коридор initial/max_dev_pct + on_limit (reset+пауза / hold / pause);
  • абсолютные min_price/max_price; защита от аномалий max_jump; предел числа перебиваний max_changes;
  • анти-«дёрганье» (конкурент держится N опросов); мин. интервал между правками min_interval_sec;
  • авто-пауза на 429 (Retry-After) и на череде ошибок; остановка на неверном токене; аварийный стоп
    по stop_event.
"""
import asyncio
import time

from .. import settings
from ..cryptobot import outbid as O
from . import logic
from .api import AuthError, RateLimited, XRocketError, XRocketExchange

CLIENT_PREFIX = "autobid"      # префикс clientOrderId, чтобы узнавать свои ордера и делать повторы идемпотентными
TERMINAL_STATUS = {"completed", "cancelled", "rejected", "expired"}


def _as_list(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def my_order_for(orders, sym, side):
    """Свой активный ордер по паре и стороне (не в финальном статусе). Первый подходящий."""
    for o in orders:
        if not isinstance(o, dict):
            continue
        if o.get("symbol") == sym and str(o.get("side", "")).lower() == side \
                and str(o.get("status", "")).lower() not in TERMINAL_STATUS:
            return o
    return None


def _remaining(order):
    """Сколько объёма ещё стоит в стакане: size - dealSize (для вычитания своего объёма с уровня)."""
    try:
        return O.to_dec(order.get("size") or 0) - O.to_dec(order.get("dealSize") or 0)
    except Exception:
        return None


class XRocketTrader:
    def __init__(self, market_key, cfg, api, log=print, trade_log=None):
        self.key = market_key
        self.sym = cfg.get("symbol") or market_key
        self.side = str(cfg.get("side", "buy")).lower()
        self.step = O.to_dec(cfg.get("step") or "0.0001")
        self.size = str(cfg.get("size") or cfg.get("amount") or "").strip()
        self.initial = cfg.get("initial")
        self.max_dev = cfg.get("max_dev_pct")
        self.on_limit = cfg.get("on_limit", "reset")
        self.limit_pause = float(cfg.get("limit_pause_sec", 30))
        self.min_price = cfg.get("min_price")
        self.max_price = cfg.get("max_price")
        self.max_jump = cfg.get("max_jump")
        self.max_changes = cfg.get("max_changes")
        self.min_interval = float(cfg.get("min_interval_sec", 0))
        self.poll = float(cfg.get("poll_sec", 3))
        self.depth = int(cfg.get("depth", 20))
        self.precision = cfg.get("precision")
        self.api = api
        self.log = log
        self.trade_log = trade_log
        self.stability = O.StabilityGuard(int(cfg.get("stability_polls", 2)))
        self._changes = 0
        self._last_change = 0.0
        self._paused_until = 0.0
        self._client_id = f"{CLIENT_PREFIX}-{self.sym}-{self.side}"

    def _paused(self, now=None):
        now = time.monotonic() if now is None else now
        return self._paused_until > now

    def _pause(self, sec=None, now=None):
        sec = self.limit_pause if sec is None else sec
        if sec and sec > 0:
            now = time.monotonic() if now is None else now
            self._paused_until = now + sec

    async def _my_order(self):
        data = await self.api.active_orders()
        return my_order_for(_as_list(data, "orders", "data", "result"), self.sym, self.side)

    async def step_once(self):
        if self._paused():
            return "pause"
        mine = await self._my_order()
        ob = await self.api.orderbook(self.sym, depth=self.depth, precision=self.precision)
        my_price = mine.get("price") if mine else None
        my_size = _remaining(mine) if mine else None

        d = logic.decide(ob["bids"], ob["asks"], self.side, self.step, my_price, my_size,
                         self.initial, self.max_dev, self.on_limit, self.min_price, self.max_price)
        act = d["action"]
        if act in ("hold", "none"):
            return act

        now = time.monotonic()
        if self.max_changes is not None and self._changes >= int(self.max_changes):
            self.log(f"[{self.key}] достигнут предел перебиваний ({self.max_changes}) — не меняю")
            return "limit"
        if self.min_interval and (now - self._last_change) < self.min_interval:
            return "wait"                                     # мин. интервал между правками (ТЗ §2)

        if d["reset"] and self.on_limit == "pause":          # край коридора, режим «приостановить»
            self._pause()
            self.log(f"[{self.key}] край коридора — пауза {self.limit_pause:.0f} с")
            return "pause"

        if not d["reset"]:                                   # обычная перебивка — защиты от аномалий/дёрганья
            if self.max_jump is not None and my_price is not None \
                    and not O.allowed_jump(O.jump_pct(my_price, d["price"]), self.max_jump):
                self.log(f"[{self.key}] скачок больше max_jump={self.max_jump} — пропускаю (аномалия?)")
                return "jump"
            comp = logic.best_competitor(ob["bids"] if self.side == "buy" else ob["asks"], my_price, my_size)
            if comp and not self.stability.confirm(self._client_id, comp[0]):
                return "flap"                                 # конкурент ещё не продержался N опросов

        if not self.size:
            self.log(f"[{self.key}] не задан size/amount — не могу выставить ордер")
            return "nosize"

        if act == "replace" and mine:
            await self.api.cancel(order_id=mine.get("id"))    # смены цены нет → отмена + новый ордер
        await self.api.place_limit(self.sym, self.side, d["price"], self.size, client_id=self._client_id)

        self._changes += 1
        self._last_change = now
        if d["reset"] and self.on_limit == "reset":
            self._pause()                                     # вернулись к началу → небольшая пауза (ТЗ п.3)
        tail = " (край коридора — возврат к начальному)" if d["reset"] else ""
        self.log(f"[{self.key}] {self.side} ордер → {d['price']}{tail}")
        if self.trade_log is not None:
            self.trade_log.record(market=f"XR {self.key}", side=self.side, action=act,
                                  new=str(d["price"]), reset=d["reset"], changes=self._changes)
        return act

    async def run(self, stop_event=None):
        self.log(f"[{self.key}] xRocket запущена: {self.sym} {self.side}, шаг {self.step}, "
                 f"опрос каждые {self.poll:.0f} с")
        errors = 0
        while stop_event is None or not stop_event.is_set():
            try:
                await self.step_once()
                errors = 0
            except AuthError as e:
                self.log(f"[{self.key}] токен не принят ({e.status}) — останавливаю рынок: {e.detail}")
                return
            except RateLimited as e:
                wait = e.retry_after or self.poll * 5
                self.log(f"[{self.key}] лимит частоты (429) — пауза {wait:.0f} с")
                await asyncio.sleep(wait)
                continue
            except XRocketError as e:
                errors += 1
                self.log(f"[{self.key}] ошибка API: {e}")
                if errors >= 5:                               # череда ошибок → длинная пауза (ТЗ §7)
                    await asyncio.sleep(min(self.poll * 2 ** errors, 120))
            except Exception as e:
                errors += 1
                self.log(f"[{self.key}] сбой прохода: {type(e).__name__}: {str(e)[:200]}")
            await asyncio.sleep(self.poll)


async def build(market_key, cfg, log=print, trade_log=None):
    """Собрать XRocketTrader из конфига рынка (XROCKET_MARKETS)."""
    token = settings.get("XROCKET_TOKEN")
    if not token:
        raise ValueError("не задан XROCKET_TOKEN в config.py")
    api = XRocketExchange(token, testnet=bool(settings.get("XROCKET_TESTNET")))
    cfg = dict(cfg)
    cfg.setdefault("symbol", market_key)
    return XRocketTrader(market_key, cfg, api, log=log, trade_log=trade_log)
