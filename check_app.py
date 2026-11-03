"""
Проверка аккаунта: вход в мини-приложение CryptoBot и чтение стаканов P2P из config.MARKETS.
Перенесено из cryptobot-p2p-bot.

Запуск:  python check_app.py <имя_сессии> [USDT/RUB ...]    (сессия создаётся: python qr_login.py <имя>)
"""
import asyncio
import sys
import traceback

from autobid import accounts, settings
from autobid.cryptobot.app_api import CryptoBotApp


async def main(name, markets):
    tg = await accounts.connect_authorized(name)
    me = await tg.get_me()
    print(f"✅ Telegram: вошли как @{me.username or me.first_name}"
          + (" (через прокси)" if accounts.proxy_url(name) else ""))
    nick = (settings.get("MY_NICKNAME") or "").lower()
    app = CryptoBotApp(tg, proxy=accounts.proxy_url(name))
    try:
        await app.login()
        print("✅ Приложение CryptoBot: вход выполнен")
        for key in markets:
            asset, fiat = key.upper().split("/")
            for side in ("sell", "buy"):
                book = await app.offers(asset, fiat, side, max_pages=2)
                mine = [d for d in book if nick and nick in d["n"].lower()]
                print(f"\n{asset}/{fiat} type={side}: {len(book)} объявлений"
                      + (f"  ← тут твои ({len(mine)} шт.)" if mine else ""))
                for d in book[:3]:
                    kind = f" ({d['ratio'] - 100:+.2f}%)" if d["ptype"] == "floating" else ""
                    print(f"   {d['p']:>12}{kind}  {d['n'][:20]:<20} лимит {d['lmin']:>8}  объём {d['vol']:>12}")
                await asyncio.sleep(0.5)
    except Exception:
        print("❌ Ошибка:")
        traceback.print_exc()
    finally:
        await app.close()
        await tg.disconnect()


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    session = sys.argv[1] if len(sys.argv) > 1 else "test"
    wanted = sys.argv[2:] or list(settings.get("MARKETS")) or ["USDT/RUB"]
    try:
        asyncio.run(main(session, wanted))
    except (settings.ConfigError, accounts.NotAuthorized) as e:
        print(f"❌ {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        pass
