"""
Клиент внутреннего API мини-приложения CryptoBot (app.cr.bot). Публичного API у P2P нет —
эндпоинты взяты из кода самого приложения, при обновлении CryptoBot могут поменяться.

Вход: Telethon запрашивает у Telegram ссылку мини-приложения для аккаунта, из неё берём initData
и меняем на cookie-сессию через /authentication/webapp — ровно как это делает приложение.
"""
import asyncio
import time
from urllib.parse import urlsplit, parse_qs, unquote

import aiohttp
from telethon import functions

APP_ORIGIN = "https://app.cr.bot"
# Представляемся WebView Telegram на Android (иначе aiohttp шлёт "Python/3.x aiohttp/3.x").
USER_AGENT = ("Mozilla/5.0 (Linux; Android 14; SM-S918B Build/UP1A.231005.007; wv) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Version/4.0 Chrome/129.0.6668.100 Mobile Safari/537.36")
API = APP_ORIGIN + "/internal/v1"
PAGE_SIZE = 20


class AppAuthError(Exception):
    pass


def _num(v, default=0.0):
    """Числа в API приходят строками; на всякий случай поддерживаем и {value: ...}."""
    if isinstance(v, dict):
        v = v.get("value", v.get("amount"))
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_offer(o, decimals=18):
    """price и limits — строки в фиате; volume — строка в минимальных единицах монеты
    (USDT: "6336000000000000000" = 6.336). Ник: username, а у кого его нет — сгенерированное name."""
    user = o.get("user") or {}
    limits = o.get("limits") or {}
    return {
        "id": o.get("id"),
        "p": round(_num(o.get("price")), 6),
        "lmin": int(_num(limits.get("min"))),
        "lmax": int(_num(limits.get("max"))),
        "vol": round(_num(o.get("volume")) / 10 ** decimals, 6),
        "n": user.get("username") or user.get("name") or "-",
        "uid": user.get("id"),
        "orders": user.get("orders") or 0,
        "available": o.get("is_available") is not False,
        "ptype": o.get("price_type") or "fixed",            # fixed / floating
        "ratio": _num(o.get("price_floating_ratio")),       # плавающая: % от биржевого курса (94.3 = -5.7%)
    }


class CryptoBotApp:
    def __init__(self, tg_client, bot_username="send", proxy=None):
        self.tg = tg_client
        self.bot_username = bot_username
        self.proxy = proxy   # "socks5://..." / "http://..." — запросы к CryptoBot идут через него
        self.http = None
        self._auth_lock = asyncio.Lock()
        self.authed_at = 0

    async def _init_data(self):
        bot = await self.tg.get_input_entity(self.bot_username)
        res = await self.tg(functions.messages.RequestMainWebViewRequest(peer=bot, bot=bot, platform="android"))
        frag = urlsplit(res.url).fragment
        data = parse_qs(frag).get("tgWebAppData")
        if not data:
            raise AppAuthError("Telegram не вернул tgWebAppData")
        return data[0] if '=' in data[0] else unquote(data[0])

    async def login(self):
        async with self._auth_lock:
            if self.http is None:
                connector = None
                if self.proxy:
                    from aiohttp_socks import ProxyConnector
                    connector = ProxyConnector.from_url(self.proxy, rdns=True)
                self.http = aiohttp.ClientSession(
                    connector=connector,
                    cookie_jar=aiohttp.CookieJar(),
                    headers={"Origin": APP_ORIGIN, "Referer": APP_ORIGIN + "/",
                             "Accept": "application/json, text/plain, */*", "X-Telegram-Platform": "android",
                             "User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
                             "X-Requested-With": "org.telegram.messenger"},
                    timeout=aiohttp.ClientTimeout(total=20))
            init_data = await self._init_data()
            async with self.http.post(API + "/authentication/webapp", json={"initData": init_data}) as r:
                if r.status >= 400:
                    raise AppAuthError(f"Вход в приложение: HTTP {r.status} {(await r.text())[:300]}")
            self.authed_at = time.time()

    async def _get(self, path, params=None, retry=True):
        if self.http is None:
            await self.login()
        async with self.http.get(API + path, params=params) as r:
            if r.status in (401, 403) and retry:
                await self.login()
                return await self._get(path, params, retry=False)
            if r.status in (502, 503, 504) and retry:   # у CryptoBot бывают короткие сбои
                await asyncio.sleep(2)
                return await self._get(path, params, retry=False)
            if r.status == 429:
                raise RuntimeError("CryptoBot ограничил частоту запросов (429) — увеличь POLL_SEC")
            r.raise_for_status()
            return await r.json()

    async def _assets(self):
        if not hasattr(self, "_asset_info"):
            data = await self._get("/p2p/currencies/assets")
            self._asset_info = {a["code"]: a for a in data.get("assets", [])}
        return self._asset_info

    async def asset_decimals(self, asset):
        return (await self._assets()).get(asset, {}).get("decimals", 18)

    async def asset_names(self, asset):
        """Как монета может называться в чате: код и имя (TON в чате CryptoBot — «GRAM»)."""
        info = (await self._assets()).get(asset, {})
        return sorted({asset.upper(), str(info.get("name") or asset).upper()})

    async def offers(self, asset, fiat, side, max_pages=5, stop=None, min_pages=1):
        """Стакан: список объявлений в порядке, как в приложении.
        side='buy' — вкладка «Купить» (продавцы USDT, цена по возрастанию),
        side='sell' — вкладка «Продать» (покупатели USDT, цена по убыванию).
        stop(offer) — дальше не листать, когда на странице нашлось такое объявление (например, своё)."""
        decimals = await self.asset_decimals(asset)
        result, cursor = [], None
        for n in range(1, max_pages + 1):
            params = {"type": side, "asset": asset, "fiat": fiat, "count": PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/p2p/offers", params)
            page = [parse_offer(o, decimals) for o in (data.get("offers") or []) if o and o.get("id")]
            result += page
            cursor = data.get("cursor")
            if not cursor or len(page) < PAGE_SIZE or (stop and n >= min_pages and any(stop(d) for d in result)):
                break
        return result

    async def my_offer_ids(self):
        """id объявлений этого аккаунта — чтобы не пытаться менять чужие."""
        data = await self._get("/p2p/offers/my", {"limit": 100, "offset": 0})
        return {o.get("id") for o in (data.get("offers") or []) if o}

    async def set_price(self, offer_id, value, floating=False, retry=True):
        """Поменять курс своего объявления — тот же запрос, что в приложении.
        Фиксированная: value — курс ("45.68"). Плавающая: value — отклонение от биржевого курса в % ("-5.6")."""
        if self.http is None:
            await self.login()
        body = {"price": {"type": "floating" if floating else "fixed", "value": str(value)}}
        async with self.http.patch(f"{API}/p2p/offers/{offer_id}", json=body) as r:
            if r.status in (401, 403) and retry:
                await self.login()
                return await self.set_price(offer_id, value, floating, retry=False)
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:300]}")
            return await r.json()

    async def close(self):
        if self.http:
            await self.http.close()
