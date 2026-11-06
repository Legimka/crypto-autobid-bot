# xRocket Exchange API — справка

Официальный Exchange API xRocket: настоящий REST + WebSocket, есть testnet. Этап 1 проекта (по ответам
заказчика) — именно биржа xRocket. Док: https://docs.xrocket.exchange/api/exchange/exchange-api-overview.
Клиент — `autobid/xrocket/api.py`. Часть схем в доке генерируется из OpenAPI и не отрисовалась — такие
поля помечены ниже «проверить на testnet».

## Среды и авторизация

| | REST | WebSocket | Бот (где взять токен) |
|---|---|---|---|
| Mainnet | `https://exchange.api.xrocket.exchange` | `wss://exchange.app-api.xrocket.exchange/` | @xRocket |
| Testnet | `https://exchange.api.testnet.xrocket.exchange` | `wss://exchange.app-api.testnet.xrocket.exchange/` | @xrocket_testnet_bot |

- Токен: в боте → Menu → Settings → Exchange settings → API token. «Renew token» мгновенно гасит старый.
- Авторизация — один заголовок `Authorization: Bearer <token>`. **Ни подписи, ни секрета, ни timestamp.**
- Токен **нельзя ограничить в правах** — полный доступ к счёту (торговля, переводы, вывод). Для разработки
  использовать только **тестовый** токен. Токены mainnet и testnet невзаимозаменяемы.
- Тестовые средства: @testgiver_ton_bot или faucet Chainstack (TON testnet).
- Балансы раздельны: **Funding** (кошелёк) и **Trading** (для ордеров). Перед торговлей перевести в Trading.

## REST (всё под `/api/v1`)

| Метод | Путь | Назначение | Авторизация |
|---|---|---|---|
| GET | `/symbols`, `/symbols/{sym}` | список пар / детали пары | нет |
| GET | `/orderbook?symbol=&depth=&precision=` | снимок стакана | нет |
| GET | `/ticker/24h`, `/trades`, `/candles`, `/rates`, `/assets` | рыночные данные | нет |
| GET | `/accounts/trading/balances` | балансы Trading | Bearer |
| GET | `/accounts/funding/balances` | балансы Funding | Bearer |
| GET | `/orders/active` | активные ордера | Bearer |
| GET | `/order?orderId=\|clientOrderId=` | один ордер | Bearer |
| GET | `/orders/history?symbol=&side=&startAt=&endAt=&currentPage=&pageSize=&hideCanceled=` | история | Bearer |
| POST | `/orders` | выставить ордер (201) | Bearer |
| DELETE | `/order?orderId=\|clientOrderId=` | отменить ордер | Bearer |

- **Смены цены нет** — отдельного endpoint для правки ордера не существует. Меняем цену так: `DELETE /order`
  (отмена) → `POST /orders` (новый). Между ними возможен частичный фил — обрабатывать (у отменённого ордера
  `dealSize > 0` и `status = cancelled`). Для идемпотентности повторов использовать `clientOrderId`.
- Символ пары — `BASE-QUOTE` через дефис (`TON-USDT`). Все числа — строки → `Decimal`.

### Тело POST /orders

Подтверждён пример market-ордера: `{"symbol":"TON-USDT","side":"buy","type":"market","size":"5","timeInForce":"IOC"}`.
Лимитный (проверить на testnet): `{"symbol":"TON-USDT","side":"buy","type":"limit","price":"2.10","size":"5","timeInForce":"GTC"}`.
Поля: `symbol`, `side` (buy/sell), `type` (limit/market/stopLimit/stopMarket), `timeInForce` (GTC/IOC/FOK),
`size` (в базовой монете), `funds` (в котируемой), `price`, `stopPrice`, `clientOrderId`, `remark`.

### Объект ордера (история и WS activeOrders)

`id` (UUID), `clientOrderId`, `symbol`, `side`, `type`, `status`, `createdAt`, `updatedAt`, `dealSize`
(исполнено в базовой), `dealFunds` (в котируемой), `fee`, `feeAsset`, `remark`, `timeInForce`, `size`,
`funds`, `price`, `stopPrice`, `stopTriggered`. `status`: working/pending/sending/completed/cancelled/
rejected/expired. Числа — строки.

## WebSocket

- Connect на корень WS-URL. Keepalive: сервер рвёт idle через 60 с — слать `{"id":"..","method":"ping"}` каждые 30 с.
- Конверт: `{"id","method","params"}`; методы `ping/auth/subscribe/unsubscribe/unsubscribeAll`.
- Приватные каналы после `{"method":"auth","params":{"token":".."}}`.
- Каналы: публичный `orderbook` (`symbol`, `depth` из 5/10/20/50/100/200/500, `precision` как у пары),
  приватные `balances` и `activeOrders`, плюс `ticker/allTickers/trades/candles`.
- Стакан: ответ на subscribe — полный снимок (`bids`/`asks` как `[price,size]`-строки, `sequence`);
  далее пуши `{"method":"subscription","params":{...,"data":{"snapshot":bool,"sequence","bids","asks"}}}`.
  `snapshot=false` — дельта; размер `0` на уровне = уровень убрать; после реконнекта брать новый снимок.
- Ордера на WS не выставляются — только через REST.
- **Реализовано:** `autobid/xrocket/ws.py` — `OrderBookStream` (публичный `orderbook`): снимок + дельты,
  size 0 = убрать уровень, ping 30 с, реконнект с новым снимком. Трейдер берёт стакан из WS при
  `use_ws: True` в рынке и откатывается на REST, пока поток не готов. Проверено на testnet: снимок WS
  совпал с REST, дельты и `sequence` приходят, сортировка «лучшие сверху» верна.

## Ошибки и лимиты

- REST: 200 ok, 201 создан (place), 400 валидация, 401/403 токен/доступ, 404, 429 лимит (заголовок
  `Retry-After`), 500/503. Тело ошибки — RFC 7807 (`detail` — человекочитаемое).
- Лимиты частоты в доке без цифр — узнавать в Dev-чате или замерять на testnet. На 429 — ждать `Retry-After`.
- WS-ошибки в стиле JSON-RPC: `-32030` invalid token, `-32033` unauthorized, `-32034` user blocked,
  `-32050` rate limit, `-32011` incorrect precision, `-32001` order not found и т.д.

## Проверить на testnet перед боем

1. ✅ Точные query-параметры `GET /orderbook` и форма ответа (REST отдаёт обе стороны по возрастанию —
   лучший бид последний; `parse_orderbook` сортирует).
2. ✅ Поля лимитного ордера и форма ответа POST /orders (201, объект ордера с `id`, `status:"working"`, `clientOrderId`).
3. ✅ `DELETE /order` — `orderId`/`clientOrderId` в query; ответ `{"cancelledOrderIds":[..]}`.
4. ✅ Форма ответа балансов: `{"balances":[{asset,balance,available,holds}]}`.
5. Реальные лимиты частоты (пока без цифр — на 429 ждём `Retry-After`).
   Прочее подтверждено на testnet: `clientOrderId` нельзя повторять даже после отмены (ставим уникальный);
   WebSocket-стакан совпадает с REST.
6. Эндпоинт перевода Funding↔Trading (в доке `exchange-transfer-controller-transfer`, путь не зафиксирован).
