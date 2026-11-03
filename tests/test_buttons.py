from telethon.tl import types

from autobid.recorder import buttons as B


def old_style(cls_name, **attrs):
    """Кнопка как в Telethon ≤ 1.44: отдельный класс на тип (KeyboardButtonCallback и т.д.)."""
    obj = type(cls_name, (), {})()
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


def test_new_schema_inline_buttons():
    markup = types.ReplyInlineMarkup(rows=[
        types.KeyboardInlineButtonRow(buttons=[
            types.KeyboardInlineButton(text="Кошелёк", type=types.InlineButtonTypeWebView(url="https://app.cr.bot/")),
            types.KeyboardInlineButton(text="P2P", type=types.InlineButtonTypeCallback(data=b"p2p:menu")),
        ]),
        types.KeyboardInlineButtonRow(buttons=[
            types.KeyboardInlineButton(text="Канал", type=types.InlineButtonTypeUrl(url="https://t.me/CryptoBotRU")),
            types.KeyboardInlineButton(text="Скопировать", type=types.InlineButtonTypeCopy(copy_text="UQ123")),
            types.KeyboardInlineButton(text="Бинарные", type=types.InlineButtonTypeCallback(data=b"\x00\xff\x10")),
        ]),
    ])
    d = B.describe_markup(markup)
    assert d["type"] == "inline"
    flat = B.flat(d)
    assert [b["kind"] for b in flat] == ["webapp", "callback", "url", "copy", "callback"]
    assert flat[0]["url"] == "https://app.cr.bot/"
    assert flat[1]["data"] == "p2p:menu" and flat[1]["data_hex"] == b"p2p:menu".hex()
    assert flat[3]["copy_text"] == "UQ123"
    assert "data" not in flat[4] and flat[4]["data_hex"] == "00ff10"
    rows = B.render_rows(d)
    assert rows[0].startswith("[1] Кошелёк ⧉мини-апп") and "[2] P2P" in rows[0]
    assert rows[1].startswith("[3] Канал 🔗")


def test_new_schema_reply_keyboard():
    markup = types.ReplyKeyboardMarkup(rows=[types.KeyboardButtonRow(buttons=[
        types.KeyboardButton(text="Купить", type=types.ButtonTypeDefault()),
        types.KeyboardButton(text="Магазин", type=types.ButtonTypeSimpleWebView(url="https://shop/")),
        types.KeyboardButton(text="Телефон", type=types.ButtonTypeRequestPhone()),
    ])], resize=True, placeholder="Сумма")
    d = B.describe_markup(markup)
    assert d["type"] == "reply" and d["placeholder"] == "Сумма"
    assert [b["kind"] for b in B.flat(d)] == ["text", "simple_webapp", "request_phone"]


def test_old_schema_buttons():
    btns = [old_style("KeyboardButtonCallback", text="Цена", data=b"price", requires_password=False),
            old_style("KeyboardButtonWebView", text="Биржа", url="https://app.cr.bot/exchange"),
            old_style("KeyboardButton", text="Меню")]
    kinds = [B.describe_button(b) for b in btns]
    assert kinds[0] == {"text": "Цена", "kind": "callback", "data_hex": b"price".hex(), "data": "price"}
    assert kinds[1]["kind"] == "webapp" and kinds[1]["url"].endswith("/exchange")
    assert kinds[2] == {"text": "Меню", "kind": "text"}


def test_hide_and_force_reply_and_none():
    assert B.describe_markup(None) is None
    assert B.describe_markup(types.ReplyKeyboardHide()) == {"type": "hide"}
    assert B.describe_markup(types.ReplyKeyboardForceReply(placeholder="x"))["type"] == "force_reply"
    assert B.flat(None) == [] and B.render_rows(None) == []
