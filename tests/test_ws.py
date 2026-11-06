"""Разбор сообщений WebSocket-стакана xRocket: снимок, дельты, size 0 = убрать уровень."""
from decimal import Decimal

from autobid.xrocket import ws


def test_apply_levels_add_update_remove():
    m = {}
    ws.apply_levels(m, [["1.10", "5"], ["1.11", "3"]])
    assert m == {Decimal("1.10"): Decimal("5"), Decimal("1.11"): Decimal("3")}
    ws.apply_levels(m, [["1.10", "7"]])                 # обновление объёма
    assert m[Decimal("1.10")] == Decimal("7")
    ws.apply_levels(m, [["1.11", "0"]])                 # size 0 — удалить уровень
    assert Decimal("1.11") not in m


def test_book_from_maps_sorts_best_first():
    bids = {Decimal("1.08"): Decimal("1"), Decimal("1.10"): Decimal("2"), Decimal("1.09"): Decimal("1")}
    asks = {Decimal("1.13"): Decimal("1"), Decimal("1.11"): Decimal("2"), Decimal("1.12"): Decimal("1")}
    b = ws.book_from_maps(bids, asks)
    assert [p for p, _ in b["bids"]] == [Decimal("1.10"), Decimal("1.09"), Decimal("1.08")]
    assert [p for p, _ in b["asks"]] == [Decimal("1.11"), Decimal("1.12"), Decimal("1.13")]


def test_book_from_maps_depth():
    bids = {Decimal(str(1 + i / 100)): Decimal("1") for i in range(5)}
    b = ws.book_from_maps(bids, {}, depth=2)
    assert [p for p, _ in b["bids"]] == [Decimal("1.04"), Decimal("1.03")]


def test_stream_snapshot_then_delta():
    s = ws.OrderBookStream("GRAM-USDT")
    assert not s.ready
    s.handle({"result": {"sequence": 1,
                         "bids": [["1.10", "5"], ["1.09", "4"]],
                         "asks": [["1.12", "3"], ["1.13", "2"]]}})
    assert s.ready
    b = s.book()
    assert b["bids"][0] == (Decimal("1.10"), Decimal("5"))
    assert b["asks"][0] == (Decimal("1.12"), Decimal("3"))
    assert b["sequence"] == 1

    # дельта: новый лучший бид, лучший аск ушёл (size 0)
    s.handle({"method": "subscription",
              "params": {"data": {"snapshot": False, "sequence": 2,
                                  "bids": [["1.11", "1"]], "asks": [["1.12", "0"]]}}})
    b = s.book()
    assert b["bids"][0] == (Decimal("1.11"), Decimal("1"))
    assert b["asks"][0] == (Decimal("1.13"), Decimal("2"))
    assert b["sequence"] == 2


def test_stream_snapshot_push_resets_book():
    s = ws.OrderBookStream("GRAM-USDT")
    s.handle({"result": {"bids": [["1.10", "5"]], "asks": [["1.20", "5"]]}})
    # повторный снимок (snapshot=True) полностью заменяет стакан, а не накапливается
    s.handle({"method": "subscription",
              "params": {"data": {"snapshot": True, "sequence": 9,
                                  "bids": [["2.00", "1"]], "asks": [["2.10", "1"]]}}})
    b = s.book()
    assert b["bids"] == [(Decimal("2.00"), Decimal("1"))]
    assert b["asks"] == [(Decimal("2.10"), Decimal("1"))]


def test_stream_handle_ignores_unknown():
    s = ws.OrderBookStream("GRAM-USDT")
    assert s.handle({"id": "ping", "result": {"pong": True}}) is False   # нет bids/asks — не снимок
    assert s.handle("not a dict") is False
    assert not s.ready
