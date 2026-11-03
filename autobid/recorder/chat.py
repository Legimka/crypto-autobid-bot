"""
Запись диалога с ботом (CryptoBot, xRocket, …): все экраны — новые сообщения и их правки — с текстом,
ссылками и кнопками (вместе со скрытыми данными кнопок), твои сообщения и удаления.

Два режима:
  обычный  — нажимаешь кнопки в Telegram (телефон, компьютер) как всегда, рекордер записывает экраны;
  -i       — нажимаешь прямо здесь: вводишь номер кнопки или текст. Так в записи есть точное нажатие
             и всплывающий ответ бота («Цена обновлена», «Слишком часто…») — его другим способом не увидеть.
  -c       — команды по списку (как в -i), для запуска без человека: -c /p2p -c 3 -c 1

Запись: recordings/<время>_chat_<бот>/chat.txt (для чтения) и events.jsonl (всё по порядку, для разбора).
Токены, initData, номера карт и телефонов в записи скрыты.
"""
import argparse
import asyncio
import re
import sys
import time
from datetime import datetime

from telethon import errors, events, functions

from .. import accounts, bots, redact, settings
from . import buttons as B
from .common import STOP_WORDS, ConsoleLines, EventLog, make_recording_dir, utf8_console
from .launch import parse_direct_link


def _red(s):
    """Текст экрана/кнопки/копии: пары «ключ: значение» (токен: …), затем карты и телефоны."""
    return redact.redact_str(redact.redact_text_kv(s), aggressive=True)

HELP_INTERACTIVE = """\
Команды (Enter после каждой):
  3          — нажать кнопку [3] на текущем экране (последнем экране бота с кнопками)
  3@4567     — нажать кнопку [3] на сообщении #4567
  /p2p       — отправить команду боту
  45.68      — отправить текст (всё, что не номер кнопки и не команда рекордера)
  > 100      — отправить текст как есть (например, число «100», а не кнопку [100])
  !s         — показать текущий экран ещё раз
  !q         — закончить запись (или Ctrl+C)"""

EXAMPLES = """\
примеры:
  python record_chat.py test cryptobot            # кликаешь в Telegram, рекордер пишет экраны
  python record_chat.py test cryptobot -i         # кликаешь отсюда по номерам кнопок
  python record_chat.py test xrocket -c /start -c 3 --duration 20
"""


def parse_command(line):
    """Строка консоли → ('press', N, msg_id|None) / ('send', text) / ('screen',) / ('help',) / ('quit',) / ('noop',)"""
    s = (line or "").strip()
    if not s:
        return ("noop",)
    low = s.lower()
    if low in STOP_WORDS:          # q / й / стоп / stop / выход / exit (и с «!»). Отправить такое боту — через «> …»
        return ("quit",)
    if low in ("!s", "!screen", "!экран"):
        return ("screen",)
    if low in ("!h", "!help", "!помощь", "?"):
        return ("help",)
    if s.startswith(">"):
        rest = s[1:].strip()
        return ("send", rest) if rest else ("noop",)
    m = re.fullmatch(r"(\d{1,3})(?:\s*@\s*(\d+))?", s)
    if m:
        return ("press", int(m.group(1)), int(m.group(2)) if m.group(2) else None)
    return ("send", s)


def serialize(msg):
    """Сообщение Telethon → словарь для журнала (без секретов не чистится — это делает вызывающий)."""
    links = []
    try:
        for ent, text in msg.get_entities_text():
            url = getattr(ent, "url", None)
            if url:
                links.append({"text": text, "url": url})
    except Exception:
        pass
    reply_to = getattr(msg, "reply_to", None)
    return {
        "id": msg.id,
        "out": bool(getattr(msg, "out", False)),
        "date": msg.date.isoformat() if getattr(msg, "date", None) else None,
        "edit_date": msg.edit_date.isoformat() if getattr(msg, "edit_date", None) else None,
        "text": getattr(msg, "message", None) or "",
        "links": links,
        "markup": B.describe_markup(getattr(msg, "reply_markup", None)),
        "media": type(msg.media).__name__ if getattr(msg, "media", None) else None,
        "reply_to": getattr(reply_to, "reply_to_msg_id", None),
    }


def clean(m):
    """Скрыть секреты в тексте экрана; данные кнопок (data_hex) не трогаем — по ним потом нажимаем."""
    m = dict(m)
    m["text"] = _red(m["text"])
    m["links"] = [{"text": _red(x["text"]), "url": redact.redact_url(x["url"])} for x in m["links"]]
    if m.get("markup") and m["markup"].get("rows"):
        rows = []
        for row in m["markup"]["rows"]:
            new_row = []
            for b in row:
                b = dict(b)
                b["text"] = _red(b["text"])
                if b.get("url"):
                    b["url"] = redact.redact_url(b["url"])
                if b.get("copy_text"):
                    b["copy_text"] = _red(b["copy_text"])
                new_row.append(b)
            rows.append(new_row)
        m["markup"] = dict(m["markup"], rows=rows)
    return m


def content_key(m):
    return (m["text"], repr(m["links"]), repr(m["markup"]), m["media"])


def render(label, m):
    stamp = datetime.now().strftime("%H:%M:%S")
    who = "🙋 ТЫ" if m["out"] else "🤖"
    lines = [f"[{stamp}] {who} {label} #{m['id']}"]
    for line in (m["text"] or "").splitlines() or [""]:
        lines.append("  │ " + line)
    for link in m["links"]:
        lines.append(f"  │ 🔗 {link['text']} → {link['url']}")
    if m["media"]:
        lines.append(f"  │ [вложение: {m['media']}]")
    mk = m["markup"]
    if mk:
        if mk["type"] in ("inline", "reply"):
            lines.append("  ├ " + ("КНОПКИ:" if mk["type"] == "inline" else "КЛАВИАТУРА ВНИЗУ ЧАТА:"))
            lines += ["  │  " + r for r in B.render_rows(mk)]
            for b in B.flat(mk):
                if b["kind"] in ("webapp", "simple_webapp", "url") and b.get("url"):
                    lines.append(f"  │  «{b['text']}» → {b['url']}")
        elif mk["type"] == "hide":
            lines.append("  ├ (клавиатура скрыта)")
    lines.append("  └")
    return "\n".join(lines)


class ChatRecorder:
    def __init__(self, client, bot, bot_username, out_dir, account="", session_name="", echo=print):
        self.client = client
        self.bot = bot                     # InputPeerUser бота
        self.bot_username = bot_username
        self.out_dir = out_dir
        self.session_name = session_name
        self.echo = echo
        self.log = EventLog(f"{out_dir}/events.jsonl")
        self.txt = open(f"{out_dir}/chat.txt", "a", encoding="utf-8", buffering=1)
        self.screens = {}                  # id → последний вид сообщения (очищенный)
        self.current_id = None             # последний экран бота с кнопками
        self.last_bot_id = None            # последнее сообщение бота (с кнопками или без — например, запрос ввода)
        self.current_changed_at = 0.0      # когда текущий экран появился/сменился (monotonic)
        self.own_sent = set()              # id сообщений, отправленных отсюда (уже записаны)
        self.changed = asyncio.Event()     # бот что-то прислал или поменял
        self.account = account

    # ------------------------------------------------------------------ вывод
    def out(self, text):
        self.echo(text)
        if not self.txt.closed:
            self.txt.write(text + "\n")

    def close(self):
        self.log.write("stop")
        self.log.close()
        if not self.txt.closed:
            self.txt.close()

    # ------------------------------------------------------------------ события
    def handle(self, msg, kind):
        """kind: 'new' / 'edit' / 'history'. Возвращает очищенный экран или None, если ничего не поменялось."""
        m = clean(serialize(msg))
        old = self.screens.get(m["id"])
        if kind == "edit" and old and content_key(old) == content_key(m):
            return None
        self.screens[m["id"]] = m
        if not m["out"]:
            self.last_bot_id = m["id"]
            if m["markup"] and m["markup"]["type"] in ("inline", "reply"):
                if m["id"] != self.current_id or (old and content_key(old) != content_key(m)):
                    self.current_changed_at = time.monotonic()   # экран с кнопками появился или поменялся
                self.current_id = m["id"]
        if m["out"] and m["id"] in self.own_sent:
            return m   # уже записано как 'send'
        self.log.write("message", event=kind, msg=m)
        label = {"new": "НОВОЕ", "edit": "ЭКРАН ИЗМЕНИЛСЯ", "history": "БЫЛО РАНЬШЕ"}[kind]
        if kind == "edit" and old and old["text"] == m["text"]:
            label += " (поменялись только кнопки)"
        self.out(render(label, m))
        if not m["out"]:
            self.changed.set()
        return m

    def handle_deleted(self, ids):
        ids = [i for i in ids if i in self.screens]
        if ids:
            self.log.write("deleted", ids=ids)
            self.out(f"[{datetime.now():%H:%M:%S}] 🗑 удалены сообщения: {', '.join('#' + str(i) for i in ids)}")
            for i in ids:
                self.screens.pop(i, None)
            if self.current_id in ids:
                self.current_id = next((i for i in sorted(self.screens, reverse=True)
                                        if not self.screens[i]["out"] and self.screens[i]["markup"]
                                        and self.screens[i]["markup"]["type"] in ("inline", "reply")), None)

    async def start(self, history=3, interactive=False):
        self.log.write("start", bot=self.bot_username, bot_title=bots.title(self.bot_username),
                       account=self.account, account_session=self.session_name,
                       mode="interactive" if interactive else "passive")
        self.out(f"Запись диалога с {bots.title(self.bot_username)} (@{self.bot_username}) → {self.out_dir}")

        @self.client.on(events.NewMessage(chats=self.bot))
        async def on_new(event):
            self.handle(event.message, "new")

        @self.client.on(events.MessageEdited(chats=self.bot))
        async def on_edit(event):
            self.handle(event.message, "edit")

        @self.client.on(events.MessageDeleted())
        async def on_deleted(event):
            # Удаления в каналах/супергруппах нумеруются отдельно и могут совпасть с id экрана бота в личке.
            if event.chat_id is not None:
                return
            self.handle_deleted(event.deleted_ids or [])

        if history:
            msgs = await self.client.get_messages(self.bot, limit=history)
            for msg in reversed(msgs):
                self.handle(msg, "history")

    # ------------------------------------------------------------------ действия
    def screen(self, msg_id=None):
        return self.screens.get(msg_id or self.current_id)

    async def press(self, index, msg_id=None, typed_at=None):
        if msg_id is None:
            if typed_at is not None and self.current_changed_at > typed_at:
                self.out(f"⚠ экран сменился уже после того, как ты набрал «{index}» — "
                         f"посмотри новый экран (!s) и набери номер снова")
                return False
            if self.last_bot_id is not None and self.current_id is not None and self.last_bot_id != self.current_id:
                self.out(f"⚠ последнее сообщение бота (#{self.last_bot_id}) просит ввод, а не кнопку.\n"
                         f"   отправить «{index}» как текст — напиши: > {index}\n"
                         f"   нажать [{index}] на прошлом экране #{self.current_id} — напиши: {index}@{self.current_id}")
                return False
        m = self.screen(msg_id)
        if not m or not m["markup"] or m["markup"]["type"] not in ("inline", "reply"):
            self.out("⚠ нет экрана с кнопками" + (f" #{msg_id}" if msg_id else "") + " — дождись ответа бота или /start")
            return False
        btns = B.flat(m["markup"])
        if not 1 <= index <= len(btns):
            self.out(f"⚠ на экране #{m['id']} нет кнопки [{index}] (кнопок: {len(btns)}). "
                     f"Чтобы отправить текст «{index}», напиши: > {index}")
            return False
        b = btns[index - 1]
        self.out(f"[{datetime.now():%H:%M:%S}] 👆 НАЖИМАЮ [{index}] «{b['text']}» на экране #{m['id']}")
        self.log.write("press", msg_id=m["id"], index=index, button=b,
                       screen_text=(m["text"] or "")[:300])
        kind = b["kind"]
        if m["markup"]["type"] == "reply":
            if kind == "text":
                return await self.send_text(b["text"], via_button=index)
            self.out(f"⚠ кнопку «{b['text']}» ({kind}) рекордер не нажимает — нажми её в Telegram")
            return False
        if kind == "callback":
            if b.get("requires_password"):
                self.out("⚠ кнопка требует облачный пароль — нажми её в Telegram")
                return False
            return await self._callback(m["id"], index, bytes.fromhex(b.get("data_hex", "")))
        if kind in ("webapp", "simple_webapp"):
            self.out(f"⧉ «{b['text']}» открывает мини-приложение. Сначала закончи эту запись (!q),\n"
                     f"   потом в другом окне:\n"
                     f"   python record_webapp.py {self.session_name or '<сессия>'} {bots.slug(self.bot_username)} "
                     f"--button \"{b['text']}\"")
            return False
        if kind == "url":
            self.out(f"🔗 ссылка кнопки: {b.get('url')}")
            if parse_direct_link(b.get("url")):
                self.out(f"⧉ это ссылка на мини-приложение. Сначала закончи эту запись (!q), потом в другом окне:\n"
                         f"   python record_webapp.py {self.session_name or '<сессия>'} "
                         f"{bots.slug(self.bot_username)} --button \"{b['text']}\"")
            return False
        if kind == "copy":
            self.out(f"📋 кнопка копирует: {b.get('copy_text')}")
            return False
        self.out(f"⚠ кнопки типа «{kind}» рекордер не нажимает — нажми её в Telegram")
        return False

    async def _callback(self, msg_id, index, data):
        # Одно нажатие = один запрос. По умолчанию Telethon на -503 Timeout повторяет запрос до 6 раз
        # (бот получает 6 нажатий) и спит сквозь FLOOD_WAIT ≤60 с, пряча лимит. Отключаем это на время вызова.
        cli = self.client
        saved = (getattr(cli, "_request_retries", None), getattr(cli, "flood_sleep_threshold", None))
        cli._request_retries, cli.flood_sleep_threshold = 0, 0
        try:
            ans = await cli(functions.messages.GetBotCallbackAnswerRequest(
                peer=self.bot, msg_id=msg_id, data=data))
        except (errors.BotResponseTimeoutError, errors.TimedOutError, errors.ServerError, ValueError) as e:
            self.log.write("callback_answer", msg_id=msg_id, index=index, timeout=True, error=type(e).__name__)
            self.out("⌛ бот не подтвердил нажатие вовремя (экран при этом мог поменяться)")
            return True
        except errors.FloodWaitError as e:
            self.log.write("callback_answer", msg_id=msg_id, index=index, error=f"FLOOD_WAIT {e.seconds}")
            self.out(f"⛔ Telegram просит подождать {e.seconds} с перед следующим нажатием")
            return False
        except (errors.MessageIdInvalidError, errors.DataInvalidError) as e:
            self.log.write("callback_answer", msg_id=msg_id, index=index, error=type(e).__name__)
            self.out("⚠ кнопка устарела (бот уже поменял это сообщение) — нажми на свежем экране")
            return False
        except errors.RPCError as e:
            self.log.write("callback_answer", msg_id=msg_id, index=index, error=f"{type(e).__name__}: {e}")
            self.out(f"❌ Telegram ответил ошибкой: {type(e).__name__}: {e}")
            return False
        finally:
            cli._request_retries, cli.flood_sleep_threshold = saved
        answer = {"message": redact.redact_str(ans.message or "", aggressive=True) or None,
                  "alert": bool(ans.alert), "url": redact.redact_url(ans.url) if ans.url else None}
        self.log.write("callback_answer", msg_id=msg_id, index=index, answer=answer)
        if answer["message"]:
            self.out(("❗ ОКНО БОТА: " if answer["alert"] else "💬 ВСПЛЫВАШКА БОТА: ") + answer["message"])
        if answer["url"]:
            self.out(f"🔗 бот открывает ссылку: {answer['url']}")
        return True

    async def send_text(self, text, via_button=None):
        if not (text or "").strip():
            self.out("⚠ пустое сообщение не отправляю (после > напиши текст)")
            return False
        shown = redact.redact_str(text, aggressive=True)
        try:
            sent = await self.client.send_message(self.bot, text, parse_mode=None, link_preview=False)
        except errors.FloodWaitError as e:
            self.log.write("send_failed", text=shown, via_button=via_button, error=f"FLOOD_WAIT {e.seconds}")
            self.out(f"⛔ Telegram просит подождать {e.seconds} с — сообщение не отправлено")
            return False
        except errors.YouBlockedUserError:
            self.log.write("send_failed", text=shown, via_button=via_button, error="YouBlockedUser")
            self.out("❌ бот заблокирован — разблокируй его в Telegram и попробуй снова")
            return False
        except (ValueError, errors.RPCError) as e:
            self.log.write("send_failed", text=shown, via_button=via_button, error=f"{type(e).__name__}: {e}")
            self.out(f"❌ сообщение не отправилось: {type(e).__name__} (запись продолжается)")
            return False
        self.own_sent.add(sent.id)
        self.log.write("send", msg_id=sent.id, text=shown, via_button=via_button)
        # Telethon мог уже показать это сообщение как событие (ответ на запрос тоже идёт в очередь событий)
        if sent.id not in self.screens:
            self.out(f"[{datetime.now():%H:%M:%S}] 🙋 ТЫ #{sent.id}: {shown}")
            self.screens[sent.id] = clean(serialize(sent))
        return True

    async def run_command(self, line, typed_at=None):
        """Выполнить одну команду консоли. False — пора выходить."""
        cmd = parse_command(line)
        if cmd[0] == "quit":
            return False
        if cmd[0] == "help":
            self.out(HELP_INTERACTIVE)
        elif cmd[0] == "screen":
            m = self.screen()
            self.out(render("ТЕКУЩИЙ ЭКРАН", m) if m else "⚠ экрана с кнопками пока нет")
        elif cmd[0] == "press":
            await self.press(cmd[1], cmd[2], typed_at=typed_at)
        elif cmd[0] == "send":
            await self.send_text(cmd[1])
        return True

    async def wait_change(self, timeout=10.0, settle=1.0):
        """Ждать ответа бота после действия; потом ещё settle сек — бот иногда шлёт несколько правок подряд."""
        try:
            await asyncio.wait_for(self.changed.wait(), timeout)
            await asyncio.sleep(settle)
            return True
        except asyncio.TimeoutError:
            return False


def build_parser():
    p = argparse.ArgumentParser(
        prog="record_chat.py", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EXAMPLES,
        description="Запись диалога с ботом: экраны, кнопки (с их данными), твои сообщения и нажатия.")
    p.add_argument("session", help="имя сессии: файл sessions/<имя>.session (создаёт python qr_login.py <имя>)")
    p.add_argument("bot", nargs="?", default="cryptobot",
                   help="бот: cryptobot, xrocket, xrocket_testnet или @username (по умолчанию cryptobot)")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="нажимать кнопки отсюда: номер кнопки или текст сообщения (подсказка — !h)")
    p.add_argument("-c", "--command", action="append", default=[], metavar="КОМАНДА",
                   help="выполнить команду как в -i (можно несколько: -c /p2p -c 3); после каждой ждём ответ бота")
    p.add_argument("--history", type=int, default=3, help="сколько последних сообщений бота показать в начале (3)")
    p.add_argument("--duration", type=float, default=0,
                   help="закончить запись через столько секунд (0 — пока не остановишь)")
    p.add_argument("--wait", type=float, default=10, help="сколько ждать ответа бота на команду из -c, сек (10)")
    p.add_argument("--label", help="подпись к папке записи, например p2p_price")
    return p


async def run(args):
    bot_username = bots.resolve(args.bot)
    client = await accounts.connect_authorized(args.session)
    rec = None
    try:
        try:
            bot = await client.get_input_entity(bot_username)
        except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
            print(f"❌ Не нашёл бота @{bot_username}")
            return 1
        me = await client.get_me()
        account = ("@" + me.username) if getattr(me, "username", None) else (me.first_name or str(me.id))
        out_dir = make_recording_dir("chat", bots.slug(bot_username), args.label)
        rec = ChatRecorder(client, bot, bot_username, out_dir, account=account, session_name=args.session)
        await rec.start(history=args.history, interactive=args.interactive or bool(args.command))

        for line in args.command:
            rec.changed.clear()
            if not await rec.run_command(line):
                return 0
            if not await rec.wait_change(args.wait):
                rec.out(f"⌛ за {args.wait:.0f} с бот ничего не прислал")

        deadline = time.monotonic() + args.duration if args.duration else None
        lines = ConsoleLines()
        if args.interactive:
            rec.out(HELP_INTERACTIVE)
        else:
            rec.out("Нажимай кнопки в Telegram — экраны появятся здесь. Закончить: !q и Enter (или Ctrl+C).")
        while True:
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            if timeout == 0.0:
                rec.out(f"⏱ прошло {args.duration:.0f} с — заканчиваю")
                break
            try:
                line, typed_at = await asyncio.wait_for(lines.get_at(), timeout)
            except asyncio.TimeoutError:
                continue
            if line is None:   # консоли нет (запуск без терминала): пишем дальше до --duration или Ctrl+C
                if deadline is None:
                    await client.run_until_disconnected()
                else:
                    await asyncio.sleep(max(0.0, deadline - time.monotonic()))
                break
            if args.interactive:
                if not await rec.run_command(line, typed_at=typed_at):
                    break
            elif line.strip().lower() in STOP_WORDS:
                break
            elif line.strip():
                rec.out("ℹ в этом режиме кнопки нажимаются в Telegram. Нажимать отсюда — запусти с -i. Закончить: q")
        return 0
    finally:
        if rec:
            rec.close()
            print(f"\n✅ Запись: {rec.out_dir}\n   Пришли эту папку разработчику (chat.txt и events.jsonl).")
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
    except errors.FloodWaitError as e:
        print(f"❌ Telegram просит подождать {e.seconds} с — попробуй позже")
        sys.exit(1)
    except (ConnectionError, OSError):
        print("❌ Нет связи с Telegram — проверь интернет и прокси в config.py")
        sys.exit(1)
