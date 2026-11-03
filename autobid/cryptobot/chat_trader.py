"""
Смена курса объявления через диалог в чате CryptoBot (кнопки нажимает Telethon).
Для CryptoBot это обычные нажатия кнопок — как с телефона; между шагами — человеческие паузы.

Диалог (записан record_flow.py на реальном аккаунте):
  /p2p → «🔋 Объявления · 2/8» → список объявлений (по 5 на страницу, кнопки «🔹 Покупка · 93,110 USDT · ₴40.32»)
  → карточка «🧾 Объявление #O<id>» → «Цена» → «Пришлите новую фиксированную цену…»
  → отправляем курс сообщением → бот присылает тот же экран с новой ценой → «Сохранить изменения ›» → карточка.
Плавающая цена — тот же путь, только CryptoBot просит процент от биржевого курса («-5.6%»).
В списке лимит не показан, поэтому кандидатов ищем по монете (и курсу), а нужное объявление
подтверждаем по номеру #O<id> на карточке (он совпадает с id объявления в API).
Монета в чате может называться иначе, чем в API: TON → «GRAM» — имена передаёт вызывающий.
"""
import asyncio
import random
import re
import time

BOT = "send"   # @CryptoBot

# Паузы «как у человека», сек
DELAY_CLICK = (1.2, 3.0)     # перед нажатием кнопки
DELAY_READ = (0.6, 1.5)      # «прочитать» новый экран
TYPE_SPEED = (0.18, 0.35)    # сек на символ при вводе курса (показываем «печатает...»)
STEP_TIMEOUT = 15            # ждать ответа бота на шаг, сек
MAX_LIST_PAGES = 5           # страниц в списке объявлений

BTN_ADS = r"^\W*Объявления\s*·"          # «🔋 Объявления · 2/8» в меню /p2p
BTN_BACK_TO_ADS = r"Назад к объявлениям"
BTN_PRICE = r"^\W*Цена\W*$"
BTN_FIXED = r"Фиксированная"
BTN_FLOATING = r"Плавающая"
BTN_SAVE = r"Сохранить изменения"

_PCT = r"([+\-−]?\d+(?:[.,]\d+)?)\s*%"


class FlowError(Exception):
    pass


def _nums(text):
    """Все числа из текста: '₴2,565.55' → 2565.55, '93,110 USDT' → 93110, '1 800 000' → 1800000."""
    text = re.sub(r'(?<=\d)[,\s  ](?=\d{3}\b)', '', text)
    return [float(v) for v in re.findall(r'\d+(?:\.\d+)?', text)]


def _has(nums, value, eps=1e-6):
    return any(abs(n - value) < eps for n in nums)


def _buttons(msg):
    return [b for row in (msg.buttons or []) for b in row] if msg else []


def _texts(msg):
    return [b.text for b in _buttons(msg)]


def _sig(msg):
    return (msg.id, msg.text, tuple(_texts(msg))) if msg else None


def _human(rng):
    return asyncio.sleep(random.uniform(*rng))


def _card_price(text):
    """Курс с карточки объявления: строка «Цена: 🪙 40.32 UAH»."""
    m = re.search(r'Цена\W*:?\W*([\d,\s  ]+(?:\.\d+)?)', text or "")
    n = _nums(m.group(1)) if m else []
    return n[0] if n else None


def _pct(s):
    """'-5.7' / '+4' / '−2.5' → float."""
    return float(s.replace('−', '-').replace(',', '.').replace('+', ''))


def _card_margin(text):
    """Процент плавающей цены с карточки: «Цена: 🪙 56.77 UAH · -5.7%»."""
    m = re.search(r'Цена[^\n]*·[^\d+\-−\n]*' + _PCT, text or "")
    return _pct(m.group(1)) if m else None


def _prompt_margin(text):
    """Процент на экране ввода: «Процент от биржевого курса: -5.7%»."""
    m = re.search(r'Процент от биржевого курса[^\d+\-−\n]*' + _PCT, text or "")
    return _pct(m.group(1)) if m else None


def format_margin(margin):
    """-5.6 → '-5.6', 4 → '+4' (как в примере CryptoBot «+4% или -2.5%»)."""
    s = f"{float(margin):+.2f}".rstrip('0').rstrip('.')
    return "0" if s in ("+0", "-0") else s


def _selected(button):
    """Выбранный вариант переключателя CryptoBot обрамляет точками: «· Плавающая ·»."""
    return button is not None and "·" in button.text


class ChatTrader:
    def __init__(self, tg_client, log=print):
        self.tg = tg_client
        self.log = log
        self.lock = asyncio.Lock()   # один диалог за раз

    async def _last(self):
        res = await self.tg.get_messages(BOT, limit=1)
        return res[0] if res else None

    async def _wait_change(self, before):
        deadline = time.monotonic() + STEP_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(0.7)
            msg = await self._last()
            if msg and _sig(msg) != before and not msg.out:
                return msg
        raise FlowError("бот не ответил за %d сек" % STEP_TIMEOUT)

    async def _click(self, msg, button):
        await _human(DELAY_CLICK)
        before = _sig(msg)
        await button.click()
        msg = await self._wait_change(before)
        await _human(DELAY_READ)
        return msg

    async def _send(self, msg, text, typing=False):
        await _human(DELAY_CLICK)
        if typing:
            async with self.tg.action(BOT, "typing"):
                await asyncio.sleep(sum(random.uniform(*TYPE_SPEED) for _ in text))
        before = _sig(msg)
        await self.tg.send_message(BOT, text)
        msg = await self._wait_change(before)
        await _human(DELAY_READ)
        return msg

    def _find(self, msg, pattern):
        rx = re.compile(pattern, re.I)
        return next((b for b in _buttons(msg) if rx.search(b.text)), None)

    async def _press(self, msg, pattern, what):
        b = self._find(msg, pattern)
        if not b:
            raise FlowError(f"нет кнопки «{what}». Кнопки на экране: {_texts(msg)}")
        return await self._click(msg, b)

    async def _open_ad(self, msg, offer_id, names, cur_price):
        """На экране списка объявлений найти и открыть карточку #O<offer_id>."""
        tag = f"#O{offer_id}"
        seen_pages = set()
        for _ in range(MAX_LIST_PAGES):
            page = next((b.text for b in _buttons(msg) if re.fullmatch(r'\W*·\s*\d+\s*·\W*', b.text)), "1")
            seen_pages.add(re.sub(r'\D', '', page))
            coin = [b.text for b in _buttons(msg)
                    if any(re.search(rf'\b{re.escape(n)}\b', b.text, re.I) for n in names)]
            # сначала с тем же курсом (у плавающей курс в списке округлён и плывёт — тогда все этой монеты)
            candidates = sorted(coin, key=lambda x: not _has(_nums(x), cur_price, eps=0.006))
            for text in candidates:
                b = next((x for x in _buttons(msg) if x.text == text), None)
                if not b:
                    continue
                msg = await self._click(msg, b)
                if tag in (msg.text or ""):
                    return msg
                msg = await self._press(msg, BTN_BACK_TO_ADS, "Назад к объявлениям")
            nxt = next((b for b in _buttons(msg)
                        if re.fullmatch(r'\s*\d+\s*', b.text) and b.text.strip() not in seen_pages), None)
            if not nxt:
                break
            msg = await self._click(msg, nxt)
        raise FlowError(f"не нашёл объявление {tag} ({'/'.join(names)} по {cur_price}) в «Объявления». "
                        f"Кнопки на экране: {_texts(msg)}")

    async def set_price(self, offer_id, names, cur_price, value, floating=False):
        """Пройти диалог и поменять курс объявления offer_id.
        names — как монета может называться в чате (["TON", "GRAM"]); cur_price — текущий курс (для поиска);
        value — новый курс ("45.68"), а у плавающей цены — отклонение от биржевого курса в % ("-5.6")."""
        value = str(value)
        if isinstance(names, str):
            names = [names]
        async with self.lock:
            t0 = time.monotonic()
            msg = await self._last()
            msg = await self._send(msg, "/p2p")
            msg = await self._press(msg, BTN_ADS, "🔋 Объявления")
            msg = await self._open_ad(msg, offer_id, names, cur_price)
            msg = await self._press(msg, BTN_PRICE, "Цена")

            # тип цены не переключаем: какой выбран у объявления, такой и должен быть
            want, kind = (BTN_FLOATING, "плавающая") if floating else (BTN_FIXED, "фиксированная")
            if not _selected(self._find(msg, want)):
                raise FlowError(f"у объявления не {kind} цена — менять не стал. Экран: {(msg.text or '')[:200]}")

            if floating:
                msg = await self._send(msg, f"{format_margin(value)}%", typing=True)
                got = _prompt_margin(msg.text)
                if got is None or abs(got - float(value)) > 1e-6 or not self._find(msg, BTN_SAVE):
                    raise FlowError(f"бот не принял процент {format_margin(value)}%: {(msg.text or '')[:300]}")
            else:
                msg = await self._send(msg, value, typing=True)
                if not _has(_nums(msg.text or ""), float(value)) or not self._find(msg, BTN_SAVE):
                    raise FlowError(f"бот не принял курс {value}: {(msg.text or '')[:300]}")

            msg = await self._press(msg, BTN_SAVE, "Сохранить изменения")
            saved = _card_margin(msg.text) if floating else _card_price(msg.text)
            if saved is None or abs(saved - float(value)) > 1e-6:
                raise FlowError(f"после сохранения на карточке {saved}, ждал {value}. "
                                f"Экран: {(msg.text or '')[:200]}")

            self.log(f"Диалог смены курса #O{offer_id}: {cur_price} → {value}{'%' if floating else ''} "
                     f"за {time.monotonic() - t0:.0f} сек")
            return msg.text or ""
