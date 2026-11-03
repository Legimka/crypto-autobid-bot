"""
Сквозная проверка рекордера мини-приложений: поддельная «биржа» на официальном telegram-web-app.js
открывается в Chromium без окна, тест кликает как человек и смотрит, что попало в запись — и что не попало.
"""
import asyncio
import base64
import json
import os
import shutil
from urllib.parse import quote

import pytest
from aiohttp import WSMsgType, web

from autobid.recorder import launch as L
from autobid.recorder import webapp as W
from autobid.recorder.common import read_events

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "fake_miniapp")
INIT_DATA = ("query_id=AAHTESTQUERY&user=%7B%22id%22%3A1%2C%22first_name%22%3A%22Test%22%7D"
             "&auth_date=1700000000&hash=" + "deadbeef" * 8)
SECRETS = ["deadbeefdeadbeef", "AAHTESTQUERY", "COOKIESECRETVALUE123", "sk_live_TOKEN_SECRET", "APIKEYSECRET",
           "SESSIONSECRETVALUE", "CLOUDSECRETTOKENVALUE", "WSSECRETTOKEN", "ZXQ-PIN-777"]
WS_FRAMES = 260


def chromium_path():
    for p in (os.environ.get("AUTOBID_CHROMIUM"), "/opt/pw-browsers/chromium"):
        if p and os.path.exists(p):
            return p
    return None


async def start_server():
    async def static(request):
        name = request.match_info.get("name") or "index.html"
        return web.FileResponse(os.path.join(FIXTURE, name))

    async def auth(request):
        body = await request.json()
        assert "auth_date=" in body["initData"], body
        resp = web.json_response({"ok": True, "user": {"id": 1}})
        resp.set_cookie("session", "COOKIESECRETVALUE123", httponly=True)
        return resp

    async def markets(request):
        return web.json_response({"pairs": [{"symbol": "TON-USDT", "bid": "1.1500", "ask": "1.1510"}]})

    async def orders(request):
        body = await request.json()
        return web.json_response({"id": 12345, "status": "placed", "price": body["price"]})

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await ws.send_bytes(b"\x08\x96\x01\x12\x04TON!")     # двоичный кадр (как protobuf)
                for i in range(WS_FRAMES - 1):
                    await ws.send_str(json.dumps({"bids": [["1.15", "10"]], "seq": i}))
        return ws

    app = web.Application()
    app.add_routes([web.get("/", static), web.get("/ws", ws_handler), web.post("/api/auth", auth),
                    web.get("/api/markets", markets), web.post("/api/orders", orders), web.get("/{name}", static)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


@pytest.fixture
async def browser():
    pw_api = pytest.importorskip("playwright.async_api")
    async with pw_api.async_playwright() as p:
        try:
            b = await p.chromium.launch(headless=True, executable_path=chromium_path())
        except Exception as e:   # нет браузера в окружении
            pytest.skip(f"Chromium недоступен: {e}")
        yield b
        await b.close()


async def test_records_clicks_requests_ws_and_hides_secrets(tmp_path, browser):
    runner, port = await start_server()
    storage = {}

    async def custom_method(method, params):
        if method == "saveStorageValue":
            storage[params["key"]] = params["value"]
            return True
        if method == "getStorageValues":
            return {k: storage.get(k, "") for k in params["keys"]}
        raise RuntimeError("METHOD_INVALID")

    url = L.normalize_launch_url(f"http://127.0.0.1:{port}/#tgWebAppData={quote(INIT_DATA, safe='')}")
    out = tmp_path / "rec"
    out.mkdir()
    rec = W.WebAppRecorder(str(out), custom_method=custom_method, echo=lambda *a: None)
    try:
        context = await browser.new_context(viewport=W.VIEWPORT, user_agent=W.ANDROID_UA, bypass_csp=True,
                                            service_workers="block")
        await rec.attach(context)
        rec.log.write("start", bot="send", bot_title="CryptoBot", what="тест", url="http://x/", platform="android",
                      account="@test", browser="chromium")
        page = await context.new_page()
        await page.goto(url)
        await page.wait_for_function("window.__appReady === true && window.__appState.auth === 200")
        state = await page.evaluate("window.__appState")
        assert state["platform"] == "android" and "auth_date=" in state["initData"]
        assert state["version"] == "8.0"

        await page.click("#markets")
        await page.wait_for_function("document.getElementById('out').textContent.length > 0")
        await page.click(".cell b")                       # клик по вложенному элементу
        await page.click("#fee span")                     # «кнопка» на div с cursor:pointer
        await page.fill("#price", "1.1499")
        await page.fill("#pin", "ZXQ-PIN-777")
        await page.press("#price", "Enter")
        await page.get_by_role("button", name="Разместить ордер").click()     # MainButton в эмуляции Telegram
        await page.get_by_role("button", name="Да").click()                   # кнопка всплывающего окна
        await page.wait_for_function("window.__appState.order && window.__appState.order.status === 'placed'")
        await page.get_by_role("button", name="‹ Назад").click()
        await page.wait_for_function("window.__appState.back === true")
        await page.wait_for_function(f"window.__appState.wsFrames >= {WS_FRAMES}")
        await page.wait_for_function("window.__appState.cloudGet !== undefined")
        state = await page.evaluate("window.__appState")
        assert state["popup"] == "yes" and state["cloudSet"] is True
        assert state["cloudGet"] == "CLOUDSECRETTOKENVALUE123456789"    # приложение получило значение…
        assert state["viewport"] == W.VIEWPORT["height"] - 64            # MainButton уменьшил высоту окна

        await asyncio.sleep(0.3)
        await rec.drain()
        await context.close()
        assert rec.closed.is_set()
    finally:
        summary = rec.finish()
        await runner.cleanup()

    events = read_events(str(out / "events.jsonl"))
    by = lambda kind: [e for e in events if e["kind"] == kind]  # noqa: E731

    clicks = [e.get("text") for e in by("click")]
    assert "Рынки" in clicks and "TON" in clicks
    nested = next(e for e in by("click") if e.get("text") == "TON")
    assert nested["tag"] == "b" and nested.get("around") == "Купить TON"
    pointer = next(e for e in by("click") if e.get("text") == "Комиссия 0.1%")
    assert pointer["tag"] == "div" and pointer.get("inner") == "0.1%"     # div с cursor:pointer, клик по span
    inputs = {e["label"]: e["value"] for e in by("input")}
    assert inputs["Цена"] == "1.1499" and inputs["PIN"] == "***"
    assert any(e["key"] == "Enter" for e in by("key"))
    ui = [(e["what"], e["text"]) for e in by("tg_ui")]
    assert ("main_button", "Разместить ордер") in ui and ("popup", "Да") in ui and ("back_button", "Назад") in ui
    methods = [e["method"] for e in by("tg")]
    assert "web_app_setup_main_button" in methods and "web_app_open_popup" in methods

    reqs = {e["id"]: e for e in by("request")}
    resps = {e["id"]: e for e in by("response")}
    auth = next(e for e in reqs.values() if e["url"].endswith("/api/auth"))
    assert auth["body"] == {"initData": "***"} and auth["headers"]["authorization"] == "Bearer ***"
    assert "COOKIESECRETVALUE123" not in json.dumps(resps[auth["id"]])
    assert resps[auth["id"]]["headers"]["set-cookie"].startswith("session=***")
    mk = next(e for e in reqs.values() if "/api/markets" in e["url"])
    assert mk["headers"]["x-api-key"] == "***"
    assert resps[mk["id"]]["body"]["pairs"][0]["bid"] == "1.1500"
    order = next(e for e in reqs.values() if e["url"].endswith("/api/orders"))
    assert order["body"]["price"] == "1.1499" and order["body"]["session_token"] == "***"
    assert resps[order["id"]]["body"]["status"] == "placed"

    cm = by("tg_custom_method")
    assert [e["method"] for e in cm] == ["saveStorageValue", "getStorageValues"]
    assert "CLOUDSECRET" not in json.dumps(cm)

    assert len(by("ws_open")) == 1
    sent = by("ws_sent")
    assert sent[0]["data"]["op"] == "subscribe" and sent[0]["data"]["token"] == "***"
    expected_logged = 200 + len([n for n in range(201, WS_FRAMES + 1) if n % 25 == 0])
    assert len(by("ws_recv")) == expected_logged
    first = by("ws_recv")[0]["data"]
    assert first["binary_bytes"] == 9 and base64.b64decode(first["b64"]) == b"\x08\x96\x01\x12\x04TON!"
    stop = by("stop")[0]
    assert stop["ws"]["1"]["recv"] == WS_FRAMES and stop["ws"]["1"]["logged_recv"] == expected_logged

    saved = {os.path.basename(e["saved"]) for e in by("response") if e.get("saved")}
    assert {"index.html", "app.js", "telegram-web-app.js"} <= saved

    assert "ХРОНОЛОГИЯ" in summary and "клик «Рынки»" in summary and "/api/orders" in summary
    assert "ЭНДПОИНТЫ" in summary and "POST   127.0.0.1" in summary
    assert "каждый 25-й" in summary

    # Ни одного секрета в записи (кроме assets/*.js — это код приложения, в нём строки теста зашиты нарочно)
    for root, _, files in os.walk(out):
        for name in files:
            path = os.path.join(root, name)
            if name.endswith(".js"):
                continue
            text = open(path, encoding="utf-8", errors="replace").read()
            for s in SECRETS:
                assert s not in text, f"{s} попал в {os.path.relpath(path, out)}"


def test_asset_paths_are_safe():
    p = W.asset_relpath("https://app.cr.bot/static/js/main.abc.js?v=1", "application/javascript")
    assert p.startswith(os.path.join("assets", "app.cr.bot", "static", "js", "main.abc_")) and p.endswith(".js")
    assert W.asset_relpath("https://app.cr.bot/", "text/html").endswith(os.path.join("app.cr.bot", "index.html"))
    assert ".." not in W.asset_relpath("https://h/../../etc/passwd", "")
    assert ":" not in W.asset_relpath("https://h:8080/a:b", "").split(os.sep, 1)[1]


def test_storage_values_masked():
    assert W.sanitize_tg("web_app_secure_storage_save_key", {"key": "k", "value": "v"})["value"] == "***"
    assert W.sanitize_tg("web_app_device_storage_save_key", {"key": "theme", "value": "dark"})["value"] == "dark"
    masked = W.sanitize_tg("web_app_device_storage_save_key", {"key": "x", "value": "A" * 40})["value"]
    assert masked.startswith("***")
    params, result = W.sanitize_custom("getStorageValues", {"keys": ["token"]}, {"token": "abc", "lang": "ru"})
    assert result == {"token": "***", "lang": "ru"}


def test_shim_has_no_unreplaced_placeholder(tmp_path):
    rec = W.WebAppRecorder(str(tmp_path), echo=lambda *a: None)
    src = rec.shim_source()
    assert "__AUTOBID_CFG__" not in src and '"platform": "android"' in src
    rec.finish()


def test_parser():
    a = W.build_parser().parse_args(["test", "cryptobot", "--button", "Биржа"])
    assert a.button == "Биржа" and a.browser == "auto" and not a.headless
    with pytest.raises(SystemExit):
        W.build_parser().parse_args(["test", "--main", "--menu"])
