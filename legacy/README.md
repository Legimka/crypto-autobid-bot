# legacy — старый бот сигналов (справка)

Снимок [cryptobot-p2p-bot](https://github.com/Legimka/cryptobot-p2p-bot) на коммите `48fdca6` («Релизная версия бота
для P2P CryptoBot»). Здесь он только как справка: логика из этих файлов переносится в пакет `autobid` с тестами,
после переноса папка будет удалена. Отсюда бот не запускается — рабочая версия остаётся в старом репозитории.

| Файл | Что из него берём |
|---|---|
| `stakan.py` | шаг перебивки на сетке `Decimal` (`fixed_step`, `_beat`, `target_for`), плавающая цена, правило лимитки (конкурент — объявление с тем же минимальным лимитом), запасные наблюдатели (`ObserverPool`), определение сделок по объёму, боты сигналов |
| `record_flow.py` | первый рекордер чата; его заменил `record_chat.py` |
| `README_signal_bot.md` | инструкция старого бота |

Уже перенесено: `accounts.py` → `autobid/accounts.py`, `cryptobot_app.py` → `autobid/cryptobot/app_api.py`,
`chat_trader.py` → `autobid/cryptobot/chat_trader.py`, `qr_login.py`, `check_app.py`, `DECISIONS.md` →
`docs/DECISIONS.md`.
