"""
Запись диалога с CryptoBot, чтобы настроить смену курса через чат (FLOW в chat_trader.py).

Запуск:  python record_flow.py <сессия перебивающего аккаунта>
Потом в Telegram на этом аккаунте вручную: /p2p → Мой профиль → Мои объявления → объявление →
изменить цену → ввести курс → «Да». Курс можно поставить тот же, что сейчас стоит.
Когда закончишь — Ctrl+C. Запись: flow_log.txt (пришли его разработчику; там только экраны бота, без ключей).
"""
import asyncio
import sys
from datetime import datetime

from telethon import events

from accounts import new_client
from config import TRADER_SESSION

sys.stdout.reconfigure(encoding="utf-8")
SESSION = sys.argv[1] if len(sys.argv) > 1 else TRADER_SESSION
BOT = "send"


def dump(kind, msg):
    lines = [f"[{datetime.now():%H:%M:%S}] {kind}" + (" (моё)" if msg.out else "")]
    lines.append("  ТЕКСТ: " + (msg.text or "").replace("\n", "\n         "))
    for row in (msg.buttons or []):
        lines.append("  КНОПКИ: " + " | ".join(b.text for b in row))
    out = "\n".join(lines) + "\n"
    print(out)
    with open("flow_log.txt", "a", encoding="utf-8") as f:
        f.write(out + "\n")


async def main():
    client = new_client(SESSION)
    await client.start()
    bot = await client.get_entity(BOT)

    @client.on(events.NewMessage(chats=bot))
    async def on_new(event):
        dump("НОВОЕ СООБЩЕНИЕ", event.message)

    @client.on(events.MessageEdited(chats=bot))
    async def on_edit(event):
        dump("ЭКРАН ИЗМЕНИЛСЯ", event.message)

    print(f"Пишу диалог с @{BOT} в flow_log.txt. Меняй курс вручную, потом Ctrl+C.\n")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Готово — пришли flow_log.txt")
