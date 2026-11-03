"""
Вход в Telegram по QR-коду (без кода подтверждения) — создаёт файл сессии sessions/<имя>.session.
Перенесено из cryptobot-p2p-bot.

Запуск:  python qr_login.py <имя_сессии>      (например test; по умолчанию test)
Телефон: Telegram → Настройки → Устройства → Подключить устройство → навести на QR в браузере.
Если включён облачный пароль (2FA) — скрипт попросит его ввести (символы не видны — так и должно быть).
"""
import asyncio
import getpass
import os
import sys
import webbrowser

import qrcode
import qrcode.image.svg
from telethon.errors import SessionPasswordNeededError

from autobid import settings
from autobid.accounts import new_client, proxy_url, session_path

QR_PAGE = os.path.join(settings.ROOT, "login_qr.html")


def show_qr(url, first):
    # SVG не требует Pillow; страница сама обновляется — в браузере всегда свежий QR
    qr = qrcode.QRCode(border=2, box_size=12, image_factory=qrcode.image.svg.SvgPathImage)
    qr.add_data(url)
    svg = qr.make_image().to_string(encoding="unicode")
    with open(QR_PAGE, "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="3">'
                '<title>Вход в Telegram</title><style>svg{width:100%;height:auto}</style>'
                '<body style="background:#fff;text-align:center;font:18px sans-serif">'
                '<p>Telegram на телефоне → Настройки → Устройства → Подключить устройство</p>'
                f'<div style="width:420px;margin:auto">{svg}</div></body>')
    if first:
        print(f"QR открыт в браузере: {QR_PAGE}")
        try:
            if hasattr(os, "startfile"):
                os.startfile(QR_PAGE)
            else:
                webbrowser.open("file://" + QR_PAGE)
        except Exception:
            print("Открой этот файл в браузере вручную.")


async def main(name):
    client = new_client(name)   # через прокси, если он задан для этой сессии в config.py
    if proxy_url(name):
        print(f"Вход через прокси {proxy_url(name).split('@')[-1]}")
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Уже вошли как @{me.username or me.first_name} (сессия {name})")
        await client.disconnect()
        return

    qr = await client.qr_login()
    first = True
    print("Жду сканирования (QR обновляется каждые ~25 сек, страница в браузере подхватит новый)...")
    while True:
        show_qr(qr.url, first)
        first = False
        try:
            await qr.wait(timeout=25)
            break
        except asyncio.TimeoutError:
            await qr.recreate()
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass("Облачный пароль 2FA (символы не видны при вводе): "))
            break
        except Exception as e:
            print(f"❌ Telegram отклонил вход: {type(e).__name__}: {e}")
            await client.disconnect()
            return

    me = await client.get_me()
    print(f"✅ Вошли как @{me.username or me.first_name}. Сессия: {session_path(name)}.session")
    print("   Этот файл даёт полный доступ к аккаунту — никому его не пересылай.")
    await client.disconnect()
    try:
        os.remove(QR_PAGE)
    except OSError:
        pass


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "test"))
    except settings.ConfigError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        pass
