"""Чистая логика перебивки: сетка шага, коридор, решения для биржи и P2P (без сети и Telegram)."""
from decimal import Decimal

from autobid.cryptobot import outbid as O
from autobid.cryptobot import exchange as X
from autobid.cryptobot.p2p import P2POutbidder, CooldownGuard, is_mine


# ------------------------------------------------------------------ сетка и шаг
def test_step_and_beat():
    assert O.step_for(5) == Decimal("0.0001") and O.step_for(45) == Decimal("0.01")
    assert O.step_for(5, "0.5") == Decimal("0.5")                 # явный шаг
    # asc (buy-стакан): встать строго ниже конкурента; desc (sell): строго выше
    assert O.beat("100", "0.01", asc=True) == Decimal("99.99")
    assert O.beat("100", "0.01", asc=False) == Decimal("100.01")
    assert O.beat("100.005", "0.01", asc=True) == Decimal("100.00")


def test_jump_and_corridor():
    assert O.jump_pct(100, 102) == Decimal("2")
    assert O.within_corridor("102", "100", "2") and not O.within_corridor("103", "100", "2")
    assert O.within_corridor("999", "100", None)                  # без коридора всё можно
    assert O.allowed_jump("4", "5") and not O.allowed_jump("6", "5")


# ------------------------------------------------------------------ фиксированная цена
def test_fixed_target_beats_enemy():
    t = O.fixed_target(my_price="101", enemy_price="100", asc=True, step="0.01")
    assert t["value"] == Decimal("99.99") and not t["reset"]

def test_fixed_target_resets_at_corridor_edge():
    # перебить = уйти за коридор ±0.5% от исходного 101 → возвращаемся к исходному курсу
    t = O.fixed_target(my_price="100.2", enemy_price="100", asc=True, step="0.01",
                       initial="101", max_dev_pct="0.5")
    assert t["reset"] and t["value"] == Decimal("101.00")

def test_fixed_target_within_corridor_keeps_outbid():
    t = O.fixed_target(my_price="100.2", enemy_price="100", asc=True, step="0.01",
                       initial="101", max_dev_pct="5")
    assert not t["reset"] and t["value"] == Decimal("99.99")


# ------------------------------------------------------------------ биржа: цель и решение
def test_exchange_target_respects_limit():
    assert X.O.exchange_target("100", "0.01", "buy", limit="110") == Decimal("100.01")
    assert X.O.exchange_target("100", "0.01", "buy", limit="100.005") is None    # enemy+step >= limit
    assert X.O.exchange_target("100", "0.01", "sell", limit="90") == Decimal("99.99")
    assert X.O.exchange_target("100", "0.01", "sell", limit="99.999") is None    # enemy-step <= limit


def test_first_num():
    assert X.first_num("🟢 1,234.56 USDT · ₴40.32") == Decimal("1234.56")
    assert X.first_num("🔴 🔸 98.70 USDT") == Decimal("98.70")
    assert X.first_num("нет чисел") is None


def test_exchange_decide_buy():
    # покупка: лучший сверху. Я внизу (хуже всех) → переставить выше топ-конкурента
    lines = ["🟢 101.00 USDT", "🟢 100.50 USDT", "🟢 🔸 100.00 USDT"]
    d = X.decide(lines, "buy", limit=Decimal("110"), step=Decimal("0.01"))
    assert d["action"] == "replace" and d["value"] == Decimal("101.01")

    # своего ордера нет → place
    d2 = X.decide(["🟢 101.00 USDT", "🟢 100.50 USDT"], "buy", Decimal("110"), Decimal("0.01"))
    assert d2["action"] == "place" and d2["value"] == Decimal("101.01")

    # я уже первый → hold
    d3 = X.decide(["🟢 🔸 101.00 USDT", "🟢 100.50 USDT"], "buy", Decimal("110"), Decimal("0.01"))
    assert d3["action"] == "hold"

    # весь стакан за лимитом → некого перебивать
    d4 = X.decide(["🟢 120.00 USDT"], "buy", Decimal("110"), Decimal("0.01"))
    assert d4["action"] == "none"


def test_exchange_decide_sell_reads_reversed():
    # продажа: лучший — наибольшая цена; старый бот читает стакан снизу вверх
    lines = ["🔴 🔸 99.00 USDT", "🔴 99.50 USDT", "🔴 100.00 USDT"]
    d = X.decide(lines, "sell", limit=Decimal("90"), step=Decimal("0.01"))
    assert d["action"] == "replace" and d["value"] == Decimal("99.99")   # 100.00 - 0.01


def test_exchange_decide_difference_steps_down_when_far_ahead():
    # я первый, но сильно оторвался от следующего (разрыв 2 > difference 0.5) → опускаюсь ближе
    lines = ["🟢 🔸 105.00 USDT", "🟢 100.00 USDT"]
    d = X.decide(lines, "buy", limit=Decimal("110"), step=Decimal("0.01"), difference="0.5")
    assert d["action"] == "replace" and d["value"] == Decimal("100.01")
    # разрыв в пределах difference → держим
    lines2 = ["🟢 🔸 100.20 USDT", "🟢 100.00 USDT"]
    d2 = X.decide(lines2, "buy", limit=Decimal("110"), step=Decimal("0.01"), difference="0.5")
    assert d2["action"] == "hold"


# ------------------------------------------------------------------ P2P: план перебивки
def _p2p(**cfg):
    base = {"asset": "USDT", "fiat": "RUB", "side": "buy", "step": "0.01"}
    base.update(cfg)
    r = P2POutbidder("USDT/RUB", base, observer=None)
    r._names = ["USDT"]
    r._my_names = {"me"}
    return r


def off(id, n, p, lmin=500, ratio=0.0):
    return {"id": id, "n": n, "p": Decimal(str(p)), "lmin": lmin, "ratio": ratio, "available": True}


def test_is_mine_by_id_and_nick():
    assert is_mine(off(1, "me", 100), {"me"}, set())
    assert is_mine(off(2, "x", 100), set(), {2})
    assert not is_mine(off(3, "x", 100), {"me"}, {2})


def test_p2p_plan_outbids_leader_in_my_limit():
    r = _p2p()
    book = [off(1, "a", 100.00), off(2, "b", 100.50), off(9, "me", 101.00)]
    plans = r.plan(book, {9})
    assert len(plans) == 1 and plans[0]["value"] == "99.99" and plans[0]["offer"]["id"] == 9


def test_p2p_plan_skips_when_first_or_other_limit():
    r = _p2p()
    # я первый в своей лимитке — ничего не делаем
    assert r.plan([off(9, "me", 99.00), off(1, "a", 100.00)], {9}) == []
    # конкурент в ДРУГОЙ лимитке — не считается
    assert r.plan([off(1, "a", 100.00, lmin=1000), off(9, "me", 101.00, lmin=500)], {9}) == []


def test_p2p_plan_corridor_reset():
    r = _p2p(initial="101", max_dev_pct="0.5")
    book = [off(1, "a", 100.00), off(9, "me", 100.20)]
    plans = r.plan(book, {9})
    assert plans and plans[0]["reset"] and plans[0]["value"] == "101.00"


# ------------------------------------------------------------------ защита от частых правок
def test_cooldown_guard():
    g = CooldownGuard(max_changes=2, cooldown_min=10)      # 2 правки, потом 10 мин
    assert g.allow("O1", now=0)
    g.record("O1", now=0); g.record("O1", now=1)
    assert not g.allow("O1", now=2)                        # исчерпал лимит, пауза ещё идёт
    assert g.allow("O1", now=0 + 10 * 60 + 2)              # пауза прошла


# ------------------------------------------------------------------ край коридора и пределы
def test_corridor_bound():
    # buy (вниз): нижняя граница ±1% от 100 → 99, округление вверх к сетке (внутри коридора)
    assert O.corridor_bound("100", "1", asc=True, step="0.01") == Decimal("99.00")
    # sell (вверх): верхняя граница → 101, округление вниз
    assert O.corridor_bound("100", "1", asc=False, step="0.01") == Decimal("101.00")


def test_clamp_price():
    assert O.clamp_price("98", "0.01", min_price="99", max_price="110") == Decimal("99.00")
    assert O.clamp_price("115", "0.01", min_price="99", max_price="110") == Decimal("110.00")
    assert O.clamp_price("105", "0.01", min_price="99", max_price="110") == Decimal("105.00")


def test_fixed_target_hold_at_border():
    # перебить = уйти за коридор ±0.5% от 101; on_limit='hold' → встать на край, а не к исходному
    t = O.fixed_target("100.2", "100", asc=True, step="0.01",
                       initial="101", max_dev_pct="0.5", on_limit="hold")
    assert t["reset"] and t["value"] == Decimal("100.50")      # 101*(1-0.005)=100.495 → вверх 100.50


def test_fixed_target_absolute_bounds():
    # цель 99.99, но min_price 100.20 → подтянуть к нижнему пределу
    t = O.fixed_target("101", "100", asc=True, step="0.01", min_price="100.20")
    assert t["value"] == Decimal("100.20")


def test_floating_target_hold_at_border():
    # плавающая, buy (вниз): коридор 2 п.п. от 95 → край 93; 'hold' встаёт на 93, не на 95
    t = O.floating_target("94", "92", asc=True, pct_step="0.01",
                          initial_ratio="95", max_dev_pp="2", on_limit="hold")
    assert t["reset"] and t["ratio"] == Decimal("93")


def test_exchange_target_absolute_bounds():
    assert O.exchange_target("100", "0.01", "buy", limit="110", max_price="100.005") is None
    assert O.exchange_target("100", "0.01", "sell", limit="90", min_price="99.995") is None


# ------------------------------------------------------------------ защита от «дёрганья»
def test_stability_guard():
    g = O.StabilityGuard(need=2)
    assert not g.confirm("O1", "100")          # первый раз видим — ждём
    assert g.confirm("O1", "100")              # держится второй опрос — реагируем
    assert not g.confirm("O1", "101")          # цена сменилась — счётчик заново
    assert g.confirm("O1", "101")
    g2 = O.StabilityGuard(need=1)
    assert g2.confirm("x", "5")                # need=1 — реагируем сразу (защита выключена)
