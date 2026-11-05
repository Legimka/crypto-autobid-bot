"""
Перебивка на P2P-рынке CryptoBot.

Наблюдатель читает стакан через внутренний API мини-приложения (app_api.CryptoBotApp), перебивающий
меняет курс своего объявления — тем же API (быстро) или через диалог в чате (chat_trader.ChatTrader,
«как человек», с паузами). Это новее старого бота (old_bot_p2p), который кликал кнопки в чате.

Логика соревнования перенесена из cryptobot-p2p-bot/stakan.py: считаемся только внутри своего
МИНИМАЛЬНОГО лимита (в CryptoBot покупатели фильтруют по нему), встаём на шаг выше ближайшего
конкурента. Поверх добавлено из ТЗ:
  • коридор — максимальное отклонение от исходного курса; на границе возвращаемся к исходному;
  • защита от аномалий — не прыгать больше max_jump за правку;
  • защита от частых правок — не больше N правок одного объявления за окно, потом пауза.

Конфиг рынка (settings.MARKETS["USDT/RUB"]) — см. config.example.py.
"""
import asyncio
import time

from .. import accounts, settings
from . import outbid as O
from .app_api import CryptoBotApp
from .chat_trader import ChatTrader, FlowError, format_margin

BOT = "send"   # @CryptoBot


def is_mine(offer, my_names, my_ids):
    """Объявление принадлежит перебивающему аккаунту: по id (точно) или по нику в стакане."""
    return offer.get("id") in my_ids or str(offer.get("n", "")).lower() in my_names


class CooldownGuard:
    """Не больше max_changes правок одного объявления подряд, потом пауза cooldown_min минут
    (перенос защиты из old_bot_p2p/utils/changeOrder.py: 5 правок, затем ~11 минут)."""

    def __init__(self, max_changes=5, cooldown_min=11):
        self.max_changes = max_changes
        self.cooldown = cooldown_min * 60
        self._state = {}   # offer_id -> {"count": int, "ts": monotonic}

    def allow(self, offer_id, now=None):
        now = time.monotonic() if now is None else now
        st = self._state.get(offer_id)
        if st is None or st["count"] < self.max_changes:
            return True
        return (now - st["ts"]) > self.cooldown

    def record(self, offer_id, now=None):
        now = time.monotonic() if now is None else now
        st = self._state.get(offer_id)
        if st is None or st["count"] >= self.max_changes:
            self._state[offer_id] = {"count": 1, "ts": now}
        else:
            st["count"] += 1
            st["ts"] = now


class P2POutbidder:
    """Одна пара (рынок). observer — CryptoBotApp для чтения стакана; для смены курса либо trader
    (CryptoBotApp того же/другого аккаунта), либо chat (ChatTrader)."""

    def __init__(self, market_key, cfg, observer, trader=None, chat=None, log=print):
        self.key = market_key
        self.cfg = cfg
        self.asset = cfg["asset"]
        self.fiat = cfg["fiat"]
        self.side = cfg.get("side", "buy")          # 'buy' — вкладка «Купить», 'sell' — «Продать»
        self.asc = self.side == "buy"               # buy-стакан по возрастанию цены
        self.floating = bool(cfg.get("floating", False))
        self.step = cfg.get("step")
        self.pct_step = cfg.get("pct_step", "0.01")
        self.max_dev = cfg.get("max_dev_pct")       # коридор фикс. цены, %
        self.max_dev_pp = cfg.get("max_dev_pp")     # коридор плавающей цены, п.п. ratio
        self.initial = cfg.get("initial")           # исходный курс (фикс)
        self.initial_ratio = cfg.get("initial_ratio")
        self.max_jump = cfg.get("max_jump", 5)      # не прыгать больше: % (фикс) / п.п. (плав)
        self.poll = float(cfg.get("poll_sec", 6))
        self.max_pages = int(cfg.get("max_pages", 5))
        self.trader_mode = cfg.get("trader_mode", "app")   # 'app' | 'chat'
        self.observer = observer
        self.trader = trader
        self.chat = chat
        self.log = log
        self.cooldown = CooldownGuard(int(cfg.get("cooldown_changes", 5)), float(cfg.get("cooldown_min", 11)))
        self._names = None
        self._my_names = set()

    async def _ensure_meta(self):
        if self._names is None:
            self._names = await self.observer.asset_names(self.asset)
            nick = str(settings.get("MY_NICKNAME") or "").lower()
            self._my_names = {nick} if nick else set()

    async def _read_book(self):
        book = await self.observer.offers(self.asset, self.fiat, self.side, max_pages=self.max_pages)
        return [d for d in book if d.get("available")]

    async def _my_ids(self):
        src = self.trader if (self.trader_mode == "app" and self.trader) else self.observer
        try:
            return await src.my_offer_ids()
        except Exception as e:                       # бывает у наблюдателя без своих объявлений
            self.log(f"[{self.key}] не прочитал свои объявления: {e}")
            return set()

    def plan(self, book, my_ids):
        """Чистое решение: для каждого своего объявления — куда переставить курс (или ничего).
        Возвращает список правок [{offer, leader, value, show, floating, jump, reset}]."""
        plans = []
        for i, me in enumerate(book):
            if not is_mine(me, self._my_names, my_ids):
                continue
            enemies = [book[x] for x in range(i)
                       if not is_mine(book[x], self._my_names, my_ids) and book[x]["lmin"] == me["lmin"]]
            if not enemies:
                continue                             # я уже первый в своей лимитке — не трогаю
            leader = enemies[0]
            if self.floating:
                t = O.floating_target(me.get("ratio") or 0, leader.get("ratio") or 0, self.asc,
                                      self.pct_step, self.initial_ratio, self.max_dev_pp)
                value, show = format_margin(t["margin"]), f"{format_margin(t['margin'])}%"
                same = abs(t["ratio"] - O.to_dec(me.get("ratio") or 0)) < O.to_dec(self.pct_step) / 2
            else:
                t = O.fixed_target(me["p"], leader["p"], self.asc, self.step, self.initial, self.max_dev)
                if not t:
                    continue
                value = show = str(t["value"])
                same = abs(t["value"] - O.to_dec(me["p"])) < O.step_for(me["p"], self.step) / 2
            if same:
                continue                             # уже на нужном курсе
            plans.append({"offer": me, "leader": leader, "value": value, "show": show,
                          "floating": self.floating, "jump": float(t["jump"]), "reset": t["reset"]})
        return plans

    async def apply(self, p, my_ids):
        """Применить одну правку: проверки аномалии/частоты, затем сменить курс."""
        me = p["offer"]
        oid = me["id"]
        unit = "п.п." if p["floating"] else "%"
        if not p["reset"] and not O.allowed_jump(p["jump"], self.max_jump):
            self.log(f"[{self.key}] #O{oid}: скачок {p['jump']:.2f}{unit} > лимита {self.max_jump}{unit} — "
                     f"не меняю (аномалия в стакане?). Поменял бы на {p['show']}")
            return False
        if not self.cooldown.allow(oid):
            self.log(f"[{self.key}] #O{oid}: слишком часто меняю — пауза, пропускаю")
            return False
        try:
            if self.trader_mode == "chat" and self.chat is not None:
                await self.chat.set_price(oid, self._names, me["p"], p["value"], p["floating"])
            else:
                if oid not in my_ids:
                    self.log(f"[{self.key}] #O{oid} не принадлежит перебивающему аккаунту — не меняю")
                    return False
                await self.trader.set_price(oid, p["value"], p["floating"])
        except FlowError as e:
            self.log(f"[{self.key}] #O{oid}: диалог с CryptoBot не задался — {str(e)[:200]}")
            return False
        except Exception as e:
            self.log(f"[{self.key}] #O{oid}: не поменял курс — {type(e).__name__}: {str(e)[:200]}")
            return False
        self.cooldown.record(oid)
        tail = " (возврат к исходному — край коридора)" if p["reset"] else f" (выше был {p['leader']['n']} {p['leader']['p']})"
        self.log(f"[{self.key}] #O{oid}: курс {me['p']} → {p['show']}{tail}")
        return True

    async def step_once(self):
        """Один проход: прочитать стакан и, если нужно, перебить."""
        await self._ensure_meta()
        book = await self._read_book()
        my_ids = await self._my_ids()
        changed = 0
        for p in self.plan(book, my_ids):
            if await self.apply(p, my_ids):
                changed += 1
        return changed

    async def run(self, stop_event=None):
        self.log(f"[{self.key}] перебивка запущена: {self.asset}/{self.fiat} {self.side}, "
                 f"опрос каждые {self.poll:.0f} с, режим смены курса — {self.trader_mode}")
        while stop_event is None or not stop_event.is_set():
            try:
                await self.step_once()
            except Exception as e:
                self.log(f"[{self.key}] ошибка прохода: {type(e).__name__}: {str(e)[:200]}")
            await asyncio.sleep(self.poll)


def _split_pair(key):
    """'USDT/RUB' → ('USDT', 'RUB'). Можно переопределить явными asset/fiat в конфиге."""
    asset, _, fiat = key.partition("/")
    return asset.strip().upper(), fiat.strip().upper()


async def build(market_key, cfg, log=print):
    """Собрать P2POutbidder из конфига рынка: поднять клиентов наблюдателя и перебивающего."""
    asset, fiat = _split_pair(market_key)
    cfg = dict(cfg)
    cfg.setdefault("asset", asset)
    cfg.setdefault("fiat", fiat)

    obs_session = cfg.get("observer") or cfg.get("session")
    trader_session = cfg.get("trader") or cfg.get("session") or obs_session
    if not obs_session:
        raise ValueError(f"{market_key}: не задан observer/session в настройках рынка")

    obs_tg = await accounts.connect_authorized(obs_session)
    observer = CryptoBotApp(obs_tg, BOT, proxy=accounts.proxy_url(obs_session))

    trader = chat = None
    if cfg.get("trader_mode", "app") == "chat":
        trader_tg = obs_tg if trader_session == obs_session else await accounts.connect_authorized(trader_session)
        chat = ChatTrader(trader_tg, log=log)
    else:
        if trader_session == obs_session:
            trader = observer
        else:
            trader_tg = await accounts.connect_authorized(trader_session)
            trader = CryptoBotApp(trader_tg, BOT, proxy=accounts.proxy_url(trader_session))
    return P2POutbidder(market_key, cfg, observer, trader=trader, chat=chat, log=log)
