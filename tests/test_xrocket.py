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


def test_parse_problem():
    body = '{"type":"/x","title":"Client data validation error","status":400,"detail":"bad size"}'
    assert X.parse_problem(body) == "bad size"
    assert X.parse_problem("plain text error") == "plain text error"
    assert X.parse_problem("") == ""


def test_client_base_url_testnet_vs_prod():
    assert X.XRocketExchange("tok", testnet=True).base.startswith("https://exchange.api.testnet.")
    assert X.XRocketExchange("tok").base == X.BASE_PROD + X.API_PREFIX
