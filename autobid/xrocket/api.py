"""
Клиент официального Exchange API xRocket (https://docs.xrocket.exchange).

В отличие от CryptoBot — это НАСТОЯЩИЙ REST API: авторизация одним Bearer-токеном, без подписи,
без эмуляции кликов и без маскировки (ровно то, что требует заказчик). Токен берётся в боте
(@xRocket для mainnet, @xrocket_testnet_bot для testnet → Settings → Exchange settings → API token)
и кладётся в config.py. Токен нельзя ограничить в правах: он даёт полный доступ к счёту, включая
переводы и вывод, поэтому разработке давать только ТЕСТОВЫЙ токен (@xrocket_testnet_bot).

Особенности API (из документации):
  • числа (цены, объёмы) приходят и отправляются СТРОКАМИ — работаем через Decimal;
  • символ пары — "BASE-QUOTE" через дефис, например "TON-USDT";
  • отдельного метода смены цены НЕТ: меняем цену отменой (DELETE /order) и новым ордером (POST /orders);
  • для торговли деньги должны быть на балансе Trading, не Funding (перевод — отдельный метод);
  • лимит частоты → HTTP 429 с заголовком Retry-After.

Эндпоинты под /api/v1. Часть схем в доке не отрисовалась (генерируются из OpenAPI) — поля помечены
как предполагаемые и проверяются на testnet; см. docs/XROCKET.md.
"""
import asyncio
import json as _json
from decimal import Decimal

import aiohttp

BASE_PROD = "https://exchange.api.xrocket.exchange"
BASE_TESTNET = "https://exchange.api.testnet.xrocket.exchange"
API_PREFIX = "/api/v1"

# статусы ордера: working/pending/sending — живой; completed/cancelled/rejected/expired — финал
TERMINAL_STATUS = {"completed", "cancelled", "rejected", "expired"}


class XRocketError(RuntimeError):
    """Ошибка API. detail — человекочитаемое описание из problem-details, если есть."""

    def __init__(self, status, detail, body=None):
        super().__init__(f"xRocket HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
        self.body = body


class AuthError(XRocketError):
    """401/403 — неверный или просроченный токен (или доступ запрещён)."""


class RateLimited(XRocketError):
    """429 — превышен лимит частоты. retry_after — сколько ждать, сек (из заголовка Retry-After)."""

    def __init__(self, status, detail, retry_after=None, body=None):
        super().__init__(status, detail, body)
        self.retry_after = retry_after


def symbol(base, quote):
    """('TON','USDT') → 'TON-USDT' (формат пары в xRocket)."""
    return f"{str(base).upper()}-{str(quote).upper()}"


def split_symbol(sym):
    """'TON-USDT' → ('TON','USDT')."""
    base, _, quote = str(sym).partition("-")
    return base.upper(), quote.upper()


def order_body(sym, side, size=None, price=None, funds=None, order_type="limit",
               tif="GTC", client_id=None, stop_price=None, remark=None):
    """Собрать тело POST /orders. Числа — строками. Пустые поля не отправляем."""
    body = {"symbol": sym, "side": str(side).lower(), "type": order_type}
    if price is not None:
        body["price"] = str(price)
    if size is not None:
        body["size"] = str(size)
    if funds is not None:
        body["funds"] = str(funds)
    if order_type in ("limit", "stopLimit") and tif:
        body["timeInForce"] = tif
    if stop_price is not None:
        body["stopPrice"] = str(stop_price)
    if client_id:
        body["clientOrderId"] = str(client_id)
    if remark:
        body["remark"] = str(remark)
    return body


def _levels(raw):
    """[["51190.00","0.45"], …] → [(Decimal('51190.00'), Decimal('0.45')), …]."""
    out = []
    for lvl in raw or []:
        try:
            out.append((Decimal(str(lvl[0])), Decimal(str(lvl[1]))))
        except (IndexError, TypeError, ArithmeticError):
            continue
    return out


def parse_orderbook(data):
    """Ответ стакана → {'bids': [(price,size)], 'asks': [(price,size)], 'sequence': str|None}.
    ВСЕГДА нормализуем порядок: bids — лучшими (бОльшая цена) сверху, asks — лучшими (меньшая)
    сверху. Это важно: REST отдаёт обе стороны по возрастанию цены (лучший bid — последний),
    а WS — лучшими сверху; сортировкой приводим к единому виду, на который рассчитан logic.decide."""
    data = data or {}
    bids = _levels(data.get("bids"))
    asks = _levels(data.get("asks"))
    bids.sort(key=lambda lvl: lvl[0], reverse=True)
    asks.sort(key=lambda lvl: lvl[0])
    return {
        "bids": bids,
        "asks": asks,
        "sequence": str(data["sequence"]) if data.get("sequence") is not None else None,
    }


def parse_problem(text):
    """Достать человекочитаемое описание из тела ошибки (RFC 7807 problem-details) или вернуть текст."""
    try:
        obj = _json.loads(text)
    except (ValueError, TypeError):
        return (text or "").strip()[:300]
    if isinstance(obj, dict):
        return str(obj.get("detail") or obj.get("title") or obj.get("message") or obj)[:300]
    return str(obj)[:300]


class XRocketExchange:
    """Асинхронный клиент Exchange API. Один экземпляр на аккаунт/токен."""

    def __init__(self, token, testnet=False, proxy=None, timeout=20):
        self.base = (BASE_TESTNET if testnet else BASE_PROD) + API_PREFIX
        self.token = token
        self.testnet = testnet
        self.proxy = proxy          # "socks5://..." / "http://..." — через aiohttp-socks при необходимости
        self._timeout = timeout
        self.http = None

    async def _session(self):
        if self.http is None:
            connector = None
            if self.proxy:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(self.proxy, rdns=True)
            headers = {"Accept": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self.http = aiohttp.ClientSession(
                connector=connector, headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout))
        return self.http

    async def _request(self, method, path, params=None, body=None):
        s = await self._session()
        kwargs = {}
        if params:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        if body is not None:
            kwargs["json"] = body
        if self.proxy:
            kwargs["proxy"] = self.proxy
        async with s.request(method, self.base + path, **kwargs) as r:
            text = await r.text()
            if r.status == 429:
                ra = r.headers.get("Retry-After")
                raise RateLimited(429, parse_problem(text), retry_after=_to_float(ra), body=text)
            if r.status in (401, 403):
                raise AuthError(r.status, parse_problem(text), body=text)
            if r.status >= 400:
                raise XRocketError(r.status, parse_problem(text), body=text)
            if not text:
                return {}
            try:
                return _json.loads(text)
            except ValueError:
                return {}

    # ------------------------------------------------------------------ публичные данные
    async def symbols(self):
        """Список всех торговых пар."""
        return await self._request("GET", "/symbols")

    async def symbol_info(self, sym):
        """Детали одной пары (шаг цены/объёма, допустимые precision и т.п.)."""
        return await self._request("GET", f"/symbols/{sym}")

    async def orderbook(self, sym, depth=20, precision=None):
        """Снимок стакана. Возвращает разобранный {'bids','asks','sequence'} (см. parse_orderbook)."""
        data = await self._request("GET", "/orderbook",
                                   params={"symbol": sym, "depth": depth, "precision": precision})
        return parse_orderbook(data)

    # ------------------------------------------------------------------ счёт (нужен токен)
    async def balances(self):
        """Балансы Trading (с них торгуем)."""
        return await self._request("GET", "/accounts/trading/balances")

    async def funding_balances(self):
        """Балансы Funding (кошелёк; на торговлю нужно перевести в Trading)."""
        return await self._request("GET", "/accounts/funding/balances")

    # ------------------------------------------------------------------ ордера (нужен токен)
    async def active_orders(self):
        """Все активные ордера аккаунта."""
        return await self._request("GET", "/orders/active")

    async def get_order(self, order_id=None, client_id=None):
        """Один ордер по orderId или clientOrderId (если заданы оба — берётся orderId)."""
        return await self._request("GET", "/order",
                                   params={"orderId": order_id, "clientOrderId": client_id})

    async def place_limit(self, sym, side, price, size, tif="GTC", client_id=None):
        """Выставить лимитный ордер. Возвращает созданный ордер (с id). side — 'buy'/'sell'."""
        body = order_body(sym, side, size=size, price=price, order_type="limit",
                          tif=tif, client_id=client_id)
        return await self._request("POST", "/orders", body=body)

    async def place_order(self, **fields):
        """Выставить произвольный ордер (market/stop и т.п.) — см. order_body."""
        return await self._request("POST", "/orders", body=order_body(**fields))

    async def cancel(self, order_id=None, client_id=None):
        """Отменить ордер по orderId или clientOrderId."""
        return await self._request("DELETE", "/order",
                                   params={"orderId": order_id, "clientOrderId": client_id})

    async def close(self):
        if self.http:
            await self.http.close()
            self.http = None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
