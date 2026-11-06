"""
Чистая логика перебивки для биржевого стакана xRocket (bid/ask). Без сети — тестируется.

Реиспользует общую математику из autobid/cryptobot/outbid.py (коридор, сетка шага, пределы).
Отличие биржи от P2P — настоящий двусторонний стакан, поэтому здесь добавлено правило ТЗ §4:
«нельзя пересекать спред» (иначе лимитный ордер исполнится сразу как рыночный, с комиссией тейкера).

side='buy'  — наш ордер в bids, «лучше» = выше; ставим на шаг выше лучшего конкурента, но строго
              ниже лучшего ask (не пересекая спред);
side='sell' — наш ордер в asks, «лучше» = ниже; на шаг ниже лучшего конкурента, но строго выше
              лучшего bid.

Свой объём на своём ценовом уровне вычитается (ТЗ §3: «бот не должен перебивать сам себя»): если на
нашем уровне остаются ещё чужие — конкурент на той же цене (нужно встать на шаг лучше); если нет —
конкурент на следующем уровне.
"""
from ..cryptobot import outbid as O


def best_competitor(levels, my_price=None, my_size=None):
    """Лучшая цена конкурента из уровней [(price,size)] (лучший сверху). На своём уровне вычитаем
    свой объём; если он там один — уровень пропускаем. Возвращает (price, size) Decimal или None."""
    mine = O.to_dec(my_price) if my_price is not None else None
    for price, size in levels:
        price, size = O.to_dec(price), O.to_dec(size)
        if mine is not None and price == mine:
            rem = size - O.to_dec(my_size or 0)
            if rem > 0:
                return (price, rem)          # на нашей цене есть и чужие
            continue                         # только мы — смотрим следующий уровень
        return (price, size)
    return None


def decide(bids, asks, side, step, my_price=None, my_size=None,
           initial=None, max_dev_pct=None, on_limit="reset", min_price=None, max_price=None):
    """Куда поставить лимитный ордер. bids — лучшие (выше) сверху, asks — лучшие (ниже) сверху;
    каждый уровень (price, size). Возвращает {'action': 'place'|'replace'|'hold'|'none',
    'price': Decimal|None, 'reset': bool}. 'place' — своего ордера ещё нет, 'replace' — переставить."""
    st = O.to_dec(step)
    best_bid = O.to_dec(bids[0][0]) if bids else None
    best_ask = O.to_dec(asks[0][0]) if asks else None
    mine = my_price is not None
    hold_or_none = {"action": "hold" if mine else "none", "price": None, "reset": False}

    comp = best_competitor(bids if side == "buy" else asks, my_price, my_size)
    if comp is None:
        return hold_or_none
    comp_price = comp[0]

    if side == "buy":
        target = comp_price + st                         # на шаг выше конкурента
        if best_ask is not None:                         # не пересекать спред: строго ниже best ask
            target = min(target, best_ask - st)
        improved = target > comp_price
        asc_for_edge = False                             # «лучше» выше → край коридора сверху
    else:
        target = comp_price - st                         # на шаг ниже конкурента
        if best_bid is not None:
            target = max(target, best_bid + st)          # строго выше best bid
        improved = target < comp_price
        asc_for_edge = True

    reset = False
    if not improved:                                     # спред слишком узкий — встать лучше нельзя
        return hold_or_none
    if initial and max_dev_pct and not O.within_corridor(target, initial, max_dev_pct):
        if on_limit == "hold":
            target = O.corridor_bound(initial, max_dev_pct, asc_for_edge, st)
        else:                                            # reset / pause → значение исходного курса
            target = O.to_dec(initial)
        reset = True
    target = O.clamp_price(target, st, min_price, max_price)
    if target <= 0:
        return hold_or_none
    if mine and abs(target - O.to_dec(my_price)) < st / 2:
        return {"action": "hold", "price": target, "reset": reset}
    return {"action": "replace" if mine else "place", "price": target, "reset": reset}
