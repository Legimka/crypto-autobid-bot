"""
Мониторинг стаканов P2P CryptoBot: сигналы о перебивке, история сделок, смены курса у мерчантов,
смена своего курса по кнопке. Настройки — в config.py, описание — в README.md.
Запуск:  python stakan.py        (python stakan.py --clear — начать с пустой памяти объёмов)
"""
import asyncio
import json
import os
import random
import secrets
import sys
import time
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from telethon import TelegramClient, events, Button

from config import *   # noqa: F401,F403 — все настройки
import config
import accounts
from chat_trader import format_margin

VOL_MEM_FILE = "vol_mem.json"
STATS_FILE = "limits_stats.json"
SETTINGS_FILE = "settings.json"      # что заглушено командами /mute (переживает перезапуск)
ACTIVITY_LOG = "activity.log"
DEALS_LOG = "deals.log"
VOL_MEM_MAX_AGE = 900   # память объёмов старше 15 мин при запуске не берём — иначе всё, что продали
                        # пока бот стоял, покажется сделками одной пачкой
CONFIRM_WINDOW = 1200   # сек: столько ждём, что у мерчанта вырастет счётчик сделок (✅ подтверждена)
SIDE_RETRY = 300        # своих объявлений на рынке нет — раз в столько сек снова ищем, в каком они стакане


def _market(key, cfg):
    asset, fiat = key.upper().split("/")
    m = {"title": key.upper(), "asset": asset, "fiat": fiat, "limits": [], "side": None,
         "bot": "usdt" if asset == "USDT" else "coins"}
    m.update(cfg)
    m["auto_side"] = m["side"] is None
    return m


MARKETS = {k.upper(): _market(k, v) for k, v in config.MARKETS.items() if v.get("enabled", True)}

vol_mem = {c: {} for c in MARKETS}
limit_stats = {c: defaultdict(lambda: {"total_deals_volume": 0.0, "deals_count": 0, "merchants": defaultdict(float)})
               for c in MARKETS}
stats = {c: {"market": {}, "top": [], "my_stat": "нет твоих объявлений", "above_name": "—", "target": "---"}
         for c in MARKETS}
price_hist = {c: defaultdict(lambda: {"last": None, "changes": deque()}) for c in MARKETS}
outbid_state = {c: {} for c in MARKETS}   # [рынок][ключ моего объявления] = {my, lmin, leader, nearest, count}

deal_history = []      # [{curr, text, uid, orders, t, confirmed}]
pending_actions = {}   # токен кнопки -> {curr, offer_id, mode, created}
muted = set()
bots = {}              # "usdt"/"coins" -> SignalBot (только с токеном)
app_client = None      # CryptoBotApp перебивающего (TRADER_MODE="app")
chat_trader = None     # ChatTrader перебивающего (TRADER_MODE="chat")
observer_pool = None


def offer_key(d):
    """У одного мерчанта бывает несколько объявлений с одинаковым лимитом — без id их объёмы
    перезаписывали бы друг друга (ложные сделки)."""
    return f"{d.get('n')}|{d.get('lmin')}|{d.get('id')}"


def is_mine(d):
    return MY_NICKNAME.lower() in d['n'].lower()


# ===================== ЛОГИ И ФОРМАТ =====================
def log_to_file(message):
    with open(ACTIVITY_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%H:%M:%S}] {message}\n")


def fmt_amount(x):
    """Объём монеты: 6005.01 USDT, 0.0123 BTC."""
    return f"{x:.2f}" if x >= 100 else f"{x:.4f}" if x >= 1 else f"{x:.8f}".rstrip("0").rstrip(".")


def fmt_price(d):
    """Курс объявления; у плавающей — ещё процент от биржевого."""
    if d.get('ptype') == 'floating' and d.get('ratio'):
        return f"{d['p']} ({format_margin(d['ratio'] - 100)}%)"
    return f"{d['p']}"


# ===================== ЗАГЛУШЕННЫЕ РЫНКИ =====================
def load_settings():
    muted.clear()
    data = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    muted.update(x.upper() for x in data.get("muted", MUTED))


def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"muted": sorted(muted)}, f, ensure_ascii=False, indent=2)


def is_muted(curr):
    m = MARKETS[curr]
    return bool({curr, m["asset"], m["fiat"]} & muted)


# ===================== БОТЫ СИГНАЛОВ =====================
def bot_for(curr):
    """Бот рынка: указанный, а если у него нет токена — любой настроенный."""
    return bots.get(MARKETS[curr]["bot"]) or next(iter(bots.values()), None)


async def notify(curr, text, buttons=None):
    """Сигнал по рынку — в его бота, если рынок не заглушён."""
    bot = bot_for(curr)
    if bot and not is_muted(curr):
        await bot.send(text, buttons)


async def notify_all(text):
    """Служебное (сбой наблюдателя, CryptoBot не отвечает) — во все боты, в один чат — один раз."""
    sent = set()
    for bot in bots.values():
        if bot.chat_id and bot.chat_id not in sent:
            sent.add(bot.chat_id)
            await bot.send(text)


class SignalBot:
    HELP = ("Команды:\n"
            "/status — где тебя перебили и какой курс ставить\n"
            "/markets — рынки этого бота и что заглушено\n"
            "/mute X — не присылать сигналы по X (рынок BTC/UAH, монета BTC или валюта KZT)\n"
            "/unmute X — снова присылать (/unmute all — всё)\n"
            "/deals — последние сделки\n"
            "/prices — смены курса у мерчантов за 10 минут\n"
            "/clear [рынок|имя] — очистить запомненные объёмы")

    def __init__(self, key, token):
        self.key, self.token = key, token
        self.chat_file = f"notify_chat_{key}.json"
        self.chat_id = None
        self.client = None
        if os.path.exists(self.chat_file):
            try:
                with open(self.chat_file, encoding="utf-8") as f:
                    self.chat_id = json.load(f).get("chat_id")
            except Exception:
                pass

    def markets(self):
        return [c for c in MARKETS if bot_for(c) is self]

    def _save_chat(self):
        with open(self.chat_file, "w", encoding="utf-8") as f:
            json.dump({"chat_id": self.chat_id}, f)

    def allowed(self, event):
        if ADMIN_IDS:
            return event.sender_id in ADMIN_IDS
        return self.chat_id is None or event.chat_id == self.chat_id

    async def send(self, text, buttons=None):
        if not (self.client and self.chat_id):
            return
        try:
            await self.client.send_message(self.chat_id, text, parse_mode='html', buttons=buttons,
                                           link_preview=False)
        except Exception as e:
            log_to_file(f"Бот {self.key}: не удалось отправить сигнал: {e}")

    def _match(self, arg):
        """Что заглушать: рынок / монета / валюта этого бота."""
        a = arg.upper()
        if a in MARKETS or any(a in (MARKETS[c]["asset"], MARKETS[c]["fiat"]) for c in MARKETS):
            return a
        return None

    def markets_report(self):
        lines = [f"Рынки бота «{self.key}»:"]
        for c in self.markets():
            lines.append(f"{'🔕' if is_muted(c) else '🔔'} {c} — {stats[c]['my_stat']}")
        if muted:
            lines.append(f"\nЗаглушено: {', '.join(sorted(muted))}")
        return "\n".join(lines)

    async def start(self):
        self.client = TelegramClient(f"signal_bot_{self.key}", API_ID, API_HASH)
        await self.client.start(bot_token=self.token)

        @self.client.on(events.NewMessage(pattern=r'^/'))
        async def on_command(event):
            if not self.allowed(event):
                return
            parts = event.raw_text.split(maxsplit=1)
            cmd, arg = parts[0].split('@')[0].lower(), (parts[1].strip() if len(parts) > 1 else None)
            if cmd == '/start':
                self.chat_id = event.chat_id
                self._save_chat()
                await event.reply(f"Сигналы включены ({', '.join(self.markets()) or 'рынков нет'}).\n\n{self.HELP}")
                await event.reply(status_report(self.markets()), parse_mode='html')
            elif cmd == '/status':
                await event.reply(status_report(self.markets()), parse_mode='html')
            elif cmd == '/markets':
                await event.reply(self.markets_report())
            elif cmd in ('/mute', '/unmute'):
                if cmd == '/unmute' and arg and arg.lower() == 'all':
                    muted.clear()
                elif not arg or not self._match(arg):
                    return await event.reply(f"Не знаю «{arg or ''}». Рынки: {', '.join(MARKETS)}")
                elif cmd == '/mute':
                    muted.add(self._match(arg))
                else:
                    muted.discard(self._match(arg))
                save_settings()
                await event.reply(self.markets_report())
            elif cmd == '/deals':
                rows = [d["text"] for d in deal_history if d["curr"] in self.markets()][-20:]
                await event.reply("\n".join(rows) or "Сделок пока нет")
            elif cmd == '/prices':
                await event.reply(prices_report(self.markets()), parse_mode='html')
            elif cmd == '/clear':
                await event.reply(f"🧹 Очищено записей: {clear_vol_mem(arg)}")

        @self.client.on(events.CallbackQuery(pattern=rb'^ob:'))
        async def on_outbid_button(event):
            if not self.allowed(event) or event.chat_id != self.chat_id:
                return await event.answer("Нет доступа", alert=True)
            action = pending_actions.pop(event.data.decode()[3:], None)
            if not action or time.time() - action["created"] > ACTION_TTL:
                await event.answer("Сигнал устарел — дождись нового или /status", alert=True)
                return await event.edit(buttons=None)
            await event.answer("Меняю курс...")
            await event.edit(buttons=None)
            await event.reply(await apply_outbid(action), parse_mode='html')

    async def close(self):
        if self.client:
            await self.client.disconnect()


async def start_bots():
    for key, token in SIGNAL_BOTS.items():
        if token:
            bots[key] = SignalBot(key, token)
    if not bots:
        log_to_file("SIGNAL_BOTS без токенов — сигналы в Telegram отключены")
    for bot in bots.values():
        await bot.start()
        log_to_file(f"Бот сигналов {bot.key}: рынки {', '.join(bot.markets())}")


# ===================== ОТЧЁТЫ =====================
def clear_vol_mem(arg=None):
    """arg: None — всё, рынок — один рынок, иначе — мерчант по имени (во всех рынках)."""
    removed = 0
    if not arg:
        for c in vol_mem:
            removed += len(vol_mem[c])
            vol_mem[c].clear()
    elif arg.upper() in vol_mem:
        removed = len(vol_mem[arg.upper()])
        vol_mem[arg.upper()].clear()
    else:
        for c in vol_mem:
            for k in [k for k in vol_mem[c] if k.split('|')[0].lower() == arg.lower()]:
                del vol_mem[c][k]
                removed += 1
    save_vol_mem()
    return removed


def price_changes(curr, key):
    h = price_hist[curr].get(key)
    if not h:
        return 0
    cutoff = time.time() - PRICE_WINDOW
    while h["changes"] and h["changes"][0] < cutoff:
        h["changes"].popleft()
    return len(h["changes"])


def prices_report(markets):
    lines = [f"📈 Смены курса за {PRICE_WINDOW // 60} мин"]
    now = time.time()
    for c in markets:
        rows = []
        for key, h in price_hist[c].items():
            n = price_changes(c, key)
            if n:
                rows.append((n, key, h["last"], int((now - h["changes"][-1]) // 60)))
        rows.sort(reverse=True)
        lines.append(f"\n<b>{c}</b>" + ("" if rows else " — без изменений"))
        for n, key, p, ago in rows[:15]:
            name, lmin = key.split('|')[:2]
            lines.append(f"{name} ({lmin}) — {n} раз, курс {p}, посл. {ago} мин назад")
    return "\n".join(lines)


def status_report(markets):
    lines = ["📊 Статус"]
    for c in markets:
        lines.append(f"\n<b>{c}</b>{' 🔕' if is_muted(c) else ''}: {stats[c]['my_stat']}")
        for st in outbid_state[c].values():
            if st["leader"]:
                lines.append(outbid_text(c, st))
            else:
                lines.append(f"👑 лимит от {st['lmin']}: ты первый ({fmt_price(st['my'])})")
    return "\n".join(lines)


# ===================== КУРС ДЛЯ ПЕРЕБИВКИ =====================
def is_asc(curr):
    """Стакан «buy» (вкладка «Купить») — по возрастанию цены, «sell» — по убыванию."""
    return MARKETS[curr]["side"] == "buy"


def fixed_step(curr, price):
    """Шаг фиксированной цены. По рынкам: USDT/UAH, KZT, RUB ставят курс до 0.01; USD, EUR (курс около 1)
    и дешёвые монеты — до 0.0001. CryptoBot сам принимает до 8 знаков."""
    if MARKETS[curr].get("step"):
        return Decimal(str(MARKETS[curr]["step"]))
    return Decimal("0.0001") if price < 10 else Decimal("0.01")


def _beat(value, step, asc):
    """Ближайшее значение на сетке шага, которое строго лучше value: ниже (asc) или выше."""
    n = Decimal(str(value)) / step
    return ((n.to_integral_value(ROUND_CEILING) - 1) if asc else (n.to_integral_value(ROUND_FLOOR) + 1)) * step


def target_for(curr, my, enemy):
    """Что поставить, чтобы встать выше enemy. Фиксированная цена — курс, плавающая — процент от биржевого."""
    asc = is_asc(curr)
    if my.get('ptype') == 'floating' and my.get('ratio'):
        rate = my['p'] / my['ratio'] * 100          # биржевой курс, от которого считается плавающая цена
        x = enemy['ratio'] if enemy.get('ptype') == 'floating' and enemy.get('ratio') else enemy['p'] / rate * 100
        step = Decimal(str(MARKETS[curr].get("pct_step", 0.01)))
        ratio = _beat(round(x, 8), step, asc)
        margin = format_margin(ratio - 100)
        approx = rate * float(ratio) / 100
        approx = f"{approx:.2f}" if approx >= 10 else f"{approx:.4f}"
        return {"floating": True, "value": margin, "copy": f"{margin}%", "approx": approx,
                "show": f"{margin}% (≈{approx})", "jump": abs(float(ratio) - my['ratio'])}
    step = fixed_step(curr, my['p'])
    new = _beat(enemy['p'], step, asc).quantize(step)
    if new <= 0:
        return None
    return {"floating": False, "value": str(new), "copy": str(new), "show": str(new),
            "jump": abs(float(new) - my['p']) / my['p'] * 100}


def outbid_text(curr, st):
    leader, nearest, my = st["leader"], st["nearest"], st["my"]
    t_first = target_for(curr, my, leader)
    lines = [f"⚔️ <b>Перебили</b> — {curr}, лимит от {st['lmin']}",
             f"Первый в лимитке: {leader['n']} — {fmt_price(leader)}"]
    if nearest is not leader:
        lines.append(f"Прямо над тобой: {nearest['n']} — {fmt_price(nearest)} (всего выше: {st['count']})")
    lines.append(f"Твой курс: {fmt_price(my)}")
    if t_first:
        lines.append(f"Стать первым: <code>{t_first['copy']}</code>"
                     + (f" (≈{t_first['approx']})" if t_first['floating'] else ""))
    if nearest is not leader:
        t_next = target_for(curr, my, nearest)
        if t_next:
            lines.append(f"Подняться на одного: <code>{t_next['copy']}</code>")
    return "\n".join(lines)


def outbid_buttons(curr, st):
    if not (AUTO_OUTBID_BUTTONS and (app_client or chat_trader) and st["my"].get('id')):
        return None

    now = time.time()   # заодно чистим протухшие кнопки
    for k in [k for k, a in pending_actions.items() if now - a["created"] > ACTION_TTL]:
        del pending_actions[k]

    def make(mode, enemy, label):
        t = target_for(curr, st["my"], enemy)
        if not t:
            return None
        token = secrets.token_hex(6)
        pending_actions[token] = {"curr": curr, "offer_id": st["my"]['id'], "mode": mode, "created": now}
        return [Button.inline(f"{label} ({t['copy']})", f"ob:{token}".encode())]

    rows = [make("first", st["leader"], "✅ Да, встать первым")]
    if st["nearest"] is not st["leader"]:
        rows.append(make("next", st["nearest"], "⬆️ На одного"))
    return [r for r in rows if r] or None


async def read_book(curr, until_id=None):
    cfg = MARKETS[curr]
    obs = await observer_pool.ensure()
    return [d for d in await obs.offers(cfg["asset"], cfg["fiat"], cfg["side"], max_pages=MAX_PAGES_SEARCH,
                                        stop=lambda d: d['id'] == until_id) if d["available"]]


async def apply_outbid(action):
    """Нажата кнопка: перечитываем стакан (с момента сигнала всё могло сдвинуться),
    пересчитываем курс и меняем цену своего объявления."""
    curr, cfg = action["curr"], MARKETS[action["curr"]]
    try:
        market = await read_book(curr, action["offer_id"])
        my_i = next((i for i, d in enumerate(market) if d['id'] == action["offer_id"]), None)
        if my_i is None:
            return f"❌ Твоё объявление не найдено в стакане (снято или ниже {MAX_PAGES_SEARCH * 20}-го места)"
        my = market[my_i]
        enemies = [market[x] for x in range(my_i) if not is_mine(market[x]) and market[x]['lmin'] == my['lmin']]
        if not enemies:
            return f"👑 Ты уже первый в лимитке {my['lmin']} ({fmt_price(my)}) — курс не менял"
        ref = enemies[0] if action["mode"] == "first" else enemies[-1]
        t = target_for(curr, my, ref)
        if not t:
            return "❌ Не получилось посчитать курс"
        max_jump = cfg.get("max_jump", MAX_JUMP_PCT)
        if t["jump"] > max_jump:
            unit = "п.п." if t["floating"] else "%"
            return (f"⚠️ Не меняю: {fmt_price(my)} → {t['show']} — скачок {t['jump']:.2f}{unit}, "
                    f"больше лимита {max_jump}{unit}.\nЕсли всё верно — поменяй вручную: <code>{t['copy']}</code>")

        if TRADER_MODE == "chat":
            # своё ли объявление — проверится само: в «Объявлениях» перебивающего аккаунта чужого нет
            names = await (await observer_pool.ensure()).asset_names(cfg["asset"])
            await chat_trader.set_price(my['id'], names, my['p'], t["value"], t["floating"])
        else:
            if my['id'] not in await app_client.my_offer_ids():
                return (f"🧪 Объявление {my['n']} не принадлежит перебивающему аккаунту — курс не меняю.\n"
                        f"Поменял бы: {fmt_price(my)} → <code>{t['copy']}</code>")
            await app_client.set_price(my['id'], t["value"], t["floating"])
        log_to_file(f"[{curr}] Курс изменён по кнопке: {fmt_price(my)} → {t['show']} (выше был {ref['n']} {ref['p']})")

        # проверяем глазами наблюдателя, что курс в стакане действительно поменялся
        await asyncio.sleep(4)
        now = next((d for d in await read_book(curr, my['id']) if d['id'] == my['id']), None)
        seen = f" В стакане сейчас: {fmt_price(now)}." if now else ""
        return (f"✅ Курс изменён: {fmt_price(my)} → <b>{t['show']}</b> "
                f"(лимит {my['lmin']}, выше был {ref['n']} {fmt_price(ref)}).{seen}")
    except Exception as e:
        log_to_file(f"[{curr}] Ошибка смены курса: {e}")
        if type(e).__name__ == "FlowError":
            return f"❌ Диалог с CryptoBot пошёл не так — курс не изменён.\n{str(e)[:600]}"
        return f"❌ Не получилось поменять курс: {str(e)[:200]}"


async def check_outbid(curr, market):
    """Сигнал, когда в твоей лимитке кто-то стоит выше тебя. Соревнуемся только внутри своего
    МИНИМАЛЬНОГО лимита (в CryptoBot покупатели фильтруют по нему); максимальный лимит не важен."""
    mine = [(i, d) for i, d in enumerate(market) if is_mine(d)]
    s = stats[curr]
    if not mine:
        s["my_stat"], s["above_name"], s["target"] = "нет твоих объявлений", "—", "---"
        outbid_state[curr].clear()
        return

    s["my_stat"] = " / ".join(f"{fmt_price(d)} ({d['lmin']})" for _, d in mine)
    first = None
    seen = set()
    for my_i, my in mine:
        key = offer_key(my)
        seen.add(key)
        enemies = [market[x] for x in range(my_i) if not is_mine(market[x]) and market[x]['lmin'] == my['lmin']]
        leader = enemies[0] if enemies else None
        st = {"my": my, "lmin": my['lmin'], "leader": leader,
              "nearest": enemies[-1] if enemies else None, "count": len(enemies)}
        old = outbid_state[curr].get(key)
        outbid_state[curr][key] = st
        if leader:
            first = first or st
            # повторяем сигнал, только если сменился лидер, его курс или твой курс
            if not old or not old["leader"] or \
                    (old["leader"]['n'], old["leader"]['p'], old["my"]['p']) != (leader['n'], leader['p'], my['p']):
                await notify(curr, outbid_text(curr, st), outbid_buttons(curr, st))
        elif old and old["leader"]:
            await notify(curr, f"👑 Снова первый — {curr}, лимит от {my['lmin']} ({fmt_price(my)})")
    for k in [k for k in outbid_state[curr] if k not in seen]:   # снятые объявления
        del outbid_state[curr][k]

    if first:
        t = target_for(curr, first["my"], first["leader"])
        s["above_name"] = f"{first['leader']['n'][:16]} {first['leader']['p']}"
        s["target"] = t["copy"] if t else "---"
    else:
        s["above_name"], s["target"] = "👑 ты первый", "KEEP"


# ===================== СДЕЛКИ =====================
def is_deal(d, old_vol):
    """Объём уменьшился на сумму не меньше минимального лимита — это сделка (меньше лимита сделки не бывает;
    мелкие изменения — округления и правки объёма). Работает для любой монеты: BTC и USDT одинаково."""
    diff = old_vol - d['vol']
    if diff <= 0:
        return False
    return diff * d['p'] >= 0.95 * d['lmin'] if d.get('lmin') else diff > 1e-6


async def log_deal(curr, d, old_vol):
    diff = old_vol - d['vol']
    asset = MARKETS[curr]["asset"]
    text = f"💥 {datetime.now():%H:%M:%S} {curr} → {d['n']} | лимит {d['lmin']} | курс {d['p']} | -{fmt_amount(diff)} {asset}"
    deal_history.append({"curr": curr, "text": text, "uid": d.get('uid'), "orders": d.get('orders', 0),
                         "t": time.time(), "confirmed": False})
    del deal_history[:-50]

    st = limit_stats[curr][d['lmin']]
    st["total_deals_volume"] += diff
    st["deals_count"] += 1
    st["merchants"][d['n']] += diff

    with open(DEALS_LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {curr} | {d['n']} | лимит {d['lmin']} | курс {d['p']} | "
                f"-{fmt_amount(diff)} {asset} | объём {old_vol} → {d['vol']}\n")
    if NOTIFY_ALL_DEALS or is_mine(d):
        await notify(curr, f"💥 Сделка {curr}\n{d['n']} | лимит {d['lmin']} | курс {d['p']}\n-{fmt_amount(diff)} {asset}")


def confirm_deals(curr, market):
    """Счётчик сделок мерчанта растёт позже уменьшения объёма (когда сделка завершена) — помечаем ✅."""
    orders = {}
    for d in market:
        orders[d.get('uid')] = max(orders.get(d.get('uid'), 0), d.get('orders') or 0)
    now = time.time()
    for e in deal_history:
        if e["curr"] == curr and not e["confirmed"] and now - e["t"] < CONFIRM_WINDOW:
            if orders.get(e["uid"], 0) > e["orders"]:
                e["confirmed"] = True
                e["text"] += " ✅"
                log_to_file(f"[{curr}] Сделка подтверждена: {e['text']}")


# ===================== СОХРАНЕНИЕ =====================
def load_vol_mem():
    if not os.path.exists(VOL_MEM_FILE):
        return
    age = time.time() - os.path.getmtime(VOL_MEM_FILE)
    if age > VOL_MEM_MAX_AGE:
        log_to_file(f"Память объёмов устарела ({int(age // 60)} мин) — начинаю с чистой")
        return
    try:
        with open(VOL_MEM_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for curr in MARKETS:
            vol_mem[curr] = {k: float(v) for k, v in data.get(curr, {}).items() if k.count('|') == 2}
        log_to_file("✅ Загружена память объёмов")
    except Exception:
        pass


def save_vol_mem():
    try:
        with open(VOL_MEM_FILE, "w", encoding="utf-8") as f:
            json.dump(vol_mem, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def load_limit_stats():
    if not os.path.exists(STATS_FILE):
        return
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for curr in MARKETS:
            for lim, s in data.get(curr, {}).items():
                limit_stats[curr][int(lim)].update({
                    "total_deals_volume": s.get("total_deals_volume", 0),
                    "deals_count": s.get("deals_count", 0),
                    "merchants": defaultdict(float, s.get("merchants", {}))})
    except Exception:
        pass


def save_limit_stats():
    try:
        data = {curr: {str(lim): {"total_deals_volume": s["total_deals_volume"], "deals_count": s["deals_count"],
                                  "merchants": dict(s["merchants"])}
                       for lim, s in limit_stats[curr].items()} for curr in MARKETS}
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


async def autosave_job():
    while True:
        await asyncio.sleep(60)
        save_vol_mem()
        save_limit_stats()


# ===================== ДАШБОРД =====================
_last_draw = 0


def draw():
    global _last_draw
    if time.time() - _last_draw < 2:   # рынков много — не мигать экраном на каждый опрос
        return
    _last_draw = time.time()
    os.system('cls' if os.name == 'nt' else 'clear')
    w = 100
    print("💥" + " ИСТОРИЯ СДЕЛОК ".center(w - 4, "═") + "💥")
    for e in deal_history[-15:]:
        print(e["text"])
    if not deal_history:
        print("Пока сделок не обнаружено...")
    print(f"\n🌟 {MY_NICKNAME} | {datetime.now():%H:%M:%S} | наблюдатель: {observer_pool.name if observer_pool else '—'}"
          f" | заглушено: {', '.join(sorted(muted)) or 'ничего'}\n")

    print("⚡" + " ВЕРХ СТАКАНА (ТОП-3) ".center(w - 4, "═") + "⚡")
    print(f"{'РЫНОК':<9} {'МЕРЧАНТ':<22} {'КУРС':<20} {'ЛИМИТ':<10} {'ОБЪЁМ':<14} СМЕН/10м")
    for c in MARKETS:
        for m in stats[c]['top'][:3]:
            print(f"{c:<9} {m['n'][:22]:<22} {fmt_price(m)[:20]:<20} {m['lmin']:<10} "
                  f"{fmt_amount(m['vol']):<14} {price_changes(c, offer_key(m))}")
    print("\n🔥" + " ТВОИ ОБЪЯВЛЕНИЯ ".center(w - 4, "═") + "🔥")
    print(f"{'РЫНОК':<9} {'МОЙ КУРС (ЛИМИТ)':<34} {'ВЫШЕ МЕНЯ':<30} ЦЕЛЬ")
    for c in MARKETS:
        print(f"{c:<9} {stats[c]['my_stat'][:34]:<34} {stats[c]['above_name'][:30]:<30} {stats[c]['target']}"
              + (" 🔕" if is_muted(c) else ""))
    print("\n📍" + " ЛИДЕРЫ ПО ЛИМИТАМ ".center(w - 4, "═") + "📍")
    for c in MARKETS:
        if MARKETS[c]["limits"]:
            row = "  ".join(f"{l:,}→{stats[c]['market'].get(l, {}).get('p', '---')}" for l in MARKETS[c]["limits"])
            print(f" {c:<9} {row}")
    print("═" * w)


# ===================== ОПРОС СТАКАНА =====================
def track_prices(curr, market):
    now = time.time()
    for d in market:
        h = price_hist[curr][offer_key(d)]
        if h["last"] is not None and h["last"] != d['p'] and d.get('ptype') != 'floating':
            h["changes"].append(now)   # у плавающей курс плывёт сам — сменой считаем только смену процента
        elif d.get('ptype') == 'floating' and h.get("ratio") is not None and h["ratio"] != d.get('ratio'):
            h["changes"].append(now)
        h["last"], h["ratio"] = d['p'], d.get('ratio')


def update_market_stats(curr, market):
    stats[curr]['top'] = market[:3]
    for tl in MARKETS[curr]["limits"]:
        leader = next((d for d in market if d.get('lmin') == tl), None)
        stats[curr]["market"][tl] = {"p": leader['p'], "n": leader['n']} if leader else {"p": "---", "n": "—"}


class ObserverPool:
    """Наблюдатели по очереди: работает один, при сбоях — переключение на следующий по кругу."""

    def __init__(self, sessions):
        self.sessions = sessions
        self.i = -1
        self.app = None
        self.name = None
        self.fails = 0
        self.lock = asyncio.Lock()

    async def ensure(self):
        if self.app:
            return self.app
        from cryptobot_app import CryptoBotApp
        async with self.lock:
            if self.app:
                return self.app
            for _ in range(len(self.sessions)):
                self.i = (self.i + 1) % len(self.sessions)
                name = self.sessions[self.i]
                try:
                    app = CryptoBotApp(await accounts.get_tg(name), proxy=accounts.proxy_url(name))
                    await app.login()
                    self.app, self.name, self.fails = app, name, 0
                    log_to_file(f"👁 Наблюдатель: {name}" + (" (через прокси)" if accounts.proxy_url(name) else ""))
                    return app
                except Exception as e:
                    log_to_file(f"Наблюдатель {name} недоступен: {str(e)[:150]}")
            raise RuntimeError("все наблюдатели недоступны")

    def ok(self):
        self.fails = 0

    async def failed(self, err):
        self.fails += 1
        if self.fails >= OBSERVER_SWITCH_AFTER and self.app:
            old = self.name
            await self.app.close()
            self.app = None
            if len(self.sessions) > 1:
                await notify_all(f"🔁 Наблюдатель {old} сбоит ({str(err)[:80]}) — переключаюсь на запасного")
            log_to_file(f"Наблюдатель {old}: {self.fails} ошибок подряд — переподключение")

    async def close(self):
        if self.app:
            await self.app.close()


async def detect_side(pool, curr):
    """В каком стакане стоят твои объявления: объявления «Покупка» — в sell, «Продажа» — в buy."""
    cfg = MARKETS[curr]
    for side in ("sell", "buy"):
        book = await (await pool.ensure()).offers(cfg["asset"], cfg["fiat"], side,
                                                  max_pages=MAX_PAGES_SEARCH, stop=is_mine)
        if any(is_mine(d) for d in book):
            log_to_file(f"[{curr}] Твоё объявление найдено в стакане type={side}")
            return side, True
    return "sell", False


async def app_job(pool, curr):
    cfg = MARKETS[curr]
    poll = cfg.get("poll", POLL_SEC)
    fails = 0
    mine_seen, search_at, side_at = True, 0, 0
    await asyncio.sleep(random.uniform(0, 3))   # рынки стартуют вразнобой
    while True:
        try:
            # стакан: из настроек, иначе ищем свои объявления; не нашли — смотрим sell и время от времени ищем снова
            if cfg["side"] is None or (cfg["auto_side"] and side_at and not mine_seen and time.time() >= side_at):
                cfg["side"], found = await detect_side(pool, curr)
                side_at = 0 if found else time.time() + SIDE_RETRY
                if not found:
                    log_to_file(f"[{curr}] Твоих объявлений нет — смотрю type=sell, поищу снова через {SIDE_RETRY // 60} мин")

            app = await pool.ensure()
            # верх стакана — MAX_PAGES страниц; если твоё объявление ниже — листаем до него
            deep = mine_seen or time.time() >= search_at
            market = [d for d in await app.offers(cfg["asset"], cfg["fiat"], cfg["side"],
                                                  max_pages=MAX_PAGES_SEARCH if deep else MAX_PAGES,
                                                  min_pages=MAX_PAGES, stop=is_mine)
                      if d["available"]]
            mine_seen = any(is_mine(d) for d in market)
            if deep and not mine_seen:
                search_at = time.time() + SEARCH_RETRY
            fails = 0
            pool.ok()
            if market:
                update_market_stats(curr, market)
                track_prices(curr, market)
                await check_outbid(curr, market)
                confirm_deals(curr, market)
                for d in market:
                    key = offer_key(d)
                    old_vol = vol_mem[curr].get(key)
                    if old_vol is not None and is_deal(d, old_vol):
                        await log_deal(curr, d, old_vol)
                    vol_mem[curr][key] = d['vol']
            draw()
        except Exception as e:
            fails += 1
            log_to_file(f"[{curr}] ошибка #{fails}: {str(e)[:200]}")
            await pool.failed(e)
            if fails == 5:
                await notify_all(f"⚠️ {curr}: CryptoBot не отвечает 5 раз подряд ({str(e)[:100]})")
        # при ошибках (503/429) ждём дольше: 10, 20, 40... до 2 минут
        await asyncio.sleep(poll + random.uniform(0, 2) if not fails else min(120, poll * 2 ** fails))


async def start_trader():
    global app_client, chat_trader
    try:
        tg = await accounts.get_tg(TRADER_SESSION)
        if TRADER_MODE == "chat":
            from chat_trader import ChatTrader
            chat_trader = ChatTrader(tg, log=log_to_file)
        else:
            from cryptobot_app import CryptoBotApp
            app_client = CryptoBotApp(tg, proxy=accounts.proxy_url(TRADER_SESSION))
        print(f"✅ Перебивающий аккаунт: {TRADER_SESSION} (смена курса: {TRADER_MODE})")
    except Exception as e:
        print(f"⚠️ Перебивающий аккаунт недоступен ({e}) — кнопки смены курса отключены")
        await notify_all(f"⚠️ Перебивающий аккаунт {TRADER_SESSION} недоступен — кнопки смены курса отключены")


# ===================== MAIN =====================
async def main():
    global observer_pool
    if os.path.exists(ACTIVITY_LOG):
        os.replace(ACTIVITY_LOG, ACTIVITY_LOG + ".old")   # прошлый запуск — в activity.log.old
    load_settings()
    load_limit_stats()
    load_vol_mem()
    if "--clear" in sys.argv:
        clear_vol_mem()

    print(f"🌟 Запуск: рынков {len(MARKETS)} ({', '.join(MARKETS)})")
    await start_bots()
    observer_pool = ObserverPool(OBSERVER_SESSIONS)
    await observer_pool.ensure()
    print(f"✅ Наблюдатель {observer_pool.name} вошёл в приложение CryptoBot")
    await start_trader()

    try:
        await asyncio.gather(autosave_job(), *[app_job(observer_pool, c) for c in MARKETS])
    finally:
        await observer_pool.close()
        if app_client:
            await app_client.close()
        for bot in bots.values():
            await bot.close()
        save_vol_mem()
        save_limit_stats()
        print("Программа завершена.")


if __name__ == '__main__':
    # эмодзи в выводе: без этого на Windows-консолях с cp1251 бот падает на первом print
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
