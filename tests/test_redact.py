import json

from autobid import redact

INIT = ("query_id=AAHdF6IQAAAAAN0XohDhrOrc&user=%7B%22id%22%3A279058397%7D&auth_date=1662771648"
        "&hash=c501b71e775f74ce10e377dea85a7ea24ecd640b223ea86dfe453e0eaed2e2b2")
INIT_ENC = INIT.replace("&", "%26").replace("=", "%3D")


def test_launch_url_hides_init_data_keeps_platform():
    url = ("https://app.cr.bot/#tgWebAppData=" + INIT_ENC +
           "&tgWebAppVersion=8.0&tgWebAppPlatform=android&tgWebAppThemeParams=%7B%22bg_color%22%3A%22%23fff%22%7D")
    out = redact.redact_url(url)
    assert "c501b71e" not in out and "279058397" not in out
    assert "tgWebAppData=***" in out
    assert "tgWebAppPlatform=android" in out and "tgWebAppVersion=8.0" in out
    assert "tgWebAppThemeParams=…" in out


def test_raw_and_encoded_init_data_in_text():
    assert redact.redact_str(INIT) == "***initData***"
    assert "c501b71e" not in redact.redact_str("init: " + INIT_ENC + " end")
    assert "c501b71e" not in redact.redact_str("Authorization: tma " + INIT)


def test_json_key_rules():
    data = {"initData": INIT, "token": "USDT", "quote_token": "TON", "access_token": "abc",
            "accessToken": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
            "price": "1.1499", "volume": "6336000000000000000", "pin": 1234, "password": "",
            "nested": [{"hash": "deadbeef" * 8, "card_number": "2202202412345678", "phone": "+79161234567"}],
            "session": {"id": 5}, "ok": True, "none": None}
    out = redact.redact_obj(data)
    assert out["initData"] == "***" and out["access_token"] == "***" and out["accessToken"] == "***"
    assert out["token"] == "USDT" and out["quote_token"] == "TON"
    assert out["price"] == "1.1499" and out["volume"] == "6336000000000000000"
    assert out["pin"] == "***" and out["password"] == ""
    assert out["nested"][0]["hash"] == "***"
    assert out["nested"][0]["card_number"] == "*** 5678" and out["nested"][0]["phone"] == "*** 4567"
    assert out["session"] == {"id": 5} and out["ok"] is True and out["none"] is None


def test_suffix_keys_mask_long_values_only():
    out = redact.redact_obj({"base_token": "USDT", "refresh_token_v2": "x" * 30, "user_secret": "s3cr3t-value-123",
                             "tokenSymbol": "NOTCOIN", "has_password": True, "password_hint": "кличка кота",
                             "X-Init-Data-Raw": "q" * 40})
    assert out["base_token"] == "USDT" and out["tokenSymbol"] == "NOTCOIN"
    assert out["refresh_token_v2"] == "***" and out["user_secret"] == "***" and out["X-Init-Data-Raw"] == "***"
    assert out["has_password"] is True and out["password_hint"] == "кличка кота"


def test_headers():
    h = redact.redact_headers({
        "Authorization": "Bearer sk_live_abcdefghijk", "Cookie": "session=SECRET; theme=dark",
        "Set-Cookie": "session=SECRET123; Path=/; HttpOnly\nother=X; Path=/",
        "X-Init-Data": INIT, "Content-Type": "application/json", "X-RateLimit-Remaining": "9"})
    assert h["authorization"] == "Bearer ***"
    assert h["cookie"] == "session=***; theme=***"
    assert "SECRET123" not in h["set-cookie"] and "HttpOnly" in h["set-cookie"]
    assert h["x-init-data"] == "***"
    assert h["content-type"] == "application/json"
    kept = redact.redact_headers({"Content-Type": "a", "Server": "nginx", "X-RateLimit-Remaining": "9"},
                                 keep={"content-type"})
    assert kept == {"content-type": "a", "x-ratelimit-remaining": "9"}


def test_body_json_form_and_text():
    j, t = redact.redact_body('{"initData": "x", "ok": true}', "application/json")
    assert j == {"initData": "***", "ok": True} and t is None
    j, t = redact.redact_body("initData=" + INIT_ENC + "&x=1", "application/x-www-form-urlencoded")
    assert j is None and t == "initData=***&x=1"
    # обрезанный JSON разбирается как текст — ключи токенов всё равно скрыты
    j, t = redact.redact_body('{"accessToken":"abcdef123456","items":[1,2', "application/json")
    assert j is None and "abcdef123456" not in t


def test_text_patterns_aggressive():
    s = redact.redact_str("Карта 2202 2024 1234 5678, тел +7 916 123-45-67, сырая 4111111111111111, "
                          "объём 6336000000000000000, бот 123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-0A",
                          aggressive=True)
    assert "2202 2024" not in s and "**** **** **** 5678" in s
    assert "916 123" not in s
    assert "4111111111111111" not in s and "1111" in s
    assert "6336000000000000000" in s          # не карта (не проходит Луна) — не трогаем
    assert "AAEhBOweik6ad9r" not in s
    # без aggressive сырые номера не трогаем (это могут быть суммы в минимальных единицах)
    assert "4111111111111111" in redact.redact_str("4111111111111111")


def test_url_userinfo_and_bot_token_path():
    out = redact.redact_url("https://user:pass@api.telegram.org/bot123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-0A/getMe?token=abcdefghijklmnopqrstu")
    assert "pass@" not in out and "AAEhBOweik6ad9r" not in out and "abcdefghijklmnopqrstu" not in out


def test_idempotent():
    data = {"initData": INIT, "url": "https://x/#tgWebAppData=" + INIT_ENC, "text": "карта 2202 2024 1234 5678"}
    once = redact.redact_obj(data, aggressive=True)
    assert redact.redact_obj(once, aggressive=True) == once
    json.dumps(once)
