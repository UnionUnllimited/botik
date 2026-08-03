# Оплата через PLATEGA

Документация провайдера: <https://docs.platega.io> (сверено 03.08.2026).
Реализация: [core/payments/platega.py](../core/payments/platega.py),
оркестрация — [core/services/payments.py](../core/services/payments.py).

## Что нужно настроить

1. В личном кабинете PLATEGA → «Настройки» взять `X-MerchantId` и `X-Secret`.
2. Записать их в `.env` (`PLATEGA_MERCHANT_ID`, `PLATEGA_SECRET`).
3. Указать провайдеру адрес колбэка: `https://<домен API>/webhooks/platega`.
   Адрес обязан быть публичным и с валидным TLS — провайдер не шлёт уведомления
   на приватные диапазоны и localhost.
4. По желанию ограничить источник колбэков: `PLATEGA_ALLOWED_IPS=1.2.3.4,5.6.7.0/24`.

## Используемые эндпоинты

| Операция | Метод и путь | Где в коде |
|---|---|---|
| Создать платёж (метод выбирает клиент) | `POST /v2/transaction/process` | `PlategaProvider.create_payment` |
| Создать платёж с заданным методом | `POST /transaction/process` | то же, если задан `PLATEGA_DEFAULT_METHOD` |
| Статус транзакции | `GET /transaction/{id}` | `PlategaProvider.check_status` |
| Возврат/отмена | `POST /transaction/{id}/cancel` | `PlategaProvider.refund` |

Пути вынесены в настройки: в документации соседствуют `/v2/transaction/process`
и `/transaction/process`, и префикс может измениться без нашего релиза.

Коды способов оплаты: `2` — СБП и SberPay, `3` — ЕРИП, `11` — карты,
`12` — международные платежи, `13` — криптовалюта.

Статусы: `PENDING`, `CONFIRMED`, `CANCELED`, `CHARGEBACKED` — раскладываются
в наши `pending / succeeded / canceled / refunded`.

## Три вещи, которых у провайдера нет

**Идемпотентности.** Передать свой `externalId` некуда. Поэтому наш
идентификатор платежа уезжает в свободное поле `payload` в виде `rs:<payment_id>`
и возвращается в колбэке. Защита от двойного зачисления держится на трёх опорах:
уникальный индекс на `payments.provider_payment_id`, отметка `payments.processed_at`
и блокировка строки `SELECT … FOR UPDATE` на время обработки.

**Подписи колбэка.** HMAC нет: провайдер присылает обратно наши же
`X-MerchantId` и `X-Secret`. Сверяем их `hmac.compare_digest`, дополнительно
сверяем сумму уведомления с суммой платежа в базе — при расхождении платёж
не зачисляется, а в админ-канал уходит алерт.

**Фискализации по 54-ФЗ.** Состав чека API не принимает. Позиции («Роутер»,
«Подписка», «Доставка» со ставками НДС) собираются в `payments.receipt` при
создании платежа и готовы к передаче в кассу. Что делать дальше — решается
с учётом вашей схемы работы:

* если PLATEGA выступает платёжным агентом и пробивает чеки сама — уточните
  это у менеджера и зафиксируйте письменно, кода менять не нужно;
* если нет — подключается облачная касса отдельной реализацией; точка
  расширения одна: `receipt_items()` в `core/services/payments.py`.

## Проверка колбэка вручную

Успешная оплата (подставьте свои реквизиты и id транзакции):

```bash
curl -i -X POST https://<домен API>/webhooks/platega -H 'Content-Type: application/json' -H "X-MerchantId: $PLATEGA_MERCHANT_ID" -H "X-Secret: $PLATEGA_SECRET" -d '{"id":"3fa85f64-5717-4562-b3fc-2c963f66afa6","amount":6900,"currency":"RUB","status":"CONFIRMED","paymentMethod":2,"payload":"rs:1"}'
```

Ожидаемо: `200`, заказ переходит в `paid`, клиенту уходит сообщение,
подписка создаётся в статусе `pending`.

Повторная отправка того же тела должна вернуть `200` и **не** продлить подписку
второй раз — в логах будет `payment.duplicate_notification`.

Неверный секрет:

```bash
curl -i -X POST https://<домен API>/webhooks/platega -H 'Content-Type: application/json' -H "X-MerchantId: $PLATEGA_MERCHANT_ID" -H 'X-Secret: wrong' -d '{"id":"x","amount":1,"currency":"RUB","status":"CONFIRMED"}'
```

Ожидаемо: `401`, в логах `webhook.auth_failed`.

## Если колбэк не дошёл

Провайдер делает три попытки с интервалом 5 минут и ждёт ответа 60 секунд.
Если все три не прошли, платёж подхватит воркер: задача `sync_pending_payments`
раз в 3 минуты опрашивает статусы висящих платежей за последние сутки.
Клиент может ускорить это кнопкой «Проверить оплату» под ссылкой.

Платежи с истёкшей ссылкой (у PLATEGA — 15 минут) переводятся в `canceled`
задачей `expire_payments`, клиенту предлагается кнопка «Оплатить заново».
