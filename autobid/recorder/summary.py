"""summary.txt по events.jsonl записи мини-приложения: хронология кликов и запросов, список эндпоинтов."""
import json
import os
import re
from collections import OrderedDict
from urllib.parse import urlsplit

from .common import human_size, read_events

API_TYPES = {"xhr", "fetch", "eventsource", "other", "document"}
QUIET_TG = {"web_app_ready", "web_app_expand"}


def path_template(url):
    """'https://app.cr.bot/internal/v1/p2p/offers/123?x=1' → 'app.cr.bot/internal/v1/p2p/offers/{id}'"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    segs = []
    for s in parts.path.split("/"):
        if re.fullmatch(r"\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|"
                        r"[0-9a-fA-F]{16,}|[A-Za-z0-9_-]{28,}", s):
            s = "{id}"
        segs.append(s)
    return parts.netloc + "/".join(segs)


def short_url(url, n=110):
    s = re.sub(r"^https?://", "", url or "")
    return s if len(s) <= n else s[:n - 1] + "…"


def preview(body, n=160):
    if body is None or body == "":
        return ""
    s = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


def _duration(sec):
    sec = int(sec)
    return f"{sec // 60} мин {sec % 60} с" if sec >= 60 else f"{sec} с"


def build_summary(events):
    start = next((e for e in events if e["kind"] == "start"), {})
    stop = next((e for e in reversed(events) if e["kind"] == "stop"), {})
    responses = {e["id"]: e for e in events if e["kind"] == "response"}
    failed = {e["id"]: e for e in events if e["kind"] == "request_failed"}
    lines = [f"Запись мини-приложения: {start.get('bot_title', '')} (@{start.get('bot', '')}), {start.get('what', '')}",
             f"Аккаунт: {start.get('account', '')} · платформа: {start.get('platform', '')} · "
             f"браузер: {start.get('browser', '')}"]
    if events:
        lines.append(f"Начало: {events[0]['t']} · длительность: {_duration(events[-1]['ts'] - events[0]['ts'])}")
    lines += ["Ссылка запуска: " + start.get("url", ""), "",
              "Секреты (initData, cookies, токены, пароли, номера карт) в записи заменены на ***.",
              "Всё по порядку — в events.jsonl, файлы страниц и скрипты — в assets/.", "",
              "ХРОНОЛОГИЯ", "=========="]

    endpoints = OrderedDict()
    for e in events:
        k, t = e["kind"], e["t"]
        if k == "click":
            what = e.get("text") or e.get("aria") or e.get("testid") or e.get("path") or "?"
            extra = f" (внутри: «{e['inner']}»)" if e.get("inner") else ""
            extra += f" (рядом: «{e['around']}»)" if e.get("around") else ""
            lines.append(f"{t}  👆 клик «{what}» <{e.get('tag', '')}>{extra}")
        elif k == "input":
            lines.append(f"{t}  ⌨  поле «{e.get('label') or e.get('name') or e.get('path')}» = {e.get('value')!r}"
                         + (f" (отмечено: {e['checked']})" if "checked" in e else ""))
        elif k == "key":
            lines.append(f"{t}  ⏎  Enter в поле «{e.get('label', '')}»")
        elif k == "submit":
            lines.append(f"{t}  📨 отправка формы {e.get('path', '')}")
        elif k == "navigate":
            lines.append(f"{t}  🌐 адрес страницы: {short_url(e.get('url'), 160)}")
        elif k == "tg_ui":
            lines.append(f"{t}  🔘 Telegram: {e.get('what')} «{e.get('text', '')}»")
        elif k == "tg" and e.get("method") not in QUIET_TG:
            lines.append(f"{t}  📱 Telegram API: {e.get('method')} {preview(e.get('params'), 120)}")
        elif k == "tg_custom_method":
            res = f" → {preview(e['result'], 80)}" if "result" in e else f" → ошибка {e.get('error')}" if e.get("error") else ""
            lines.append(f"{t}  ☁  CloudStorage/{e.get('method')} {preview(e.get('params'), 100)}{res}")
        elif k == "dialog":
            lines.append(f"{t}  💬 окно браузера ({e.get('type')}): {e.get('message')}")
        elif k == "request" and e.get("type") in API_TYPES:
            r = responses.get(e["id"])
            status = r.get("status") if r else ("ошибка: " + failed[e["id"]].get("failure", "")) if e["id"] in failed else "—"
            size = human_size(r.get("size")) if r and r.get("size") is not None else ""
            lines.append(f"{t}  → {e['method']:<6} {short_url(e['url'])}  [{status}] {size}")
            if e.get("body") not in (None, ""):
                lines.append(f"{'':12}    отправлено: {preview(e['body'])}")
            if r and r.get("body") not in (None, ""):
                lines.append(f"{'':12}    ответ: {preview(r['body'])}")
            key = (e["method"], path_template(e["url"]))
            ep = endpoints.setdefault(key, {"n": 0, "status": set()})
            ep["n"] += 1
            if r:
                ep["status"].add(str(r.get("status")))
        elif k == "ws_open":
            lines.append(f"{t}  🔌 WebSocket #{e.get('ws')}: {short_url(e.get('url'))}")
        elif k == "ws_sent":
            lines.append(f"{t}  ⇢  WS #{e.get('ws')} отправлено: {preview(e.get('data'))}")
        elif k == "ws_recv" and e.get("n") == 1:
            lines.append(f"{t}  ⇠  WS #{e.get('ws')} первый входящий кадр: {preview(e.get('data'))}")
        elif k in ("console", "pageerror", "shim_error"):
            lines.append(f"{t}  ⚠  {k}: {preview(e.get('text') or e.get('error'), 200)}")

    lines += ["", "ЭНДПОИНТЫ (запросы приложения к серверу)", "========================================"]
    for (method, path), ep in endpoints.items():
        lines.append(f"  {method:<6} {path}  ×{ep['n']}  [{', '.join(sorted(ep['status'])) or '—'}]")
    if not endpoints:
        lines.append("  (запросов не было)")

    ws = stop.get("ws") or {}
    if ws:
        lines += ["", "WEBSOCKET", "========="]
        for wid, st in ws.items():
            note = ""
            if st.get("recv", 0) > st.get("logged_recv", 0):
                note = f" — записано {st.get('logged_recv')} входящих (первые 200, дальше каждый 25-й)"
            lines.append(f"  #{wid} {short_url(st.get('url'))}: отправлено {st.get('sent', 0)}, "
                         f"получено {st.get('recv', 0)}{note}")
    skipped = stop.get("skipped") or {}
    if skipped:
        lines += ["", "Не записывались (картинки, шрифты, стили): " +
                  ", ".join(f"{k} ×{v}" for k, v in sorted(skipped.items()))]
    if stop.get("assets_skipped"):
        lines.append(f"Файлов не сохранено из-за лимита размера: {stop['assets_skipped']}")
    return "\n".join(lines) + "\n"


def write_summary(out_dir):
    events = read_events(os.path.join(out_dir, "events.jsonl"))
    text = build_summary(events)
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return text
