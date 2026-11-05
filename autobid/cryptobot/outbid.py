"""
Математика перебивки для CryptoBot — общая для P2P и биржи. Чистые функции: без сети и Telegram,
легко тестируются.

Идея перебивки: встать на один шаг лучше ближайшего конкурента, но с двумя ограничителями:
  • «коридор» — максимальное отклонение от ИСХОДНОГО курса. На границе коридора не продолжаем гонку,
    а возвращаемся к исходному курсу (как в ТЗ: «при достижении лимита вернуться к начальному курсу»);
  • max_jump — не менять курс сильнее, чем на столько за одну правку (защита от аномального чужого курса
    и опечаток в стакане).

Фиксированная цена — это сам курс. Плавающая — процент (ratio) от биржевого курса: 94.3 ≈ -5.7%.
Логика сетки шага и «beat» перенесена из cryptobot-p2p-bot/stakan.py; формулы биржи — из old_bot_p2p.
"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


def to_dec(x):
    return x if isinstance(x, Decimal) else Decimal(str(x))


def step_for(price, step=None):
    """Шаг сетки фиксированной цены. Явный step из настроек рынка, иначе 0.0001 для дешёвых монет
    (курс < 10, например USDT/USD около 1), иначе 0.01. CryptoBot принимает до 8 знаков."""
    if step:
        return to_dec(step)
    return Decimal("0.0001") if to_dec(price) < 10 else Decimal("0.01")


def beat(value, step, asc):
    """Ближайшее значение на сетке шага, строго лучшее, чем value:
    ниже при asc=True (стакан по возрастанию, лучшая цена — меньшая),
    выше при asc=False (стакан по убыванию, лучшая цена — большая)."""
    step = to_dec(step)
    n = to_dec(value) / step
    if asc:
        return (n.to_integral_value(rounding=ROUND_CEILING) - 1) * step
    return (n.to_integral_value(rounding=ROUND_FLOOR) + 1) * step


def jump_pct(old, new):
    """На сколько процентов new отличается от old (абсолютное значение)."""
    old = to_dec(old)
    if old == 0:
        return Decimal(0)
    return abs(to_dec(new) - old) / old * 100


def within_corridor(value, initial, max_dev_pct):
    """value внутри коридора ±max_dev_pct (%) от исходного курса initial? Без коридора — всегда True."""
    if not initial or not max_dev_pct:
        return True
    return jump_pct(initial, value) <= to_dec(max_dev_pct)


def allowed_jump(jump, max_jump):
    """Разрешена ли правка: скачок не больше max_jump. Без ограничения (None) — всегда разрешена."""
    return max_jump is None or to_dec(jump) <= to_dec(max_jump)


def fixed_target(my_price, enemy_price, asc, step=None, initial=None, max_dev_pct=None):
    """Куда переставить фиксированную цену, чтобы встать на шаг выше enemy.
    Возвращает {value: Decimal, jump: Decimal(%), reset: bool} или None, если результат ≤ 0.
    reset=True — встать выше конкурента значит выйти за коридор, поэтому вернулись к исходному курсу."""
    st = step_for(my_price, step)
    target = beat(enemy_price, st, asc).quantize(st)
    reset = False
    if initial and max_dev_pct and not within_corridor(target, initial, max_dev_pct):
        target = to_dec(initial).quantize(st)      # на границе коридора — назад к исходному курсу
        reset = True
    if target <= 0:
        return None
    return {"value": target, "jump": jump_pct(my_price, target), "reset": reset}


def floating_target(my_ratio, enemy_ratio, asc, pct_step="0.01", initial_ratio=None, max_dev_pp=None):
    """Плавающая цена: ratio — процент от биржевого курса (94.3 = -5.7%). Встать на шаг лучше enemy_ratio.
    max_dev_pp — коридор в пунктах ratio. Возвращает {ratio, margin (ratio-100), jump (п.п.), reset}."""
    st = to_dec(pct_step)
    ratio = beat(to_dec(enemy_ratio), st, asc)
    reset = False
    if initial_ratio and max_dev_pp and abs(ratio - to_dec(initial_ratio)) > to_dec(max_dev_pp):
        ratio = to_dec(initial_ratio)
        reset = True
    return {"ratio": ratio, "margin": ratio - 100, "jump": abs(ratio - to_dec(my_ratio)), "reset": reset}


def exchange_target(enemy_price, step, side, limit=None):
    """Биржа (как в old_bot_p2p): встать на шаг «лучше» конкурента.
    side='buy'  — выше (enemy + step), не выше коридора limit;
    side='sell' — ниже (enemy - step), не ниже коридора limit.
    Возвращает Decimal или None, если результат выходит за коридор limit."""
    enemy, st = to_dec(enemy_price), to_dec(step)
    if side == "buy":
        target = enemy + st
        if limit is not None and target >= to_dec(limit):   # old_bot: price + step < limit
            return None
        return target
    target = enemy - st
    if limit is not None and target <= to_dec(limit):        # old_bot: price - step > limit
        return None
    return target
