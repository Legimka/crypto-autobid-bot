"""Регрессии по правкам из разбора рекордеров: утечки в маскировке, устойчивость, хранилища."""
import json

import pytest

from autobid import redact as R
from autobid.recorder import webapp as W
from autobid.recorder import launch as L
from autobid.recorder.common import read_events


# ------------------------------------------------------------------ redact
def test_bearer_token_not_left_after_scheme_word():
    # раньше _KV_TEXT съедал слово «Bearer» как значение и оставлял сам токен
    out = R.redact_text_kv("Authorization: Bearer 3f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c")
    assert "3f9a8b7c" not in out
    out2 = R.redact_text_kv("CONNECT\nAuthorization:Bearer 3f9a8b7c6d5e4f3a2b1c0d9e8f")
    assert "3f9a8b7c" not in out2


def test_pay_token_with_short_app_id_masked():
    tok = "9009:AAzQcB2X6RBK3U7FbVgAOQu1CEyxDqdQHk5zz"
    assert "AAzQcB2X" not in R.redact_str("API Token: " + tok, aggressive=True)
    assert "c01709a9" not in R.redact_text_kv("Токен: c01709a9c058bd25eeefea6b2a7f3e91aa")


def test_cheque_codes_masked():
    assert R.redact_str("https://t.me/send?start=CQ8xKqZ3mN2p") == "https://t.me/send?start=CQ***"
    assert "8xKqZ3mN2p" not in R.redact_text_kv("твой чек: CQ8xKqZ3mN2p")


def test_relative_url_with_secret_masked():
    assert R.redact_str("/withdraw/confirm?token=RELHREFSECRET0123456789") == "/withdraw/confirm?token=***"
    assert "OPAQUE" not in R.redact_str("/cb#access_token=OPAQUEabcdef0123456789")
    for keep in ("a=1&b=2", "45.68", "/start", "Купить TON"):
        assert R.redact_str(keep) == keep


def test_header_names_auth_session_access_masked():
    h = R.redact_headers({"X-Session-Id": "OPAQUESESSIONVALUE123", "X-Access": "tok_abcdefghij"})
    assert h == {"x-session-id": "***", "x-access": "***"}


def test_form_body_detection_is_linear_and_correct():
    # не экспоненциально и тот же приём/отказ, что и раньше
    j, t = R.redact_body("a=1&b=2&c=3")
    assert j is None and t == "a=1&b=2&c=3"
    j, t = R.redact_body("plain text with spaces = not a form")
    assert t is not None   # ушло в текстовую ветку, не в form


# ------------------------------------------------------------------ webapp: хранилища
def test_mask_storage_hides_short_pins_keeps_flags():
    assert W.mask_storage_value("lock", "975310") .startswith("***")
    assert W.mask_storage_value("passcode", "4821").startswith("***")
    assert W.mask_storage_value("lang", "ru") == "ru"
    assert W.mask_storage_value("theme", "dark") == "dark"
    assert W.mask_storage_value("locale", "en-US") == "en-US"
    assert W.mask_storage_value("flag", "true") == "true"
    assert W.mask_storage_value("secure_key", "anything", secure=True) == "***"


# ------------------------------------------------------------------ webapp: двоичный WebSocket
def test_binary_ws_json_frame_is_redacted_not_base64(tmp_path):
    rec = W.WebAppRecorder(str(tmp_path), echo=lambda *a: None)
    rec.ws_stats["1"] = {"url": "wss://x/", "sent": 0, "recv": 0, "logged_recv": 0}
    rec._ws_frame("1", "recv", b'{"type":"auth_ok","session_token":"WSBINSECRET_0123456789abcdef"}')
    rec._ws_frame("1", "recv", b"\x08\x96\x01\x12\x04TON!")   # настоящий двоичный — остаётся b64
    rec.log.close()
    frames = [e for e in read_events(str(tmp_path / "events.jsonl")) if e["kind"] == "ws_recv"]
    dump = json.dumps(frames, ensure_ascii=False)
    assert "WSBINSECRET" not in dump
    assert frames[0]["data"].get("text", {}).get("session_token") == "***"
    assert "b64" in frames[1]["data"]      # неразбираемый двоичный кадр сохранён как есть


# ------------------------------------------------------------------ webapp: дочистка выученных секретов
def test_learned_secrets_scrubbed_from_whole_record(tmp_path):
    rec = W.WebAppRecorder(str(tmp_path), echo=lambda *a: None)
    rec._learn({"authorization": "Bearer OPAQUEBEARERTOKEN1234567890"})
    # тот же токен вернулся в теле под безобидным ключом result — redact_obj его не трогает
    rec.log.write("response", id=1, body={"ok": True, "result": "OPAQUEBEARERTOKEN1234567890"})
    rec.finish()
    text = open(tmp_path / "events.jsonl", encoding="utf-8").read()
    assert "OPAQUEBEARERTOKEN" not in text


# ------------------------------------------------------------------ webapp: прокси сессии
def test_playwright_proxy_refuses_socks5_with_auth():
    assert W.playwright_proxy(None) is None
    assert W.playwright_proxy("http://127.0.0.1:8080")["server"] == "http://127.0.0.1:8080"
    with pytest.raises(RuntimeError):
        W.playwright_proxy("socks5://user:pass@host:1080")


# ------------------------------------------------------------------ launch: служебные пути t.me
def test_reserved_tme_paths_are_not_apps():
    for u in ("https://t.me/share/url?url=x", "t.me/joinchat/AAAA", "t.me/invoice/abc",
              "tg://resolve?domain=share&startapp=x"):
        assert L.parse_direct_link(u) is None
    assert L.parse_direct_link("https://t.me/send/exchange?startapp=TON")["bot"] == "send"


async def test_launch_direct_bad_entity_raises_launcherror():
    class Bad:
        async def get_input_entity(self, name):
            raise ValueError('No user has "x" as username')

    with pytest.raises(L.LaunchError):
        await L._launch_direct(Bad(), {"bot": "x", "app": "a", "start_param": None}, "android", "t")


# ------------------------------------------------------------------ accounts: защита файла сессии
def test_session_guards(tmp_path, monkeypatch):
    from autobid import accounts, settings
    monkeypatch.setattr(settings, "sessions_dir", lambda: str(tmp_path))
    assert accounts.list_sessions() == [] and not accounts.session_exists("test")
    open(tmp_path / "test.session", "w").close()
    assert accounts.session_exists("test") and "test" in accounts.list_sessions()
    lock = accounts._lock_session("test")
    try:
        with pytest.raises(accounts.SessionBusy):
            accounts._lock_session("test")      # второй процесс на том же файле сессии
    finally:
        lock.close()


async def test_connect_authorized_missing_session_makes_no_file(tmp_path, monkeypatch):
    from autobid import accounts, settings
    monkeypatch.setattr(settings, "sessions_dir", lambda: str(tmp_path))
    with pytest.raises(accounts.NotAuthorized):
        await accounts.connect_authorized("nope")
    assert not (tmp_path / "nope.session").exists()   # опечатка в имени не создаёт пустую сессию
