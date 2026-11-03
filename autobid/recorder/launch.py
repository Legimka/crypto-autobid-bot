"""
Ссылка запуска мини-приложения от Telegram для конкретного аккаунта — с подписанным initData,
ровно как её получает приложение Telegram, когда ты нажимаешь кнопку.

Как можно открыть приложение (так же, как это делает Telegram):
  main    — главное мини-приложение бота (кнопка «Открыть» в профиле бота, ссылки t.me/<бот>?startapp)
  button  — кнопка мини-приложения в сообщении бота (например, «Биржа» в меню CryptoBot)
  menu    — кнопка меню слева от поля ввода
  app     — именованное мини-приложение t.me/<бот>/<имя>
  url     — готовая ссылка (если в ней нет tgWebAppData — запрашиваем её как для кнопки)
"""
import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qsl, quote

from telethon import errors, functions, types, utils

from . import buttons as B

DEFAULT_PLATFORM = "android"
DEFAULT_VERSION = "8.0"
# Тёмная тема Telegram для Android — приложения красят себя по ней
THEME = {
    "bg_color": "#212d3b", "text_color": "#ffffff", "hint_color": "#7d7f81", "link_color": "#5eabe1",
    "button_color": "#50a8eb", "button_text_color": "#ffffff", "secondary_bg_color": "#1d2733",
    "header_bg_color": "#242d39", "accent_text_color": "#64b5ef", "section_bg_color": "#17212b",
    "section_header_text_color": "#79c4fc", "subtitle_text_color": "#7b8790",
    "destructive_text_color": "#ee686f", "bottom_bar_bg_color": "#1d2733", "section_separator_color": "#111921",
}


class LaunchError(RuntimeError):
    pass


@dataclass
class Launch:
    url: str
    mode: str
    title: str = ""
    query_id: Optional[int] = None
    button: Optional[dict] = field(default=None)
    bot: Optional[object] = None          # бот-владелец приложения: у прямой ссылки он может быть не текущим
    bot_username: Optional[str] = None


def theme_json():
    return types.DataJSON(data=json.dumps(THEME))


def has_init_data(url):
    return "tgWebAppData=" in url


def normalize_launch_url(url, platform=DEFAULT_PLATFORM, version=DEFAULT_VERSION, theme=None):
    """Дописать во #фрагмент недостающие tgWebAppVersion / tgWebAppPlatform / tgWebAppThemeParams:
    без них SDK мини-приложений (особенно @telegram-apps/sdk) падают на старте. Существующее не трогаем."""
    base, _, frag = url.partition("#")
    keys = {p.split("=", 1)[0] for p in frag.split("&") if p}
    add = []
    if "tgWebAppVersion" not in keys:
        add.append("tgWebAppVersion=" + quote(version))
    if "tgWebAppPlatform" not in keys:
        add.append("tgWebAppPlatform=" + quote(platform))
    if "tgWebAppThemeParams" not in keys:
        add.append("tgWebAppThemeParams=" + quote(json.dumps(theme or THEME, separators=(",", ":"))))
    if not add:
        return url
    return base + "#" + "&".join([frag] + add if frag else add)


_DIRECT = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?P<bot>[A-Za-z][A-Za-z0-9_]{3,})"
                     r"(?:/(?P<app>[A-Za-z0-9_]+))?/?(?:\?(?P<q>[^#]*))?$", re.I)
# Служебные пути t.me — это не боты с мини-приложениями (t.me/share/url, t.me/joinchat/… и т.п.)
_RESERVED = {"share", "addstickers", "addemoji", "addtheme", "addlist", "joinchat", "boost", "invoice",
             "giftcode", "setlanguage", "login", "proxy", "socks", "contact", "confirmphone", "bg", "iv", "nft",
             "c", "s", "a", "m", "k"}


def parse_direct_link(url):
    """Прямая ссылка на мини-приложение: t.me/<бот>/<приложение>?startapp=… или t.me/<бот>?startapp=…
    (а также tg://resolve?domain=…&appname=…&startapp=…). Возвращает {'bot', 'app', 'start_param'} или None."""
    url = (url or "").strip()
    if url.lower().startswith("tg://resolve?"):
        q = dict(parse_qsl(url.split("?", 1)[1], keep_blank_values=True))
        dom = q.get("domain")
        if dom and dom.lower() not in _RESERVED and (q.get("appname") or "startapp" in q):
            return {"bot": dom, "app": q.get("appname") or None, "start_param": q.get("startapp") or None}
        return None
    m = _DIRECT.match(url)
    if not m or m.group("bot").lower() in _RESERVED:
        return None
    q = dict(parse_qsl(m.group("q") or "", keep_blank_values=True))
    app = m.group("app")
    if app and not app.isdigit():      # t.me/канал/123 — это ссылка на пост, не на приложение
        return {"bot": m.group("bot"), "app": app, "start_param": q.get("startapp") or None}
    if not app and "startapp" in q:
        return {"bot": m.group("bot"), "app": None, "start_param": q.get("startapp") or None}
    return None


def _norm(text):
    return re.sub(r"[\W_]+", " ", (text or "").lower()).strip()


def match_button(found, text):
    """found — [(msg, номер, кнопка)]. Сначала точное совпадение (без эмодзи и регистра), потом вхождение."""
    want = _norm(text)
    exact = [f for f in found if _norm(f[2]["text"]) == want]
    if exact:
        return exact[0]
    part = [f for f in found if want and want in _norm(f[2]["text"])]
    return part[0] if part else None


async def find_webapp_buttons(client, bot, limit=30):
    """Кнопки мини-приложений в последних сообщениях бота, новые сообщения первыми.
    Считаются и кнопки-ссылки на мини-приложения (t.me/<бот>/<приложение>, ?startapp=…)."""
    found = []
    for msg in await client.get_messages(bot, limit=limit):
        if getattr(msg, "out", False):
            continue
        for i, b in enumerate(B.flat(B.describe_markup(getattr(msg, "reply_markup", None))), 1):
            if b["kind"] in ("webapp", "simple_webapp") and b.get("url"):
                found.append((msg, i, b))
            elif b["kind"] == "url" and parse_direct_link(b.get("url")):
                found.append((msg, i, dict(b, direct=parse_direct_link(b["url"]))))
    return found


async def _launch_direct(client, link, platform, title, button=None):
    """Открыть мини-приложение по прямой ссылке (её бот может отличаться от текущего)."""
    try:
        peer = await client.get_input_entity(link["bot"])
    except (ValueError, errors.RPCError) as e:
        raise LaunchError(f"по ссылке не нашёл бота @{link['bot']}") from e
    try:
        if link["app"]:
            res = await client(functions.messages.RequestAppWebViewRequest(
                peer=peer, app=types.InputBotAppShortName(bot_id=utils.get_input_user(peer), short_name=link["app"]),
                platform=platform, start_param=link["start_param"], theme_params=theme_json()))
        else:
            res = await client(functions.messages.RequestMainWebViewRequest(
                peer=peer, bot=peer, platform=platform, start_param=link["start_param"], theme_params=theme_json()))
    except TypeError as e:            # ссылка ведёт на канал или чат, а не на бота
        raise LaunchError(f"по ссылке @{link['bot']} это не бот") from e
    return Launch(res.url, "direct", title, getattr(res, "query_id", None), button,
                  bot=peer, bot_username=link["bot"])


async def send_start_and_wait(client, bot, timeout=15.0):
    """/start боту и ждём сообщение с кнопками (главное меню)."""
    last = await client.get_messages(bot, limit=1)
    last_id = last[0].id if last else 0
    await client.send_message(bot, "/start")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(1.0)
        for m in await client.get_messages(bot, limit=5, min_id=last_id):
            if not m.out and getattr(m, "reply_markup", None):
                return True
    return False


async def menu_button(client, bot):
    full = await client(functions.users.GetFullUserRequest(id=bot))
    menu = getattr(getattr(full.full_user, "bot_info", None), "menu_button", None)
    return menu if type(menu).__name__ == "BotMenuButton" else None


async def list_options(client, bot, send_start=True, echo=print):
    """Что можно открыть у этого бота: [(подпись, параметры для request_launch)]."""
    options = [("главное мини-приложение бота", {"mode": "main"})]
    try:
        menu = await menu_button(client, bot)
        if menu:
            options.append((f"кнопка меню «{menu.text}»", {"mode": "menu"}))
    except errors.RPCError:
        pass
    found = await find_webapp_buttons(client, bot)
    if not found and send_start:
        echo("В последних сообщениях бота нет кнопок мини-приложений — отправляю /start")
        await send_start_and_wait(client, bot)
        found = await find_webapp_buttons(client, bot)
    seen = set()
    for _, _, b in found:
        if b["text"] not in seen:
            seen.add(b["text"])
            options.append((f"кнопка «{b['text']}»", {"mode": "button", "button": b["text"]}))
    return options


async def request_launch(client, bot, mode="main", button=None, url=None, app=None, start_param=None,
                         platform=DEFAULT_PLATFORM, send_start=True, echo=print):
    """Запросить у Telegram ссылку запуска. bot — InputPeerUser бота. Возвращает Launch."""
    try:
        if mode == "main":
            res = await client(functions.messages.RequestMainWebViewRequest(
                peer=bot, bot=bot, platform=platform, start_param=start_param, theme_params=theme_json()))
            return Launch(res.url, "main", "главное мини-приложение", getattr(res, "query_id", None))

        if mode == "button":
            found = await find_webapp_buttons(client, bot)
            hit = match_button(found, button)
            if not hit and send_start:
                echo(f"Кнопки «{button}» в последних сообщениях бота нет — отправляю /start")
                await send_start_and_wait(client, bot)
                found = await find_webapp_buttons(client, bot)
                hit = match_button(found, button)
            if not hit:
                names = sorted({f[2]["text"] for f in found})
                raise LaunchError(f"Не нашёл кнопку мини-приложения «{button}». "
                                  f"Кнопки мини-приложений у бота: {', '.join(names) or 'нет ни одной'}")
            _, _, b = hit
            if b.get("direct"):
                return await _launch_direct(client, b["direct"], platform, f"кнопка «{b['text']}»", b)
            if b["kind"] == "simple_webapp":
                res = await client(functions.messages.RequestSimpleWebViewRequest(
                    bot=bot, platform=platform, url=b["url"], theme_params=theme_json()))
            else:
                res = await client(functions.messages.RequestWebViewRequest(
                    peer=bot, bot=bot, platform=platform, url=b["url"], theme_params=theme_json()))
            return Launch(res.url, "button", f"кнопка «{b['text']}»", getattr(res, "query_id", None), b)

        if mode == "menu":
            menu = await menu_button(client, bot)
            if not menu:
                raise LaunchError("У бота нет кнопки меню с мини-приложением")
            res = await client(functions.messages.RequestWebViewRequest(
                peer=bot, bot=bot, platform=platform, url=menu.url, from_bot_menu=True, theme_params=theme_json()))
            return Launch(res.url, "menu", f"кнопка меню «{menu.text}»", getattr(res, "query_id", None))

        if mode == "app":
            res = await client(functions.messages.RequestAppWebViewRequest(
                peer=bot, app=types.InputBotAppShortName(bot_id=utils.get_input_user(bot), short_name=app),
                platform=platform, start_param=start_param, theme_params=theme_json()))
            return Launch(res.url, "app", f"приложение «{app}»")

        if mode == "url":
            if has_init_data(url):
                return Launch(url, "url", "готовая ссылка")
            if parse_direct_link(url):
                return await _launch_direct(client, parse_direct_link(url), platform, f"ссылка {url}")
            res = await client(functions.messages.RequestWebViewRequest(
                peer=bot, bot=bot, platform=platform, url=url, theme_params=theme_json()))
            return Launch(res.url, "url", "ссылка", getattr(res, "query_id", None))
    except errors.RPCError as e:
        raise LaunchError(f"Telegram не выдал ссылку мини-приложения ({mode}): {type(e).__name__}: {e}") from e
    raise LaunchError(f"неизвестный режим запуска: {mode}")


async def prolong_loop(client, bot, query_id, on_error=None, every=50):
    """Пока окно открыто, Telegram-клиент раз в минуту продлевает запуск (если у него есть query_id)."""
    while True:
        await asyncio.sleep(every)
        try:
            await client(functions.messages.ProlongWebViewRequest(peer=bot, bot=bot, query_id=query_id))
        except Exception as e:   # не критично для записи
            if on_error:
                on_error(e)
            return
