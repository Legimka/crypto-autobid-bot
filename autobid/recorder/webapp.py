"""
Запись мини-приложения бота (Биржа CryptoBot, xRocket, …).

Telegram выдаёт ссылку запуска для твоего аккаунта, рекордер открывает её в окне браузера (Edge или Chrome
на твоём компьютере) и записывает:
  • каждый запрос приложения к серверу — адрес, тело, ответ; WebSocket-кадры;
  • твои клики, ввод в поля, нажатия кнопок Telegram (MainButton, «Назад», всплывающие окна);
  • файлы страницы и скрипты приложения (assets/) — по ним видно все запросы, которые приложение умеет делать.
Telegram в браузере эмулирует webview_shim.js. CloudStorage приложения идёт в настоящий Telegram.

⚠ Это настоящий аккаунт: заявки, объявления и переводы в окне делаются по-настоящему.
Запись: recordings/<время>_webapp_<бот>/ — summary.txt (читать), events.jsonl (всё по порядку), assets/.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import traceback
from urllib.parse import unquote, urlsplit

from .. import accounts, bots, redact, settings
from . import launch as L
from .common import STOP_WORDS, ConsoleLines, EventLog, make_recording_dir, utf8_console
from .summary import write_summary

SHIM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webview_shim.js")
# Как WebView Telegram на Android (тот же, что у клиента API в autobid/cryptobot/app_api.py)
ANDROID_UA = ("Mozilla/5.0 (Linux; Android 14; SM-S918B Build/UP1A.231005.007; wv) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Version/4.0 Chrome/129.0.6668.100 Mobile Safari/537.36")
VIEWPORT = {"width": 412, "height": 860}

SKIP_TYPES = {"image", "media", "font", "stylesheet", "texttrack", "manifest"}
ASSET_TYPES = {"script", "document"}
MAX_BODY = 256 * 1024             # тело запроса/ответа в журнале
MAX_ASSETS = 80 * 1024 * 1024     # всего на файлы страниц и скрипты
WS_FULL = 200                     # входящих кадров WebSocket пишем полностью, дальше — каждый WS_EVERY-й
WS_EVERY = 25
WS_BINARY_KEEP = 4096             # байт двоичного входящего кадра (base64)
MAX_TRACKED = 20000               # запросов в памяти для связи «запрос → ответ» (длинные записи)
REQ_HEADERS_DROP = {"accept-encoding", "user-agent", "accept-language", "upgrade-insecure-requests", "pragma",
                    "cache-control", "priority", "dnt", "connection", "accept"}
RESP_HEADERS_KEEP = {"content-type", "content-length", "location", "set-cookie", "retry-after", "x-request-id"}
TEXT_TYPES = ("json", "text", "javascript", "xml", "x-www-form-urlencoded", "event-stream", "graphql")
ASSET_CTYPES = ("javascript", "ecmascript", "html")   # сохраняем как файл приложения, не кладём тело в журнал
_WS_SECRET = re.compile(rb"(?i)token|secret|passw|session|cookie|init_?data|eyJ")


def _truncate(text):
    if text is None or len(text) <= MAX_BODY:
        return text
    return text[:MAX_BODY] + f"…(обрезано, всего {len(text)} символов)"


def mask_storage_value(key, value, secure=False):
    """Значения хранилищ приложения (CloudStorage, DeviceStorage): там бывают токены сессии и PIN-коды."""
    if value is None:
        return None
    if secure or redact.key_rule(key or "") in ("strong", "suffix", "weak"):
        return redact.MASK
    v = redact.redact_value(key, value)
    if v != value or not isinstance(value, str):
        return v
    low = value.strip().lower()
    # Очевидно безобидное оставляем: флаги, язык/тема (ru, en-US, dark), фразы с пробелами.
    if low in ("", "true", "false", "null", "0", "1") or re.fullmatch(r"[a-z]{2,8}(?:[-_][a-z]{2,4})?", low) \
            or re.search(r"\s", value):
        return value
    return f"{redact.MASK} ({len(value)} симв.)"   # короткий непрозрачный токен/PIN — тоже скрываем


def sanitize_tg(method, params):
    """Параметры вызова Telegram API из приложения — без значений из хранилищ."""
    if not isinstance(params, dict):
        return params
    p = dict(params)
    if "storage" in method and "value" in p:
        p["value"] = mask_storage_value(p.get("key"), p["value"], secure="secure" in method)
    return p


def sanitize_custom(method, params, result=None):
    """CloudStorage: saveStorageValue {key, value}; getStorageValues → {key: value}."""
    params = dict(params or {}) if isinstance(params, dict) else params
    if isinstance(params, dict) and "value" in params:
        params["value"] = mask_storage_value(params.get("key"), params["value"])
    if isinstance(result, dict):
        result = {k: mask_storage_value(k, v) if isinstance(v, str) else v for k, v in result.items()}
    return params, result


def asset_relpath(url, ctype=""):
    """assets/<хост>/<путь> — без недопустимых в Windows символов."""
    u = urlsplit(url)
    path = u.path or "/"
    if path.endswith("/"):
        path += "index.html"
    name = re.sub(r'[<>:"|?*\\\x00-\x1f]', "_", (u.netloc + path).replace("..", "_"))
    if u.query:
        stem, ext = os.path.splitext(name)
        name = f"{stem}_{hashlib.sha1(u.query.encode()).hexdigest()[:8]}{ext}"
    if "." not in os.path.basename(name):
        name += ".html" if "html" in ctype else ".js" if "javascript" in ctype else ""
    parts = [p[:80] for p in name.split("/") if p]
    return os.path.join("assets", *parts)


class WebAppRecorder:
    def __init__(self, out_dir, *, platform=L.DEFAULT_PLATFORM, version=L.DEFAULT_VERSION, custom_method=None,
                 save_assets=True, echo=print):
        self.out_dir = out_dir
        self.log = EventLog(os.path.join(out_dir, "events.jsonl"))
        self.platform, self.version = platform, version
        self.custom_method = custom_method       # async (method, params) → result
        self.save_assets = save_assets
        self.echo = echo
        self.req_ids = {}
        self.n_req = 0
        self.skipped = {}
        self.saved = {}
        self.assets_bytes = 0
        self.assets_skipped = 0
        self.ws_stats = {}
        self.pages = set()
        self.closed = asyncio.Event()
        self.tasks = set()
        self.dialog_waiter = None
        self.app_page = None          # страница самого приложения; биндинги принимаем только из её главного фрейма
        self.learned = set()          # непрозрачные секреты из заголовков (Authorization, Cookie) — вычистить в конце
        self.opened = False           # приложение реально открылось (страница загрузилась)

    # ------------------------------------------------------------------ подключение к браузеру
    def shim_source(self):
        cfg = {"platform": self.platform, "version": self.version, "theme": L.THEME,
               "label": "Telegram (эмуляция) · идёт запись"}
        with open(SHIM_PATH, encoding="utf-8") as f:
            return f.read().replace("__AUTOBID_CFG__", json.dumps(cfg, ensure_ascii=False))

    async def attach(self, context):
        await context.expose_binding("__autobidRec", self._on_rec)
        await context.expose_binding("__autobidInvoke", self._on_invoke)
        await context.add_init_script(self.shim_source())
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfinished", lambda r: self._spawn(self._on_finished(r)))
        context.on("requestfailed", self._on_failed)
        context.on("page", self._on_page)
        context.on("close", lambda *_: self.closed.set())
        for page in context.pages:
            self._on_page(page)

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def drain(self, timeout=5.0):
        """Дождаться, пока допишутся тела последних ответов."""
        if self.tasks:
            await asyncio.wait(list(self.tasks), timeout=timeout)

    # ------------------------------------------------------------------ события страницы
    def _on_page(self, page):
        if page in self.pages:
            return
        self.pages.add(page)
        page.on("websocket", self._on_ws)
        page.on("framenavigated", lambda frame: self._on_nav(page, frame))
        page.on("console", self._on_console)
        page.on("pageerror", lambda err: self.log.write("pageerror", error=redact.redact_text_kv(str(err))[:2000]))
        page.on("dialog", lambda d: self._spawn(self._on_dialog(page, d)))
        page.on("close", lambda *_: self._on_page_close(page))

    def _on_page_close(self, page):
        self.pages.discard(page)
        if not self.pages:
            self.closed.set()

    def _on_nav(self, page, frame):
        if frame == page.main_frame:
            url = redact.redact_url(frame.url)
            self.log.write("navigate", url=url)

    def _on_console(self, msg):
        if msg.type in ("error", "warning"):
            self.log.write("console", level=msg.type, text=redact.redact_text_kv(msg.text)[:2000])

    async def _on_dialog(self, page, dialog):
        """alert/confirm/prompt приложения: alert закрываем сами, на confirm/prompt отвечаешь в консоли."""
        kind, message = dialog.type, redact.redact_str(dialog.message, aggressive=True)
        self.log.write("dialog", type=kind, message=message)
        try:
            if kind in ("alert", "beforeunload"):
                await dialog.accept()
                self.echo(f"💬 окно приложения: {message}")
                try:
                    await page.evaluate("m => window.__autobidToast && window.__autobidToast(m)", message)
                except Exception:
                    pass
                return
            self.echo(f"❓ Приложение спрашивает: «{message}»\n   Ответь здесь: да/y — подтвердить, "
                      "нет/n или Enter — отмена" + (", либо впиши текст и Enter" if kind == "prompt" else ""))
            yes, no = {"y", "yes", "да", "д", "ok", "ок"}, {"n", "no", "нет", "н", "отмена", "cancel"}
            while True:
                fut = asyncio.get_running_loop().create_future()
                self.dialog_waiter = fut
                try:
                    answer = await asyncio.wait_for(fut, 600)
                except asyncio.TimeoutError:
                    answer = ""
                finally:
                    self.dialog_waiter = None
                low = (answer or "").strip().lower()
                if kind == "prompt" and answer and low not in yes and low not in no and low not in STOP_WORDS:
                    await dialog.accept(answer)                 # для prompt непустой текст — это и есть ввод
                    result = "accepted:text"
                elif low in yes:
                    await dialog.accept(dialog.default_value or "")
                    result = "accepted"
                elif low in no or low == "" or low in STOP_WORDS:
                    await dialog.dismiss()
                    result = "dismissed"
                else:
                    self.echo("   не понял — напиши да или нет")
                    continue
                break
            self.log.write("dialog_answer", type=kind, result=result)
        except Exception as e:   # страница закрылась, пока ждали ответ
            self.log.write("dialog_answer", type=kind, error=str(e)[:200])

    def _is_app_main(self, page, frame):
        """Главный фрейм самой страницы приложения? (до её создания app_page=None — всё своё, принимаем)."""
        if self.app_page is None:
            return True
        return page is self.app_page and frame is getattr(page, "main_frame", None)

    def _on_rec(self, source, kind, payload=None):
        payload = payload if isinstance(payload, dict) else {"value": payload}
        frame = source.get("frame") if isinstance(source, dict) else None
        page = source.get("page") if isinstance(source, dict) else None
        # Из чужих фреймов/вкладок (сторонний iframe, окно от openLink) берём только действия пользователя,
        # а события Telegram/служебные (в т.ч. поддельный «stop») — нет.
        if not self._is_app_main(page, frame) and kind not in ("click", "input", "key", "submit"):
            return
        if kind == "tg":
            payload["params"] = sanitize_tg(payload.get("method", ""), payload.get("params"))
        if kind in ("click", "input", "key", "submit", "tg_ui"):
            payload = redact.redact_obj(payload, aggressive=True)
        try:
            if frame is not None and page is not None and frame != page.main_frame:
                payload["frame"] = redact.redact_url(frame.url)
        except Exception:
            pass
        self.log.write(kind, **payload)
        if kind == "click":
            self.echo(f"👆 клик «{payload.get('text') or payload.get('aria') or payload.get('path')}»")
        elif kind == "input":
            self.echo(f"⌨  поле «{payload.get('label')}» = {payload.get('value')!r}")
        elif kind == "tg_ui":
            self.echo(f"🔘 Telegram: {payload.get('what')} «{payload.get('text')}»")

    async def _on_invoke(self, source, what, params=None):
        page = source.get("page") if isinstance(source, dict) else None
        frame = source.get("frame") if isinstance(source, dict) else None
        if not self._is_app_main(page, frame):
            # вызов из чужого фрейма/вкладки: иначе сторонний iframe прочитал бы CloudStorage реального аккаунта
            self.log.write("tg_custom_method", error="FORBIDDEN_FRAME")
            return {"error": "FORBIDDEN"}
        if what != "custom_method" or not isinstance(params, dict):
            return {"error": "UNSUPPORTED"}
        method, args = params.get("method"), params.get("params")
        logged, _ = sanitize_custom(method, args)
        if not self.custom_method:
            self.log.write("tg_custom_method", method=method, params=logged, error="UNSUPPORTED")
            return {"error": "UNSUPPORTED"}
        try:
            result = await self.custom_method(method, args)
        except Exception as e:
            self.log.write("tg_custom_method", method=method, params=logged, error=str(e)[:300])
            return {"error": str(e)[:300]}
        _, shown = sanitize_custom(method, args, result)
        self.log.write("tg_custom_method", method=method, params=logged, result=shown)
        return {"result": result}

    # ------------------------------------------------------------------ секреты из заголовков
    def _add_secret(self, v):
        v = (v or "").strip()
        if len(v) >= 12 and redact.MASK not in v and not re.search(r"\s", v):
            self.learned.add(v)

    def _learn(self, headers):
        """Запомнить непрозрачные секреты из заголовков, чтобы вычистить их из всей записи в конце
        (тот же токен часто возвращается в теле ответа под безобидным ключом result/data)."""
        for name, value in (headers or {}).items():
            k = name.lower()
            if not value:
                continue
            if k in ("authorization", "proxy-authorization"):
                self._add_secret(value.split(" ", 1)[1] if " " in value else value)
            elif k in ("cookie", "set-cookie"):
                for part in value.replace("\n", ";").split(";"):
                    _, sep, val = part.strip().partition("=")
                    if sep:
                        self._add_secret(val)
            elif redact.key_rule(k) in ("strong", "suffix", "weak") or any(p in k for p in ("auth", "session", "access")):
                self._add_secret(value)

    # ------------------------------------------------------------------ сеть
    def _on_request(self, request):
        rtype = request.resource_type
        if rtype in SKIP_TYPES:
            self.skipped[rtype] = self.skipped.get(rtype, 0) + 1
            return
        try:
            self._learn(request.headers)
        except Exception:
            pass
        self.n_req += 1
        rid = self.req_ids[request] = self.n_req
        if len(self.req_ids) > MAX_TRACKED:      # запись на часы: старые запросы давно завершились
            for old in list(self.req_ids)[:MAX_TRACKED // 4]:
                del self.req_ids[old]
        entry = {"id": rid, "method": request.method, "url": redact.redact_url(request.url), "type": rtype}
        try:
            if request.redirected_from is not None and request.redirected_from in self.req_ids:
                entry["redirected_from"] = self.req_ids[request.redirected_from]
        except Exception:
            pass
        # Тело и заголовки нужны для обычных запросов и для любого не-GET (в т.ч. POST-навигации формой).
        if rtype not in ASSET_TYPES or request.method != "GET":
            headers = {k: v for k, v in (request.headers or {}).items()
                       if k.lower() not in REQ_HEADERS_DROP and not k.lower().startswith("sec-fetch")}
            entry["headers"] = redact.redact_headers(headers)
            body = None
            try:
                body = request.post_data
            except Exception:
                try:
                    body = f"<двоичные данные {len(request.post_data_buffer or b'')} байт>"
                except Exception:
                    body = "<тело не прочиталось>"
            if body:
                as_json, as_text = redact.redact_body(body, headers.get("content-type", ""))
                entry["body"] = as_json if as_json is not None else _truncate(as_text)
        self.log.write("request", **entry)

    def _on_response(self, response):
        """Потоковые ответы (event-stream) никогда не доходят до requestfinished — ловим их статус здесь."""
        req = response.request
        if req.resource_type != "eventsource" or req not in self.req_ids:
            return
        try:
            self._learn(response.headers)
        except Exception:
            pass
        self.log.write("response", id=self.req_ids[req], status=response.status,
                       url=redact.redact_url(req.url),
                       headers=redact.redact_headers(response.headers or {}, keep=RESP_HEADERS_KEEP),
                       note="поток event-stream — тело не сохраняется")

    def _on_failed(self, request):
        rid = self.req_ids.get(request)
        if rid is not None:
            self.log.write("request_failed", id=rid, failure=str(request.failure or "")[:300])

    async def _on_finished(self, request):
        rid = self.req_ids.get(request)
        if rid is None:
            return
        try:
            resp = await request.response()
        except Exception:
            resp = None
        if resp is None:
            return
        rtype = request.resource_type
        entry = {"id": rid, "status": resp.status, "url": redact.redact_url(request.url)}
        try:
            headers = await resp.all_headers()
        except Exception:
            headers = dict(resp.headers or {})
        ctype = headers.get("content-type", "")
        try:
            self._learn(headers)
        except Exception:
            pass
        entry["headers"] = redact.redact_headers(headers, keep=RESP_HEADERS_KEEP)
        try:
            timing = request.timing or {}
            if timing.get("responseEnd", -1) > 0:
                entry["ms"] = round(timing["responseEnd"])
        except Exception:
            pass
        if 300 <= resp.status < 400:
            self.log.write("response", **entry)
            return
        try:
            body = await resp.body()
        except Exception as e:
            entry["body_error"] = str(e).splitlines()[0][:200]
            self.log.write("response", **entry)
            return
        entry["size"] = len(body)
        # JS-чанки с предзагрузкой приходят типом 'other' — тоже сохраняем как файл, а не пишем тело в журнал
        if rtype in ASSET_TYPES or any(t in ctype for t in ASSET_CTYPES):
            if self.save_assets:
                entry["saved"] = self._save_asset(request.url, body, ctype, rtype)
        elif any(t in ctype for t in TEXT_TYPES) or not ctype:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is None:
                entry["body"] = f"<двоичные данные {len(body)} байт>"
            else:
                as_json, as_text = redact.redact_body(text, ctype)
                if as_json is not None:
                    dumped = json.dumps(as_json, ensure_ascii=False)
                    entry["body"] = as_json if len(dumped) <= MAX_BODY else _truncate(dumped)
                else:
                    entry["body"] = _truncate(as_text)
        else:
            entry["body"] = f"<{ctype or 'двоичные данные'}, {len(body)} байт>"
        self.log.write("response", **entry)

    def _save_asset(self, url, body, ctype, rtype):
        if url in self.saved:
            return self.saved[url]
        if self.assets_bytes + len(body) > MAX_ASSETS:
            self.assets_skipped += 1
            return None
        rel = asset_relpath(url, ctype)
        path = os.path.join(self.out_dir, rel)
        if rtype == "document" or "html" in ctype:
            # в HTML бывают токены и данные пользователя — скрываем как в тексте
            body = redact.redact_text_kv(body.decode("utf-8", "replace")).encode("utf-8")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(body)
        except OSError:
            return None
        self.assets_bytes += len(body)
        self.saved[url] = rel.replace(os.sep, "/")
        return self.saved[url]

    # ------------------------------------------------------------------ WebSocket
    def _on_ws(self, ws):
        wid = str(len(self.ws_stats) + 1)
        st = self.ws_stats[wid] = {"url": redact.redact_url(ws.url), "sent": 0, "recv": 0, "logged_recv": 0}
        self.log.write("ws_open", ws=wid, url=st["url"])
        ws.on("framesent", lambda payload: self._ws_frame(wid, "sent", payload))
        ws.on("framereceived", lambda payload: self._ws_frame(wid, "recv", payload))
        ws.on("close", lambda *_: self.log.write("ws_close", ws=wid, sent=st["sent"], recv=st["recv"]))
        ws.on("socketerror", lambda err: self.log.write("ws_error", ws=wid, error=str(err)[:300]))

    def _ws_frame(self, wid, direction, payload):
        st = self.ws_stats[wid]
        st[direction] += 1
        n = st[direction]
        if direction == "recv":
            if n > WS_FULL and n % WS_EVERY:
                return
            st["logged_recv"] += 1
        if isinstance(payload, (bytes, bytearray)):
            # Стакан биржи часто приходит в двоичном виде. Исходящие (там бывает авторизация) — только размер.
            data = {"binary_bytes": len(payload)}
            if direction == "recv":
                raw = bytes(payload[:WS_BINARY_KEEP])
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
                if text is not None:
                    # кадр оказался текстом/JSON (бывает JSON через двоичный opcode) — чистим как текст
                    as_json, as_text = redact.redact_body(text)
                    data["text"] = as_json if as_json is not None else _truncate(as_text)
                elif not _WS_SECRET.search(raw):
                    data["b64"] = base64.b64encode(raw).decode("ascii")   # настоящий двоичный кадр без явных секретов
                # иначе (двоичное + похоже на секрет) — оставляем только размер
        else:
            as_json, as_text = redact.redact_body(payload)
            data = as_json if as_json is not None else _truncate(as_text)
        self.log.write("ws_" + direction, ws=wid, n=n, data=data)

    # ------------------------------------------------------------------ окончание
    async def wait_until_done(self, duration=0, console=True):
        """До закрытия окна браузера, «q» + Enter в консоли или окончания --duration."""
        waiters = [asyncio.ensure_future(self.closed.wait())]
        if console:
            lines = ConsoleLines()

            async def read_console():
                while True:
                    line = await lines.get()
                    if line is None:            # консоли нет — ждём закрытия окна
                        await asyncio.Event().wait()
                    stop = (line or "").strip().lower() in STOP_WORDS
                    if self.dialog_waiter is not None and not self.dialog_waiter.done():
                        self.dialog_waiter.set_result(line)   # это ответ на вопрос приложения
                        if stop:
                            return                            # ответили стоп-словом → и вопрос закрыли, и выходим
                        continue
                    if stop:
                        return
            waiters.append(asyncio.ensure_future(read_console()))
        if duration:
            waiters.append(asyncio.ensure_future(asyncio.sleep(duration)))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()

    def finish(self):
        self.log.write("stop", requests=self.n_req, skipped=self.skipped, ws=self.ws_stats,
                       assets=len(self.saved), assets_skipped=self.assets_skipped)
        self.log.close()
        self._scrub_learned(os.path.join(self.out_dir, "events.jsonl"))
        return write_summary(self.out_dir)   # summary строится из уже вычищенного журнала

    def _scrub_learned(self, path):
        """Непрозрачные токены из заголовков часто возвращаются в теле под безобидным ключом (result/data) —
        финальный проход вычищает их из всей записи. summary читает журнал уже после этого."""
        if not self.learned:
            return
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return
        new = text
        for v in sorted(self.learned, key=len, reverse=True):
            new = new.replace(v, redact.MASK)
        if new != text:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)
            except OSError:
                pass


# ---------------------------------------------------------------------- запуск из командной строки
def playwright_proxy(url):
    """Прокси сессии → формат Playwright. SOCKS5 с логином/паролем Playwright открыть не умеет."""
    if not url:
        return None
    u = urlsplit(url)
    scheme = u.scheme.lower().replace("socks5h", "socks5")
    if scheme in ("socks5", "socks4") and (u.username or u.password):
        raise RuntimeError("Playwright не открывает SOCKS5 с логином и паролем. "
                           "Запусти с --no-proxy (запись пойдёт с реального IP) или укажи http-прокси.")
    p = {"server": f"{scheme}://{u.hostname}:{u.port}"}
    if u.username:
        p["username"] = unquote(u.username)
    if u.password:
        p["password"] = unquote(u.password)
    return p


async def launch_browser(playwright, browser="auto", headless=False, profile_dir=None, proxy=None):
    """Edge/Chrome с компьютера (качать ничего не надо), иначе Chromium от Playwright."""
    if browser != "auto":
        order = [browser]
    elif os.name == "nt" or sys.platform == "darwin":
        order = ["msedge", "chrome", "chromium"]
    else:
        order = ["chromium", "chrome", "msedge"]
    problems = []
    for name in order:
        kwargs = dict(headless=headless, user_agent=ANDROID_UA, locale="ru-RU",
                      bypass_csp=True, service_workers="block")
        if headless:
            kwargs.update(viewport=VIEWPORT, args=["--window-size=460,1000"])
        else:
            # окно подстраивается под экран, страница тянется за окном. С фиксированным viewport 412×860
            # на обычных ноутбуках нижняя кнопка Telegram (эмуляция) уходила ниже края экрана.
            kwargs.update(no_viewport=True, args=["--window-size=430,760"])
        if proxy:
            kwargs["proxy"] = proxy
        if name != "chromium":
            kwargs["channel"] = name
        elif os.environ.get("AUTOBID_CHROMIUM"):
            kwargs["executable_path"] = os.environ["AUTOBID_CHROMIUM"]
        try:
            ctx = await playwright.chromium.launch_persistent_context(os.path.join(profile_dir, name), **kwargs)
            return ctx, name
        except Exception as e:
            problems.append(f"{name}: {str(e).strip().splitlines()[0][:160]}")
    raise RuntimeError("Не получилось открыть браузер:\n  " + "\n  ".join(problems) +
                       "\nПоставь Chromium для записи: python -m playwright install chromium")


def _flag_for(spec):
    """Параметры запуска → как это открыть явным ключом (для подсказки без терминала)."""
    mode = spec.get("mode")
    if mode == "main":
        return "--main"
    if mode == "menu":
        return "--menu"
    if mode == "button":
        return f'--button "{spec["button"]}"'
    return f"--{mode}"


async def choose_launch(client, bot, send_start=True):
    options = await L.list_options(client, bot, send_start=send_start)
    if len(options) == 1:
        return options[0][1]
    if not sys.stdin.isatty():
        print("Не понял, что открыть. Укажи одним из ключей и запусти снова:")
        for label, spec in options:
            print(f"  {_flag_for(spec):20} — {label}")
        raise L.LaunchError("запусти с нужным ключом (--main / --button … / --menu)")
    print("Что открыть?")
    for i, (label, _) in enumerate(options, 1):
        print(f"  {i}. {label}")
    while True:
        answer = (await asyncio.to_thread(input, "Номер (Enter — 1): ")).strip() or "1"
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][1]
        print("Нет такого номера")


def build_parser():
    p = argparse.ArgumentParser(
        prog="record_webapp.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Запись мини-приложения: запросы к серверу, WebSocket, клики и ввод. "
                    "Окно браузера — твой настоящий аккаунт.",
        epilog="примеры:\n"
               "  python record_webapp.py test cryptobot --button Биржа --label birzha\n"
               "  python record_webapp.py test cryptobot --main\n"
               "  python record_webapp.py test xrocket          # покажет, что можно открыть\n")
    p.add_argument("session", help="имя сессии: файл sessions/<имя>.session (создаёт python qr_login.py <имя>)")
    p.add_argument("bot", nargs="?", default="cryptobot",
                   help="бот: cryptobot, xrocket, xrocket_testnet или @username (по умолчанию cryptobot)")
    what = p.add_mutually_exclusive_group()
    what.add_argument("--button", metavar="ТЕКСТ", help="кнопка мини-приложения в меню бота, например Биржа")
    what.add_argument("--main", action="store_true", help="главное мини-приложение бота")
    what.add_argument("--menu", action="store_true", help="кнопка меню слева от поля ввода")
    what.add_argument("--app", metavar="ИМЯ", help="мини-приложение t.me/<бот>/<ИМЯ>")
    what.add_argument("--url", metavar="ССЫЛКА", help="ссылка мини-приложения (например, из кнопки в record_chat)")
    p.add_argument("--start-param", metavar="P", help="параметр запуска (startapp) для --main и --app")
    p.add_argument("--label", help="подпись к папке записи, например birzha_order")
    p.add_argument("--platform", default=L.DEFAULT_PLATFORM,
                   help="платформа для приложения: android (как раньше), ios, tdesktop, weba")
    p.add_argument("--browser", default="auto", choices=["auto", "msedge", "chrome", "chromium"],
                   help="каким браузером открыть (auto: Edge → Chrome → Chromium)")
    p.add_argument("--no-assets", action="store_true", help="не сохранять файлы страниц и скрипты в assets/")
    p.add_argument("--no-start", action="store_true", help="не отправлять боту /start, если кнопки не нашлось")
    p.add_argument("--no-proxy", action="store_true",
                   help="не использовать прокси сессии для браузера (запись пойдёт с реального IP)")
    p.add_argument("--duration", type=float, default=0, help="закончить через столько секунд (0 — когда закроешь окно)")
    p.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    return p


async def run(args):
    from playwright.async_api import async_playwright
    from telethon import errors, functions, types

    bot_username = bots.resolve(args.bot)
    try:
        proxy = None if args.no_proxy else playwright_proxy(accounts.proxy_url(args.session))
    except RuntimeError as e:
        print(f"❌ {e}")
        return 1
    client = await accounts.connect_authorized(args.session)
    rec = None
    prolong = None
    try:
        try:
            bot = await client.get_input_entity(bot_username)
        except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
            print(f"❌ Не нашёл бота @{bot_username}")
            return 1
        me = await client.get_me()
        account = ("@" + me.username) if getattr(me, "username", None) else (me.first_name or str(me.id))
        if args.url:
            spec = {"mode": "url", "url": args.url}
        elif args.button:
            spec = {"mode": "button", "button": args.button}
        elif args.menu:
            spec = {"mode": "menu"}
        elif args.app:
            spec = {"mode": "app", "app": args.app}
        elif args.main:
            spec = {"mode": "main"}
        else:
            spec = await choose_launch(client, bot, send_start=not args.no_start)
        launch = await L.request_launch(client, bot, start_param=args.start_param, platform=args.platform,
                                        send_start=not args.no_start, **spec)
        url = L.normalize_launch_url(launch.url, platform=args.platform)

        # У прямой ссылки бот-владелец приложения может быть не тем, что в командной строке:
        # CloudStorage и продление идут ему, папка и журнал подписываются по нему.
        app_bot = launch.bot if launch.bot is not None else bot
        app_user = launch.bot_username or bot_username
        app_slug, app_title = bots.slug(app_user), bots.title(app_user)

        async def custom_method(method, params):
            res = await client(functions.bots.InvokeWebViewCustomMethodRequest(
                bot=app_bot, custom_method=method, params=types.DataJSON(data=json.dumps(params or {}))))
            return json.loads(res.data)

        profile = os.path.join(settings.browser_dir(), re.sub(r"[^\w-]+", "_", f"{args.session}_{app_slug}"))
        async with async_playwright() as pw:
            try:
                context, browser_name = await launch_browser(pw, args.browser, args.headless, profile, proxy=proxy)
            except RuntimeError as e:
                print(f"❌ {redact.redact_text_kv(str(e))}")
                return 1
            label = args.label or (launch.button["text"] if launch.button else spec["mode"])
            out_dir = make_recording_dir("webapp", app_slug, label)
            rec = WebAppRecorder(out_dir, platform=args.platform, custom_method=custom_method,
                                 save_assets=not args.no_assets)
            rec.log.write("start", bot=app_user, bot_title=app_title, mode=launch.mode,
                          what=launch.title, url=redact.redact_url(url), platform=args.platform,
                          account=account, account_session=args.session, browser=browser_name)
            await rec.attach(context)
            if launch.query_id:
                prolong = asyncio.ensure_future(L.prolong_loop(
                    client, app_bot, launch.query_id, on_error=lambda e: rec.log.write("prolong_error", error=str(e))))
            page = context.pages[0] if context.pages else await context.new_page()
            rec.app_page = page
            print(f"\n🌐 {app_title}: {launch.title} — открываю в {browser_name}.\n"
                  f"⚠ Это твой настоящий аккаунт: всё, что сделаешь в окне, произойдёт по-настоящему.\n"
                  f"Кликай как обычно. Закончить: закрой окно браузера или напиши q и Enter.\n")
            try:
                await page.goto(url, wait_until="commit")
            except Exception as e:
                msg = redact.redact_text_kv(str(e))     # в тексте ошибки Playwright бывает ссылка с initData
                rec.log.write("goto_error", error=msg[:2000])
                print(f"❌ Мини-приложение не открылось: {type(e).__name__}: {msg.splitlines()[0][:300]}")
                return 1
            rec.opened = True
            await rec.wait_until_done(duration=args.duration, console=True)
            await rec.drain()
            try:
                await context.close()
            except Exception:
                pass
        return 0
    except L.LaunchError as e:
        print(f"❌ {e}")
        return 1
    finally:
        if prolong:
            prolong.cancel()
        if rec:
            rec.finish()
            if rec.opened:
                print(f"\n✅ Запись: {rec.out_dir}\n   Главное — summary.txt; пришли разработчику всю папку.")
            else:
                print(f"\n⚠ Записать не получилось — в папке {rec.out_dir} ничего полезного, присылать нечего.")
        try:
            await client.disconnect()
        except Exception:
            pass


def main(argv=None):
    utf8_console()
    args = build_parser().parse_args(argv)
    try:
        sys.exit(asyncio.run(run(args)) or 0)
    except KeyboardInterrupt:
        pass
    except (settings.ConfigError, accounts.NotAuthorized, accounts.SessionBusy) as e:
        print(f"❌ {e}")
        sys.exit(1)
    except (ConnectionError, OSError):
        print("❌ Нет связи с Telegram — проверь интернет и прокси в config.py")
        sys.exit(1)
    except Exception as e:
        # на всякий случай: ни один сырой traceback (в нём бывает ссылка с initData) не уходит в консоль
        if type(e).__name__ == "FloodWaitError":
            print(f"❌ Telegram просит подождать {getattr(e, 'seconds', '?')} с — попробуй позже")
        else:
            lines = redact.redact_text_kv(traceback.format_exc()).strip().splitlines()
            print("❌ " + (lines[-1] if lines else "не получилось записать"))
        sys.exit(1)
