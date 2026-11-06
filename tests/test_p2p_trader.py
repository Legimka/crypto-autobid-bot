"""Режим смены курса P2P CryptoBot через API мини-приложения (trader_mode='app') — без сети.

Подставляем вместо CryptoBotApp фейк и проверяем, что бот: меняет курс своего объявления, пишет в
журнал сделок, не трогает чужие объявления и быстро реагирует на остановку."""
import asyncio
from decimal import Decimal

from autobid.cryptobot.p2p import P2POutbidder


class FakeApp:
    """Подставной CryptoBotApp: запоминает смены курса и отдаёт заданные свои id/стакан."""
    def __init__(self, my=(), book=None):
        self.changes = []
        self._my = set(my)
        self._book = book or []
        self.closed = False

    async def set_price(self, offer_id, value, floating=False):
        self.changes.append((offer_id, value, floating))
        return {"ok": True}

    async def my_offer_ids(self):
        return set(self._my)

    async def offers(self, asset, fiat, side, max_pages=5):
        return list(self._book)

    async def asset_names(self, asset):
        return [asset.upper()]

    async def close(self):
        self.closed = True


class FakeTradeLog:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)


def _p2p(trader, trade_log=None, **cfg):
    base = {"asset": "USDT", "fiat": "RUB", "side": "buy", "step": "0.01", "stability_polls": 1}
    base.update(cfg)
    r = P2POutbidder("USDT/RUB", base, observer=trader, trader=trader, log=lambda *a: None, trade_log=trade_log)
    r._names = ["USDT"]
    r._my_names = {"me"}
    return r


def off(id, n, p, lmin=500):
    return {"id": id, "n": n, "p": Decimal(str(p)), "lmin": lmin, "ratio": 0.0, "available": True}


def _plan(me, leader, value="99.99", reset=False):
    return {"offer": me, "leader": leader, "value": value, "show": value,
            "floating": False, "jump": 0.2, "reset": reset}


def test_apply_app_mode_changes_price_and_logs():
    app = FakeApp(my={9})
    tl = FakeTradeLog()
    r = _p2p(app, trade_log=tl)
    ok = asyncio.run(r.apply(_plan(off(9, "me", 100.20), off(1, "a", 100.00)), my_ids={9}))
    assert ok and app.changes == [(9, "99.99", False)]
    assert r._changes[9] == 1
    assert tl.records and tl.records[0]["new"] == "99.99" and tl.records[0]["offer"] == 9


def test_apply_app_mode_refuses_foreign_offer():
    # объявление #5 не принадлежит перебивающему аккаунту (my_ids={9}) — курс не трогаем
    app = FakeApp(my={9})
    r = _p2p(app)
    ok = asyncio.run(r.apply(_plan(off(5, "x", 100.20), off(1, "a", 100.00)), my_ids={9}))
    assert ok is False and app.changes == []


def test_apply_respects_max_changes():
    app = FakeApp(my={9})
    r = _p2p(app, max_changes=1)
    p = _plan(off(9, "me", 100.20), off(1, "a", 100.00))
    assert asyncio.run(r.apply(p, {9})) is True
    assert asyncio.run(r.apply(p, {9})) is False      # предел перебиваний исчерпан
    assert app.changes == [(9, "99.99", False)]


def test_step_once_outbids_via_app():
    # стакан: конкурент 100.00 и наше 101.00 в одной лимитке → ставим 99.99 через API
    book = [off(1, "a", 100.00), off(9, "me", 101.00)]
    app = FakeApp(my={9}, book=book)
    r = _p2p(app)
    changed = asyncio.run(r.step_once())
    assert changed == 1 and app.changes == [(9, "99.99", False)]


def test_run_stops_promptly():
    # poll большой, но остановка должна прервать сон почти сразу
    app = FakeApp(my={9}, book=[])
    r = _p2p(app, poll_sec=30)
    stop = asyncio.Event()

    async def drive():
        task = asyncio.ensure_future(r.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2)      # не должен ждать 30 с

    asyncio.run(drive())
