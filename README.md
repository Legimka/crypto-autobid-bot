# crypto-autobid-bot

Автоперебивка заявок на P2P-маркете и бирже **CryptoBot** и **xRocket**. Бот держит твою заявку первой
в стакане в пределах заданного коридора цен. ТЗ и выводы анализа — в [docs/TZ.md](docs/TZ.md).

> **Статус: этап 0 — изучение площадок.** Готовы рекордеры, которые записывают, как работают боты и
> мини-приложения. Ядро автоперебивки пишется по этим записям. Код из старого бота сигналов
> ([cryptobot-p2p-bot](https://github.com/Legimka/cryptobot-p2p-bot)) перенесён сюда.

## Что внутри

| Путь | Что это |
|---|---|
| `record_chat.py` | запись диалога с ботом: экраны, кнопки с их данными, нажатия ([как пользоваться](docs/RECORDING.md)) |
| `record_webapp.py` | запись мини-приложения (Биржа CryptoBot, xRocket): запросы к серверу, WebSocket, клики, ввод |
| `qr_login.py` | вход в аккаунт Telegram по QR → `sessions/<имя>.session` |
| `check_app.py` | проверка: вход в мини-приложение CryptoBot и чтение стаканов P2P |
| `autobid/cryptobot/` | клиент внутреннего API мини-приложения CryptoBot и смена курса через чат (из старого бота) |
| `autobid/recorder/` | рекордеры: эмуляция Telegram в браузере (`webview_shim.js`), маскирование секретов, сводки |
| `autobid/redact.py` | что скрывается в записях: initData, cookies, токены, пароли, номера карт и телефонов |
| `legacy/` | старый бот сигналов как справка для переноса логики (отсюда не запускается) |
| `docs/` | [доступы](docs/ACCESS.md), [запись кликов](docs/RECORDING.md), [ТЗ](docs/TZ.md), [решения](docs/DECISIONS.md) |

## Быстрый старт (Windows)

1. Поставь Python 3.10+ с [python.org](https://www.python.org/downloads/) (галочка **«Add Python to PATH»**).
2. Запусти `install.bat`: он поставит библиотеки и создаст `config.py`.
3. Открой `config.py` Блокнотом и впиши `API_ID` и `API_HASH` (можно взять из старого бота).
4. Войди в тестовый аккаунт: `python qr_login.py test`.
5. Запиши, что нужно (подробно — [docs/RECORDING.md](docs/RECORDING.md)):
   ```bash
   python record_chat.py test cryptobot -i
   python record_webapp.py test cryptobot --button Биржа
   ```

## Безопасность

- `config.py`, `sessions/`, `*.session` и `recordings/` в git не попадают (см. `.gitignore`).
- Файл `.session` даёт полный доступ к аккаунту. Вход основного аккаунта лучше держать вне папки проекта:
  `SESSIONS_DIR = r"C:\tg-sessions"`.
- В записях секреты заменены на `***`. Цены, объёмы и номера заявок остаются: без них записи бесполезны.
- Запись идёт с настоящего аккаунта: заявки, объявления и переводы в окне делаются по-настоящему.

## Тесты

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

Сквозной тест открывает поддельное мини-приложение на официальном `telegram-web-app.js` в Chromium без окна.
Он проверяет, что рекордер записал клики, запросы, WebSocket и кнопки Telegram и что ни один секрет не попал
в файлы записи.
