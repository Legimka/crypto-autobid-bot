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


def corridor_bound(initial, max_dev_pct, asc, step):
    """Край коридора на стороне движения: самая агрессивная цена, ещё НЕ выходящая за ±max_dev_pct
    от исходного курса. asc=True (перебиваем вниз) → нижняя граница, округлённая ВВЕРХ к сетке
    (чтобы остаться внутри коридора); asc=False (вверх) → верхняя граница, округлённая ВНИЗ."""
    st, init, dev = to_dec(step), to_dec(initial), to_dec(max_dev_pct) / 100
    if asc:
        return ((init * (1 - dev)) / st).to_integral_value(rounding=ROUND_CEILING) * st
    return ((init * (1 + dev)) / st).to_integral_value(rounding=ROUND_FLOOR) * st


def mid_price(best_bid, best_ask):
    """Опора плавающего коридора — «биржевой курс» (ТЗ §6): середина спреда (best_bid+best_ask)/2.
    Если одна сторона пуста — берём доступную; если стакан пуст — None."""
    b = to_dec(best_bid) if best_bid is not None else None
    a = to_dec(best_ask) if best_ask is not None else None
    if b is not None and a is not None:
        return (b + a) / 2
    return b if b is not None else a


def clamp_price(value, step, min_price=None, max_price=None):
    """Загнать курс в [min_price, max_price] (абсолютные пределы из ТЗ §4), округляя к сетке так,
    чтобы остаться внутри: снизу — вверх, сверху — вниз. None-предел не ограничивает."""
    st, value = to_dec(step), to_dec(value)
    if min_price is not None and value < to_dec(min_price):
        value = (to_dec(min_price) / st).to_integral_value(rounding=ROUND_CEILING) * st
    if max_price is not None and value > to_dec(max_price):
        value = (to_dec(max_price) / st).to_integral_value(rounding=ROUND_FLOOR) * st
    return value


def fixed_target(my_price, enemy_price, asc, step=None, initial=None, max_dev_pct=None,
                 on_limit="reset", min_price=None, max_price=None):
    """Куда переставить фиксированную цену, чтобы встать на шаг выше enemy, с ограничителями.
    Возвращает {value: Decimal, jump: Decimal(%), reset: bool} или None, если результат ≤ 0.

    on_limit — что делать, когда перебить = выйти за коридор ±max_dev_pct от исходного курса:
      'reset' — вернуться к исходному курсу initial (как в тексте ТЗ);
      'hold'  — встать на край коридора (самая агрессивная цена внутри предела, место держим дольше).
    В обоих случаях reset=True (признак «упёрлись в край коридора»).
    min_price/max_price — абсолютные пределы курса (ТЗ §4), применяются всегда."""
    st = step_for(my_price, step)
    target = beat(enemy_price, st, asc).quantize(st)
    reset = False
    if initial and max_dev_pct and not within_corridor(target, initial, max_dev_pct):
        if on_limit == "hold":
            target = corridor_bound(initial, max_dev_pct, asc, st).quantize(st)
        else:
            target = to_dec(initial).quantize(st)   # на границе коридора — назад к исходному курсу
        reset = True
    target = clamp_price(target, st, min_price, max_price).quantize(st)
    if target <= 0:
        return None
    return {"value": target, "jump": jump_pct(my_price, target), "reset": reset}


def floating_target(my_ratio, enemy_ratio, asc, pct_step="0.01", initial_ratio=None, max_dev_pp=None,
                    on_limit="reset"):
    """Плавающая цена: ratio — процент от биржевого курса (94.3 = -5.7%). Встать на шаг лучше enemy_ratio.
    max_dev_pp — коридор в пунктах ratio. on_limit как у fixed_target: 'reset' — назад к initial_ratio,
    'hold' — встать на край коридора. Возвращает {ratio, margin (ratio-100), jump (п.п.), reset}."""
    st = to_dec(pct_step)
    ratio = beat(to_dec(enemy_ratio), st, asc)
    reset = False
    if initial_ratio and max_dev_pp and abs(ratio - to_dec(initial_ratio)) > to_dec(max_dev_pp):
        edge = to_dec(initial_ratio) + (to_dec(max_dev_pp) if not asc else -to_dec(max_dev_pp))
        ratio = edge if on_limit == "hold" else to_dec(initial_ratio)
        reset = True
    return {"ratio": ratio, "margin": ratio - 100, "jump": abs(ratio - to_dec(my_ratio)), "reset": reset}


def exchange_target(enemy_price, step, side, limit=None, min_price=None, max_price=None):
    """Биржа (как в old_bot_p2p): встать на шаг «лучше» конкурента.
    side='buy'  — выше (enemy + step), не выше коридора limit;
    side='sell' — ниже (enemy - step), не ниже коридора limit.
    min_price/max_price — абсолютные пределы (ТЗ §4). Возвращает Decimal или None, если результат
    выходит за коридор limit или за пределы min/max."""
    enemy, st = to_dec(enemy_price), to_dec(step)
    if side == "buy":
        target = enemy + st
        if limit is not None and target >= to_dec(limit):   # old_bot: price + step < limit
            return None
    else:
        target = enemy - st
        if limit is not None and target <= to_dec(limit):    # old_bot: price - step > limit
            return None
    if min_price is not None and target < to_dec(min_price):
        return None
    if max_price is not None and target > to_dec(max_price):
        return None
    return target


class AnchorSmoother:
    """Сглаживание опоры плавающего коридора (EMA), чтобы центр не дёргался от одной чужой заявки
    (ТЗ §7, «стабильность важнее скорости»). alpha в (0,1]: 1 — без сглаживания (сырой mid), меньше —
    плавнее и инертнее. None-значение update() игнорируется (пустой стакан), опора не меняется."""

    def __init__(self, alpha=1.0):
        self.alpha = to_dec(alpha)
        self._v = None

    def update(self, value):
        if value is None:
            return self._v
        v = to_dec(value)
        self._v = v if self._v is None else self._v + self.alpha * (v - self._v)
        return self._v

    @property
    def value(self):
        return self._v

    def reset(self):
        self._v = None


class StabilityGuard:
    """Защита от «дёрганья» (ТЗ §7): конкурента учитываем, только если его цена держится
    не меньше `need` опросов подряд. Чистая логика без времени — считаем по проходам.

    confirm(key, price) → True, когда по этому ключу (например, лидеру лимитки) цена `price`
    наблюдается уже need-й раз подряд. Сменилась цена или лидер — счётчик начинается заново."""

    def __init__(self, need=2):
        self.need = max(1, int(need))
        self._seen = {}   # key -> [last_price_str, count]

    def confirm(self, key, price):
        p = str(price)
        prev = self._seen.get(key)
        count = prev[1] + 1 if prev and prev[0] == p else 1
        self._seen[key] = [p, count]
        return count >= self.need

    def reset(self, key=None):
        if key is None:
            self._seen.clear()
        else:
            self._seen.pop(key, None)
