from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import unquote

import json
import pytest
from telethon import functions
from telethon.tl import types

from autobid import bots
from autobid.recorder import launch as L

BOT = types.InputPeerUser(user_id=777, access_hash=1)


def msg(mid, markup, out=False):
    return types.Message(id=mid, peer_id=types.PeerUser(777), date=datetime(2026, 11, 3, tzinfo=timezone.utc),
                         message="меню", out=out, reply_markup=markup)


MENU = types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=[
    types.KeyboardInlineButton(text="Кошелёк", type=types.InlineButtonTypeWebView(url="https://app.cr.bot/")),
    types.KeyboardInlineButton(text="🔄 Биржа", type=types.InlineButtonTypeWebView(url="https://app.cr.bot/exchange")),
    types.KeyboardInlineButton(text="P2P", type=types.InlineButtonTypeCallback(data=b"p2p")),
])])


class FakeClient:
    def __init__(self, messages=(), full=None):
        self.messages = list(messages)
        self.full = full
        self.requests, self.sent = [], []

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.users.GetFullUserRequest):
            return self.full
        return types.WebViewResultUrl(url="https://app.cr.bot/#tgWebAppData=query_id%3D1%26hash%3Dabc", query_id=42)

    async def get_messages(self, entity, limit=None, min_id=None):
        return [m for m in self.messages if min_id is None or m.id > min_id][:limit]

    async def send_message(self, entity, text, **kw):
        self.sent.append(text)
        self.messages.insert(0, msg(100 + len(self.sent), MENU))


def test_normalize_adds_missing_params_only():
    url = L.normalize_launch_url("https://a/#tgWebAppData=abc")
    frag = url.split("#", 1)[1]
    assert frag.startswith("tgWebAppData=abc&")
    assert "tgWebAppVersion=8.0" in frag and "tgWebAppPlatform=android" in frag
    theme = json.loads(unquote(frag.split("tgWebAppThemeParams=")[1]))
    assert theme["bg_color"] == "#212d3b"
    full = "https://a/#tgWebAppData=abc&tgWebAppVersion=9.1&tgWebAppPlatform=ios&tgWebAppThemeParams=%7B%7D"
    assert L.normalize_launch_url(full) == full
    assert L.normalize_launch_url("https://a/").startswith("https://a/#tgWebAppVersion=")


def test_match_button():
    found = [(None, 1, {"text": "Кошелёк"}), (None, 2, {"text": "🔄 Биржа"}), (None, 3, {"text": "Биржа PRO"})]
    assert L.match_button(found, "биржа")[1] == 2           # точное совпадение без эмодзи важнее вхождения
    assert L.match_button(found, "pro")[1] == 3
    assert L.match_button(found, "P2P") is None


async def test_main_mode():
    c = FakeClient()
    res = await L.request_launch(c, BOT, mode="main", start_param="abc")
    req = c.requests[0]
    assert isinstance(req, functions.messages.RequestMainWebViewRequest)
    assert req.platform == "android" and req.start_param == "abc" and json.loads(req.theme_params.data)["bg_color"]
    assert res.mode == "main" and res.query_id == 42 and "tgWebAppData" in res.url


async def test_button_mode_finds_inline_webapp():
    c = FakeClient([msg(10, MENU)])
    res = await L.request_launch(c, BOT, mode="button", button="Биржа")
    req = c.requests[0]
    assert isinstance(req, functions.messages.RequestWebViewRequest)
    assert req.url == "https://app.cr.bot/exchange" and not req.from_bot_menu
    assert res.button["text"] == "🔄 Биржа" and res.title == "кнопка «🔄 Биржа»"


async def test_button_mode_sends_start_when_missing(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(L.asyncio, "sleep", no_sleep)
    c = FakeClient([])
    res = await L.request_launch(c, BOT, mode="button", button="Биржа")
    assert c.sent == ["/start"] and res.button["url"].endswith("/exchange")


async def test_button_mode_not_found_lists_buttons():
    c = FakeClient([msg(10, MENU)])
    with pytest.raises(L.LaunchError) as e:
        await L.request_launch(c, BOT, mode="button", button="Магазин", send_start=False)
    assert "Кошелёк" in str(e.value) and "Биржа" in str(e.value)


async def test_simple_webview_from_reply_keyboard():
    kb = types.ReplyKeyboardMarkup(rows=[types.KeyboardButtonRow(buttons=[
        types.KeyboardButton(text="Магазин", type=types.ButtonTypeSimpleWebView(url="https://shop/"))])])
    c = FakeClient([msg(10, kb)])
    await L.request_launch(c, BOT, mode="button", button="Магазин", send_start=False)
    assert isinstance(c.requests[0], functions.messages.RequestSimpleWebViewRequest)
    assert c.requests[0].url == "https://shop/"


async def test_menu_and_url_and_app_modes():
    full = SimpleNamespace(full_user=SimpleNamespace(bot_info=SimpleNamespace(
        menu_button=types.BotMenuButton(text="Открыть", url="https://menu/"))))
    c = FakeClient(full=full)
    res = await L.request_launch(c, BOT, mode="menu")
    assert c.requests[-1].from_bot_menu is True and c.requests[-1].url == "https://menu/"
    assert res.title == "кнопка меню «Открыть»"

    ready = "https://x/#tgWebAppData=abc"
    assert (await L.request_launch(c, BOT, mode="url", url=ready)).url == ready
    await L.request_launch(c, BOT, mode="url", url="https://x/page")
    assert c.requests[-1].url == "https://x/page"

    await L.request_launch(c, BOT, mode="app", app="exchange", start_param="s")
    req = c.requests[-1]
    assert isinstance(req, functions.messages.RequestAppWebViewRequest)
    assert req.app.short_name == "exchange" and req.app.bot_id.user_id == 777


async def test_menu_missing():
    c = FakeClient(full=SimpleNamespace(full_user=SimpleNamespace(bot_info=None)))
    with pytest.raises(L.LaunchError):
        await L.request_launch(c, BOT, mode="menu")


async def test_list_options():
    full = SimpleNamespace(full_user=SimpleNamespace(bot_info=SimpleNamespace(
        menu_button=types.BotMenuButton(text="Открыть", url="https://menu/"))))
    c = FakeClient([msg(10, MENU)], full=full)
    labels = [o[0] for o in await L.list_options(c, BOT)]
    assert labels == ["главное мини-приложение бота", "кнопка меню «Открыть»", "кнопка «Кошелёк»", "кнопка «🔄 Биржа»"]


def test_bot_aliases():
    assert bots.resolve("cryptobot") == "send" and bots.resolve("@CryptoBot") == "send"
    assert bots.resolve("https://t.me/xrocket?start=1") == "xrocket"
    assert bots.resolve("xrocket_testnet") == "xrocket_testnet_bot"
    assert bots.slug("send") == "cryptobot" and bots.title("xrocket") == "xRocket"


def test_parse_direct_link():
    assert L.parse_direct_link("https://t.me/send/exchange?startapp=TON_USDT") == \
        {"bot": "send", "app": "exchange", "start_param": "TON_USDT"}
    assert L.parse_direct_link("t.me/xrocket?startapp") == {"bot": "xrocket", "app": None, "start_param": None}
    assert L.parse_direct_link("tg://resolve?domain=send&appname=p2p&startapp=x") == \
        {"bot": "send", "app": "p2p", "start_param": "x"}
    assert L.parse_direct_link("https://t.me/CryptoBotRU/123") is None          # пост канала
    assert L.parse_direct_link("https://t.me/send") is None                      # просто бот
    assert L.parse_direct_link("https://example.com/send/app") is None


class DirectClient(FakeClient):
    async def get_input_entity(self, name):
        return types.InputPeerUser(user_id=999, access_hash=2)


async def test_button_with_direct_link_opens_named_app():
    markup = types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=[
        types.KeyboardInlineButton(text="Биржа", type=types.InlineButtonTypeUrl(url="https://t.me/send/exchange?startapp=a"))])])
    c = DirectClient([msg(10, markup)])
    res = await L.request_launch(c, BOT, mode="button", button="Биржа", send_start=False)
    req = c.requests[-1]
    assert isinstance(req, functions.messages.RequestAppWebViewRequest)
    assert req.app.short_name == "exchange" and req.start_param == "a" and req.app.bot_id.user_id == 999
    assert res.mode == "direct"


async def test_url_mode_with_main_app_direct_link():
    c = DirectClient()
    await L.request_launch(c, BOT, mode="url", url="https://t.me/xrocket?startapp=ref")
    req = c.requests[-1]
    assert isinstance(req, functions.messages.RequestMainWebViewRequest) and req.start_param == "ref"
