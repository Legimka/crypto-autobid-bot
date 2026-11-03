"""
Кнопки сообщений бота в единый вид — для любых версий Telethon.

Telethon до 1.44 (слой TL < 229): кнопка — это класс KeyboardButtonCallback / KeyboardButtonWebView / …
Telethon 1.45 (слой 229): кнопка — KeyboardInlineButton(text, type=InlineButtonTypeCallback(...)) или
KeyboardButton(text, type=ButtonType...). Тип определяем по имени класса — без импорта конкретных классов.
"""

KINDS = {
    # старая схема              # новая схема (поле type)
    "KeyboardButtonCallback": "callback", "InlineButtonTypeCallback": "callback",
    "KeyboardButtonUrl": "url", "InlineButtonTypeUrl": "url",
    "KeyboardButtonWebView": "webapp", "InlineButtonTypeWebView": "webapp",
    "KeyboardButtonSimpleWebView": "simple_webapp", "ButtonTypeSimpleWebView": "simple_webapp",
    "KeyboardButtonCopy": "copy", "InlineButtonTypeCopy": "copy",
    "KeyboardButtonSwitchInline": "switch_inline", "InlineButtonTypeSwitchInline": "switch_inline",
    "KeyboardButtonGame": "game", "InlineButtonTypeGame": "game",
    "KeyboardButtonBuy": "buy", "InlineButtonTypeBuy": "buy",
    "KeyboardButtonUrlAuth": "url_auth", "InputKeyboardButtonUrlAuth": "url_auth",
    "InlineButtonTypeUrlAuth": "url_auth", "InputInlineButtonTypeUrlAuth": "url_auth",
    "KeyboardButtonUserProfile": "user_profile", "InputKeyboardButtonUserProfile": "user_profile",
    "InlineButtonTypeUserProfile": "user_profile", "InputInlineButtonTypeUserProfile": "user_profile",
    "InlineButtonTypeDisabled": "disabled",
    "KeyboardButton": "text", "ButtonTypeDefault": "text",
    "KeyboardButtonRequestPhone": "request_phone", "ButtonTypeRequestPhone": "request_phone",
    "KeyboardButtonRequestGeoLocation": "request_geo", "ButtonTypeRequestGeoLocation": "request_geo",
    "KeyboardButtonRequestPeer": "request_peer", "InputKeyboardButtonRequestPeer": "request_peer",
    "ButtonTypeRequestPeer": "request_peer", "InputButtonTypeRequestPeer": "request_peer",
    "KeyboardButtonRequestPoll": "request_poll", "ButtonTypeRequestPoll": "request_poll",
}

# Подписи для текстового журнала
MARKS = {"callback": "", "url": " 🔗", "webapp": " ⧉мини-апп", "simple_webapp": " ⧉мини-апп", "copy": " 📋",
         "switch_inline": " ↪", "game": " 🎮", "buy": " 💳", "url_auth": " 🔑", "user_profile": " 👤",
         "disabled": " ⛔", "text": "", "request_phone": " 📞", "request_geo": " 📍", "request_peer": " 👥",
         "request_poll": " 📊"}


def _decode_data(data):
    if data is None:
        return {}
    if isinstance(data, str):
        data = data.encode()
    out = {"data_hex": data.hex()}
    try:
        text = data.decode("utf-8")
        if text.isprintable():
            out["data"] = text
    except UnicodeDecodeError:
        pass
    return out


def describe_button(btn):
    """{'text', 'kind', и по типу: 'data'/'data_hex', 'url', 'copy_text', 'query', 'requires_password'}"""
    t = getattr(btn, "type", None)
    raw = t if t is not None and not isinstance(t, (str, int)) and type(t).__name__ in KINDS else btn
    name = type(raw).__name__
    kind = KINDS.get(name, name)
    d = {"text": getattr(btn, "text", ""), "kind": kind}
    if kind == "callback":
        d.update(_decode_data(getattr(raw, "data", None)))
        if getattr(raw, "requires_password", None):
            d["requires_password"] = True
    for attr in ("url", "copy_text", "query"):
        val = getattr(raw, attr, None)
        if val not in (None, ""):
            d[attr] = val
    return d


def describe_markup(markup):
    """reply_markup сообщения → {'type': 'inline'|'reply'|'hide'|'force_reply', 'rows': [[кнопка, …], …]}"""
    if markup is None:
        return None
    name = type(markup).__name__
    if name == "ReplyKeyboardHide":
        return {"type": "hide"}
    if name == "ReplyKeyboardForceReply":
        return {"type": "force_reply", "placeholder": getattr(markup, "placeholder", None)}
    rows = []
    for row in getattr(markup, "rows", None) or []:
        rows.append([describe_button(b) for b in (getattr(row, "buttons", None) or [])])
    out = {"type": "inline" if name == "ReplyInlineMarkup" else "reply", "rows": rows}
    if getattr(markup, "placeholder", None):
        out["placeholder"] = markup.placeholder
    return out


def flat(markup_desc):
    """Кнопки по порядку (номера с 1, слева направо, сверху вниз)."""
    if not markup_desc:
        return []
    return [b for row in markup_desc.get("rows", []) for b in row]


def render_rows(markup_desc, width=100):
    """Текстовые строки клавиатуры с номерами: '[1] Кошелёк ⧉мини-апп   [2] Обмен ⧉мини-апп'"""
    lines, n = [], 0
    for row in (markup_desc or {}).get("rows", []):
        cells = []
        for b in row:
            n += 1
            cells.append(f"[{n}] {b['text']}{MARKS.get(b['kind'], ' (' + b['kind'] + ')')}")
        lines.append("   ".join(cells)[:width * 2])
    return lines
