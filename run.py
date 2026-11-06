"""
Запуск автоперебивки CryptoBot. Читает config.py (MARKETS — P2P, EXCHANGE — биржа) и держит
все включённые рынки 24/7.

    python run.py            все включённые рынки (P2P + биржа)
    python run.py p2p        только P2P
    python run.py exchange   только биржа

Ctrl+C — мягкая остановка.
Перед первым запуском: install.bat, затем заполни config.py (API_ID/API_HASH, MY_NICKNAME, рынки)
и войди в аккаунт — python qr_login.py <имя_сессии>.
"""
import asyncio
import sys

MODES = ("all", "p2p", "exchange")


def main():
    which = "all"
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in ("-h", "--help", "help"):
            print(__doc__)
            return
        if arg not in MODES:
            print(f"Неизвестный режим: {sys.argv[1]!r}. Допустимо: {', '.join(MODES)}.")
            print(__doc__)
            sys.exit(2)
        which = arg

    from autobid import runner
    try:
        asyncio.run(runner.main(which))
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
