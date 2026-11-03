import asyncio
import json
from datetime import datetime, timezone

import pytest
from telethon import errors, functions
from telethon.tl import types

from autobid.recorder import chat
from autobid.recorder.common import read_events

BOT = types.InputPeerUser(user_id=777, access_hash=1)


def message(mid, text, markup=None, out=False, entities=None):
    return types.Message(id=mid, peer_id=types.PeerUser(777), date=datetime(2026, 11, 3, tzinfo=timezone.utc),
                         message=text, out=out, reply_markup=markup, entities=entities)


def inline(*rows):
    return types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=list(r)) for r in rows])


def cb(text, data):
    return types.KeyboardInlineButton(text=text, type=types.InlineButtonTypeCallback(data=data))


def webapp(text, url):
    return types.KeyboardInlineButton(text=text, type=types.InlineButtonTypeWebView(url=url))


class FakeClient:
    def __init__(self, answer=None, error=None):
        self.requests, self.sent = [], []
        self.answer, self.error = answer, error
        self.next_id = 900

    async def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.answer

    async def send_message(self, entity, text, **kw):
        self.next_id += 1
        self.sent.append((entity, text, kw))
        return message(self.next_id, text, out=True)


@pytest.fixture
def rec(tmp_path):
    def make(client=None):
        lines = []
        r = chat.ChatRecorder(client or FakeClient(), BOT, "send", str(tmp_path), session_name="test",
                              echo=lines.append)
        r.lines = lines
        return r
    return make


MENU = inline([webapp("Кошелёк", "https://app.cr.bot/"), cb("P2P", b"p2p")],
              [webapp("🔄 Биржа", "https://app.cr.bot/exchange"), cb("Настройки", b"settings")])


def test_parse_command():
    assert chat.parse_command("3") == ("press", 3, None)
    assert chat.parse_command(" 2 @ 4567 ") == ("press", 2, 4567)
    assert chat.parse_command("/p2p") == ("send", "/p2p")
    assert chat.parse_command("45.68") == ("send", "45.68")
    assert chat.parse_command("> 100") == ("send", "100")
    assert chat.parse_command("!q") == ("quit",) and chat.parse_command("!выход") == ("quit",)
    assert chat.parse_command("!s") == ("screen",) and chat.parse_command("?") == ("help",)
    assert chat.parse_command("") == ("noop",)
    assert chat.parse_command("q") == ("send", "q")    # в интерактиве «q» — это текст боту


def test_handle_renders_and_logs(rec, tmp_path):
    r = rec()
    m = r.handle(message(10, "Привет! Карта 2202 2024 1234 5678", MENU), "new")
    assert r.current_id == 10
    out = "\n".join(r.lines)
    assert "[1] Кошелёк ⧉мини-апп" in out and "[4] Настройки" in out
    assert "«🔄 Биржа» → https://app.cr.bot/exchange" in out
    assert "2202 2024" not in out and "5678" in out
    # та же правка без изменений — не пишем
    assert r.handle(message(10, "Привет! Карта 2202 2024 1234 5678", MENU), "edit") is None
    r.handle(message(10, "Привет! Карта 2202 2024 1234 5678", inline([cb("Назад", b"back")])), "edit")
    assert "поменялись только кнопки" in r.lines[-1]
    r.close()
    ev = read_events(str(tmp_path / "events.jsonl"))
    assert [e["kind"] for e in ev] == ["message", "message", "stop"]
    assert ev[0]["msg"]["markup"]["rows"][0][1]["data_hex"] == b"p2p".hex()
    assert "2202 2024" not in json.dumps(ev, ensure_ascii=False)


async def test_press_callback_with_toast(rec, tmp_path):
    client = FakeClient(answer=types.messages.BotCallbackAnswer(cache_time=0, message="Цена обновлена", alert=False))
    r = rec(client)
    r.handle(message(10, "Меню", MENU), "new")
    assert await r.press(2) is True
    req = client.requests[-1]
    assert isinstance(req, functions.messages.GetBotCallbackAnswerRequest)
    assert req.msg_id == 10 and req.data == b"p2p" and req.peer == BOT
    assert any("ВСПЛЫВАШКА БОТА: Цена обновлена" in x for x in r.lines)
    r.close()
    kinds = [e["kind"] for e in read_events(str(tmp_path / "events.jsonl"))]
    assert kinds == ["message", "press", "callback_answer", "stop"]


async def test_press_timeout_and_stale(rec):
    r = rec(FakeClient(error=errors.BotResponseTimeoutError(request=None)))
    r.handle(message(10, "Меню", MENU), "new")
    assert await r.press(2) is True
    assert "не подтвердил" in r.lines[-1]
    r2 = rec(FakeClient(error=errors.MessageIdInvalidError(request=None)))
    r2.handle(message(10, "Меню", MENU), "new")
    assert await r2.press(2) is False and "устарела" in r2.lines[-1]


async def test_press_webapp_and_bad_index(rec):
    r = rec()
    r.handle(message(10, "Меню", MENU), "new")
    assert await r.press(3) is False
    assert 'record_webapp.py test cryptobot --button "🔄 Биржа"' in r.lines[-1]
    assert await r.press(9) is False and "> 9" in r.lines[-1]
    assert await rec().press(1) is False      # экрана ещё нет


async def test_reply_keyboard_press_sends_text(rec):
    client = FakeClient()
    r = rec(client)
    kb = types.ReplyKeyboardMarkup(rows=[types.KeyboardButtonRow(buttons=[
        types.KeyboardButton(text="Купить", type=types.ButtonTypeDefault()),
        types.KeyboardButton(text="Телефон", type=types.ButtonTypeRequestPhone())])])
    r.handle(message(11, "Выбери", kb), "new")
    assert await r.press(1) is True
    assert client.sent[-1][1] == "Купить" and client.sent[-1][2]["parse_mode"] is None
    assert await r.press(2) is False


async def test_run_command_and_wait_change(rec):
    client = FakeClient()
    r = rec(client)
    assert await r.run_command("/p2p") is True and client.sent[-1][1] == "/p2p"
    assert await r.run_command("!q") is False
    r.changed.clear()
    asyncio.get_running_loop().call_later(0.05, r.handle, message(12, "Экран", MENU), "new")
    assert await r.wait_change(timeout=1, settle=0) is True


def test_deleted_updates_current(rec):
    r = rec()
    r.handle(message(10, "A", MENU), "history")
    r.handle(message(11, "B", MENU), "new")
    r.handle_deleted([11, 99999])
    assert r.current_id == 10 and 11 not in r.screens


def test_links_from_entities():
    text = "Подробнее тут"
    m = message(5, text, entities=[types.MessageEntityTextUrl(offset=10, length=3, url="https://t.me/send?startapp=x")])
    s = chat.serialize(m)
    assert s["links"] == [{"text": "тут", "url": "https://t.me/send?startapp=x"}]


def test_parser_defaults():
    a = chat.build_parser().parse_args(["test"])
    assert a.bot == "cryptobot" and not a.interactive and a.command == [] and a.history == 3


async def test_own_message_event_before_send_returns_is_not_duplicated(rec):
    holder = {}

    class RacingClient(FakeClient):
        async def send_message(self, entity, text, **kw):
            sent = message(950, text, out=True)
            holder["r"].handle(sent, "new")       # событие пришло раньше, чем send_message вернулся
            return sent

    r = rec(RacingClient())
    holder["r"] = r
    await r.send_text("45.68")
    assert sum("45.68" in line for line in r.lines) == 1


async def test_url_button_to_mini_app_gives_hint(rec):
    r = rec()
    r.handle(message(10, "Меню", inline([types.KeyboardInlineButton(
        text="Биржа", type=types.InlineButtonTypeUrl(url="https://t.me/send/exchange"))])), "new")
    assert await r.press(1) is False
    assert 'record_webapp.py test cryptobot --button "Биржа"' in r.lines[-1]
