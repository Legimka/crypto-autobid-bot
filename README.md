# crypto-autobid-bot

Автоперебивка заявок на P2P-маркете и бирже **CryptoBot** и **xRocket**. Бот держит твою заявку первой
в стакане в пределах заданного коридора цен. ТЗ и выводы анализа — в [docs/TZ.md](docs/TZ.md).

> **Статус: этап 0 — изучение площадок.** Сюда перенесён код из старого бота сигналов
> ([cryptobot-p2p-bot](https://github.com/Legimka/cryptobot-p2p-bot)). Рекордеры, которые записывают, как работают
> боты и мини-приложения, — в работе.

## Что внутри

| Путь | Что это |
|---|---|
| `qr_login.py` | вход в аккаунт Telegram по QR → `sessions/<имя>.session` |
| `check_app.py` | проверка: вход в мини-приложение CryptoBot и чтение стаканов P2P |
| `autobid/cryptobot/` | клиент внутреннего API мини-приложения CryptoBot и смена курса через чат (из старого бота) |
| `autobid/accounts.py` | аккаунты Telegram (Telethon), прокси |
| `legacy/` | старый бот сигналов как справка для переноса логики (отсюда не запускается) |
| `docs/` | [доступы](docs/ACCESS.md), [ТЗ](docs/TZ.md), [решения](docs/DECISIONS.md) |

## Быстрый старт (Windows)

1. Поставь Python 3.10+ с [python.org](https://www.python.org/downloads/) (галочка **«Add Python to PATH»**).
2. Запусти `install.bat`: он поставит библиотеки и создаст `config.py`.
3. Открой `config.py` Блокнотом и впиши `API_ID` и `API_HASH` (можно взять из старого бота).
4. Войди в тестовый аккаунт: `python qr_login.py test`.
5. Проверь, что аккаунт видит стаканы P2P CryptoBot: `python check_app.py test`.

## Безопасность

- `config.py`, `sessions/`, `*.session` и `recordings/` в git не попадают (см. `.gitignore`).
- Файл `.session` даёт полный доступ к аккаунту. Вход основного аккаунта лучше держать вне папки проекта:
  `SESSIONS_DIR = r"C:\tg-sessions"`.
