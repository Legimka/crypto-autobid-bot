"""Чистые помощники клиента xRocket Exchange: символы, тело ордера, разбор стакана и ошибок."""
from decimal import Decimal

from autobid.xrocket import api as X


def test_symbol_roundtrip():
    assert X.symbol("ton", "usdt") == "TON-USDT"
    assert X.split_symbol("TON-USDT") == ("TON", "USDT")


def test_order_body_limit():
    b = X.order_body("TON-USDT", "buy", size="5", price="2.10")
    assert b == {"symbol": "TON-USDT", "side": "buy", "type": "limit",
                 "price": "2.10", "size": "5", "timeInForce": "GTC"}


def test_order_body_strings_and_optional():
    # числа приводятся к строкам; пустые поля не попадают; clientOrderId пробрасывается
    b = X.order_body("TON-USDT", "SELL", size=5, price=2.1, client_id="abc")
    assert b["side"] == "sell" and b["size"] == "5" and b["price"] == "2.1"
    assert b["clientOrderId"] == "abc" and "funds" not in b and "stopPrice" not in b


def test_order_body_market_has_no_tif_price():
    b = X.order_body("TON-USDT", "buy", funds="10", order_type="market", tif="IOC")
    assert b["type"] == "market" and b["funds"] == "10" and "price" not in b
    assert "timeInForce" not in b                      # tif только для limit/stopLimit


def test_parse_orderbook():
    raw = {"sequence": "42",
           "bids": [["51190.00", "0.45"], ["51185.00", "1.2"]],
           "asks": [["51200.00", "0.3"]]}
    ob = X.parse_orderbook(raw)
    assert ob["sequence"] == "42"
    assert ob["bids"][0] == (Decimal("51190.00"), Decimal("0.45"))
    assert ob["asks"][0] == (Decimal("51200.00"), Decimal("0.3"))


def test_parse_orderbook_empty():
    ob = X.parse_orderbook({})
    assert ob == {"bids": [], "asks": [], "sequence": None}


def test_parse_orderbook_normalizes_order():
    # REST отдаёт обе стороны по возрастанию — bids должны стать лучшими (выше) сверху
    raw = {"bids": [["1.073", "1"], ["1.079", "2"], ["1.095", "3"]],
           "asks": [["1.124", "1"], ["1.102", "2"], ["1.108", "3"]]}
    ob = X.parse_orderbook(raw)
    assert [p for p, _ in ob["bids"]] == [Decimal("1.095"), Decimal("1.079"), Decimal("1.073")]
    assert [p for p, _ in ob["asks"]] == [Decimal("1.102"), Decimal("1.108"), Decimal("1.124")]


def test_parse_problem():
    body = '{"type":"/x","title":"Client data validation error","status":400,"detail":"bad size"}'
    assert X.parse_problem(body) == "bad size"
    assert X.parse_problem("plain text error") == "plain text error"
    assert X.parse_problem("") == ""


def test_client_base_url_testnet_vs_prod():
    assert X.XRocketExchange("tok", testnet=True).base.startswith("https://exchange.api.testnet.")
    assert X.XRocketExchange("tok").base == X.BASE_PROD + X.API_PREFIX


# ------------------------------------------------------------------ логика перебивки стакана
from decimal import Decimal as D

from autobid.xrocket import logic as L


def _bids(*pairs):
    return [(D(str(p)), D(str(s))) for p, s in pairs]


def test_decide_buy_place_above_top():
    d = L.decide(_bids((1.07, 10), (1.06, 5)), _bids((1.10, 3)), "buy", step="0.01")
    assert d["action"] == "place" and d["price"] == D("1.08")


def test_decide_buy_hold_when_already_first_alone():
    # наш ордер один на лучшем уровне 1.08, следующий конкурент 1.07 → мы уже первые → hold
    d = L.decide(_bids((1.08, 5), (1.07, 10)), _bids((1.20, 1)), "buy",
                 step="0.01", my_price="1.08", my_size="5")
    assert d["action"] == "hold"


def test_decide_buy_replace_when_shared_level():
    # на нашей цене 1.08 ещё чужие (15-5=10) → встать на шаг выше: 1.09
    d = L.decide(_bids((1.08, 15),), _bids((1.20, 1)), "buy",
                 step="0.01", my_price="1.08", my_size="5")
    assert d["action"] == "replace" and d["price"] == D("1.09")


def test_decide_buy_no_cross_spread():
    # лучший конкурент 1.09, best ask 1.10, шаг 0.01 → перебить нельзя не пересекая спред → hold/none
    d = L.decide(_bids((1.09, 5),), _bids((1.10, 2)), "buy", step="0.01")
    assert d["action"] == "none"


def test_decide_sell_below_top_no_cross():
    # продажа: лучший ask-конкурент 1.11, best bid 1.07 → встаём 1.10
    d = L.decide(_bids((1.07, 5),), _bids((1.11, 4), (1.12, 9)), "sell", step="0.01")
    assert d["action"] == "place" and d["price"] == D("1.10")


def test_decide_buy_corridor_reset():
    # перебить = выйти за коридор ±1% от 1.05 (пол/потолок) → reset к 1.05
    d = L.decide(_bids((1.07, 5),), _bids((1.20, 1)), "buy",
                 step="0.01", initial="1.05", max_dev_pct="1")
    assert d["reset"] and d["price"] == D("1.05")


def test_decide_buy_corridor_hold_at_border():
    d = L.decide(_bids((1.07, 5),), _bids((1.20, 1)), "buy",
                 step="0.01", initial="1.05", max_dev_pct="1", on_limit="hold")
    # потолок коридора 1.05*1.01=1.0605 → вниз к сетке 1.06
    assert d["reset"] and d["price"] == D("1.06")
