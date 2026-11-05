"""
Торговля монетами на бирже CryptoBot с автоперебивкой — перенос логики из old_bot_p2p
(plugins/refresh/refreshExchangeNew.py, utils/handelPurchase.py).

У биржи CryptoBot нет публичного API, поэтому, как и старый бот, работаем кликами по кнопкам в чате
с @CryptoBot: заходим в /exchange, выбираем пару, читаем стакан (ордера-кнопки с 🟢/🔴), находим свой
ордер (🔸) и встаём на шаг «лучше» ближайшего конкурента в пределах коридора. После исполнения ордера
(«Order #… executed successfully») режим «Покупка» переключается на «Продажу», «Продажа» — останавливает.

⚠️  ПОЗИЦИИ КНОПОК (NAV, BTN_*) взяты из СБОРКИ старого бота и соответствуют ТОЙ версии интерфейса
    CryptoBot. Текущий CryptoBot мог поменять раскладку — перед боевым запуском проверь и, если надо,
    перепиши позиции по свежей записи: `python record_chat.py test cryptobot -i`, пройди /exchange и
    посмотри номера кнопок в записи (chat.txt). Решение (decide) от позиций не зависит и тестируется.
"""
import asyncio
import re

from decimal import Decimal

from .. import accounts, settings
from . import outbid as O

BOT = "@CryptoBot"
MINE = "🔸"                      # маркер своего ордера в стакане
SYMBOL = {"buy": "🟢", "sell": "🔴"}

# --- позиции кнопок из old_bot_p2p (проверить по свежей записи!) ---
NAV = [(2, 0), (1, 0), (0, 0), (15, 0)]     # /exchange → … → экран выбора пары
TAB = {"buy": (0, 0), "sell": (0, 1)}       # вкладка «Купить» / «Продать» на экране стакана
BTN_PLACE = (14, 1)                         # выставить новый ордер (своего ещё нет)
BTN_CANCEL = (14, 2)                        # отменить/переставить свой ордер
BTN_AMOUNT_FIELD = (0, 1)                   # поле ввода цены
BTN_CONFIRM = (0, 0)                        # подтвердить
BTN_OK = (1, 0)
BTN_BACK = (-1, 0)
CANCEL_SEQ = [BTN_CANCEL, BTN_OK, BTN_OK]   # отменить свой ордер (из refreshExchangeNew)

ORDER_DONE = re.compile(
    r"Order\s+#(\w+)\s+to\s+(buy|sell)\s+🪙\s+([\d.]+)\s+(\w+)\s+for\s+🪙\s+([\d.]+)\s+(\w+)\s+executed successfully\.")

_NUM = re.compile(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?")


def first_num(line):
    """Первое число в строке ордера: '🟢 1,234.56 USDT · …' → Decimal('1234.56'). None, если чисел нет."""
    m = _NUM.search(line or "")
    return Decimal(m.group(0).replace(",", "")) if m else None


def decide(lines, side, limit, step, difference=None):
    """Чистое решение по стакану (перенос do_after_delay из refreshExchangeNew).

    lines — тексты кнопок-ордеров сверху вниз; ордера этой стороны помечены 🟢 (buy) / 🔴 (sell),
    а СВОЙ ордер — тем же значком плюс 🔸. Для 'sell' стакан читаем снизу вверх (лучшая цена — большая).
    Берём первый ордер в коридоре (как старый бот):
      • он мой → я лучший: {"action":"hold"} (или "replace", если сильно оторвался от следующего — difference);
      • он чужой → перебиваем: {"action":"place"|"replace", "value": Decimal} (place, если своего ещё нет);
    если в коридоре перебивать некого — {"action":"none"}."""
    sym = SYMBOL[side]
    ordered = list(reversed(lines)) if side == "sell" else list(lines)
    book = [l for l in ordered if sym in l]            # ордера этой стороны (и мои, и чужие)
    mine_present = any(MINE in l for l in book)

    for idx, line in enumerate(book):
        price = first_num(line)
        if price is None:
            continue
        if MINE in line:                               # первый в коридоре — мой: я лучший
            if O.exchange_target(price, step, side, limit) is None:
                continue                               # мой за коридором — ищем, кого перебить ниже
            if difference in (None, ""):
                return {"action": "hold"}
            nxt = next((first_num(book[j]) for j in range(idx + 1, len(book)) if MINE not in book[j]), None)
            if nxt is not None and abs(price - nxt) > O.to_dec(difference):
                target = O.exchange_target(nxt, step, side, limit)
                if target is not None:
                    return {"action": "replace", "value": target}
            return {"action": "hold"}
        target = O.exchange_target(price, step, side, limit)   # первый в коридоре — чужой: перебиваем
        if target is None:
            continue
        return {"action": "replace" if mine_present else "place", "value": target}
    return {"action": "none"}


class ExchangeTrader:
    """Одна пара на бирже CryptoBot. cfg: pair, limit, step, amount, mode('Buy'/'Sell'), sell_limit,
    difference, poll_sec (см. config.example.py)."""

    def __init__(self, tg_client, cfg, log=print):
        self.tg = tg_client
        self.cfg = dict(cfg)
        self.pair = cfg["pair"]
        self.limit = O.to_dec(cfg["limit"])
        self.step = O.to_dec(cfg["step"])
        self.amount = str(cfg.get("amount", "")).strip()
        self.mode = cfg.get("mode", "Buy")          # 'Buy' / 'Sell'
        self.sell_limit = cfg.get("sell_limit")
        self.difference = cfg.get("difference")
        self.poll = float(cfg.get("poll_sec", 10))
        self.log = log
        self._filled = asyncio.Event()
        self._last_fill = None

    @property
    def side(self):
        return "buy" if self.mode.lower().startswith("b") else "sell"

    # --- примитивы работы с кнопками (порт из old_bot_p2p/plugins/clickButton.py) ---
    async def _msg(self):
        res = await self.tg.get_messages(BOT, limit=1)
        return res[0] if res else None

    async def _click_pos(self, positions, timeout=10):
        for row, col in positions:
            end = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < end:
                msg = await self._msg()
                if msg and msg.buttons and row < len(msg.buttons) and col < len(msg.buttons[row]):
                    try:
                        await msg.buttons[row][col].click()
                        await asyncio.sleep(0.5)
                        break
                    except Exception:
                        await asyncio.sleep(0.5)
                else:
                    await asyncio.sleep(0.5)

    async def _click_text(self, substr, timeout=10):
        end = asyncio.get_event_loop().time() + timeout
        want = substr.lower()
        while asyncio.get_event_loop().time() < end:
            msg = await self._msg()
            for row in (msg.buttons or []) if msg else []:
                for b in row:
                    if b.text and want in b.text.lower():
                        try:
                            await b.click()
                            await asyncio.sleep(0.5)
                            return True
                        except Exception:
                            await asyncio.sleep(0.5)
            await asyncio.sleep(0.5)
        return False

    async def _stack(self):
        """Строки стакана: тексты кнопок последнего сообщения, где есть ордера (🟢/🔴/🔸)."""
        msg = await self._msg()
        lines = [b.text for row in (msg.buttons or []) if msg for b in row if b.text]
        return [l for l in lines if any(s in l for s in ("🟢", "🔴", MINE))]

    async def _open(self):
        await self.tg.send_message(BOT, "/exchange")
        await asyncio.sleep(1.0)
        await self._click_pos(NAV)
        if not await self._click_text(self.pair):
            raise RuntimeError(f"не нашёл пару {self.pair} на бирже (проверь позиции NAV по свежей записи)")
        await asyncio.sleep(0.5)

    async def _place(self, value):
        await self._click_pos([BTN_PLACE, TAB[self.side], BTN_AMOUNT_FIELD])
        await asyncio.sleep(0.4)
        await self.tg.send_message(BOT, str(value))
        await asyncio.sleep(0.4)
        await self._click_pos([BTN_CONFIRM])
        if self.amount:
            await self.tg.send_message(BOT, self.amount)
        else:
            await self._click_pos([BTN_OK])
        await self._click_pos([BTN_CONFIRM])

    async def _replace(self, value):
        await self._click_pos(CANCEL_SEQ)
        await self._place(value)

    async def step_once(self):
        await self._open()
        lines = await self._stack()
        d = decide(lines, self.side, self.limit, self.step, self.difference)
        if d["action"] == "place":
            self.log(f"[{self.pair}] выставляю {self.mode} по {d['value']}")
            await self._place(d["value"])
        elif d["action"] == "replace":
            self.log(f"[{self.pair}] переставляю {self.mode} на {d['value']}")
            await self._replace(d["value"])
        return d["action"]

    def _on_fill(self):
        from telethon import events
        @self.tg.on(events.NewMessage(chats=BOT))
        async def handler(event):
            m = ORDER_DONE.search(event.raw_text or "")
            if m:
                self._last_fill = {"id": m.group(1), "side": m.group(2)}
                self._filled.set()

    async def run(self, stop_event=None):
        self._on_fill()
        self.log(f"[{self.pair}] биржа запущена: {self.mode}, лимит {self.limit}, шаг {self.step}, "
                 f"опрос каждые {self.poll:.0f} с")
        while stop_event is None or not stop_event.is_set():
            if self._filled.is_set():
                self._filled.clear()
                side = (self._last_fill or {}).get("side")
                if side == "buy" and self.sell_limit:         # купили — переключаемся на продажу
                    self.mode, self.limit = "Sell", O.to_dec(self.sell_limit)
                    self.log(f"[{self.pair}] ордер исполнен (buy) → перехожу на продажу, лимит {self.limit}")
                else:
                    self.log(f"[{self.pair}] ордер исполнен ({side}) — останавливаю пару")
                    return
            try:
                await self.step_once()
            except Exception as e:
                self.log(f"[{self.pair}] ошибка прохода: {type(e).__name__}: {str(e)[:200]}")
            await asyncio.sleep(self.poll)


async def build(pair_key, cfg, log=print):
    """Собрать ExchangeTrader из конфига рынка."""
    cfg = dict(cfg)
    cfg.setdefault("pair", pair_key)
    session = cfg.get("session") or cfg.get("trader")
    if not session:
        raise ValueError(f"{pair_key}: не задан session в настройках биржевого рынка")
    tg = await accounts.connect_authorized(session)
    return ExchangeTrader(tg, cfg, log=log)
