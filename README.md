# Router Shop

Бэкенд сервиса роутеров с подпиской на стабильный доступ к зарубежным
ресурсам: парк устройств и туннели к ним, каталог, заказы, подписки, клиенты
и вебхуки оплаты.

Своего интерфейса здесь нет. И клиентская часть, и админка — в стороннем
продукте в `bot/`, он ставится отдельным процессом на хосте и ходит к нам
по HTTP. Порядок — в `docs/state.md`.

Состав:

| Сервис | Что делает |
|---|---|
| `api` | ручки каталога и парка, вебхуки платежей, панель роутера |
| `worker` | расписание: напоминания, статусы устройств, чистка данных |
| `postgres` | данные |
| `redis` | FSM, кэш, rate-limit, очередь рассылок |
| `nginx` + `certbot` | TLS и маршрутизация доменов (выключены, если прокси уже есть) |

## Требования

- VPS с Ubuntu 24.04, 2 vCPU / 4 ГБ RAM / 40 ГБ диска (минимум — 2 ГБ)
- Два домена, A-записи которых указывают на сервер: `api.<домен>` и `admin.<домен>`
- Токен бота от [@BotFather](https://t.me/BotFather)

## Развёртывание с нуля

### 1. Подготовка сервера

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git make curl ufw
```

Docker (официальный репозиторий):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker
docker --version && docker compose version
```

Firewall:

```bash
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw --force enable
```

Часовой пояс сервера держим в UTC (в БД всё в UTC, пользователю показываем Москву):

```bash
sudo timedatectl set-timezone UTC
```

### 2. Код на сервере

Вариант с git (предпочтительный — потом работает `make deploy`):

```bash
sudo mkdir -p /opt/router-shop && sudo chown "$USER" /opt/router-shop
git clone <адрес репозитория> /opt/router-shop && cd /opt/router-shop
```

Вариант без репозитория — залить с рабочей машины по rsync (исключая мусор):

```bash
rsync -az --delete --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
  --exclude '.env' --exclude 'backups' ./ user@<ip>:/opt/router-shop/
```

### 3. Конфигурация

```bash
cd /opt/router-shop && make env
```

`make env` создаёт `.env` из `.env.example` и генерирует `SECURITY_SECRET_KEY`,
`SECURITY_ENCRYPTION_KEY`, `BOT_WEBHOOK_SECRET`, `POSTGRES_PASSWORD`.

Дальше заполните вручную в `.env`:

```dotenv
APP_ENV=prod
APP_BRAND=<название бренда>
APP_BOT_USERNAME=<username бота без @>

BOT_TOKEN=<токен от BotFather>
BOT_MODE=polling                  # на вебхук переключим после выпуска сертификатов
BOT_WEBHOOK_BASE_URL=https://api.<домен>
BOT_OWNER_ID=<ваш TG-id>
BOT_ADMIN_IDS=<id админов через запятую>
BOT_SUPPORT_GROUP_ID=<id группы поддержки, -100...>
BOT_ALERTS_CHAT_ID=<id канала алертов, -100...>

API_PUBLIC_BASE_URL=https://api.<домен>
# Из него собирается разовая ссылка на панель роутера, по которой админка бота
# отправляет браузер оператора. Домен тот же, что у API.
API_ADMIN_BASE_URL=https://api.<домен>
NGINX_API_DOMAIN=api.<домен>
NGINX_ADMIN_DOMAIN=admin.<домен>
```

> Свой TG-id можно узнать у [@userinfobot](https://t.me/userinfobot). Для группы поддержки
> включите режим тем (Topics) и добавьте бота администратором с правом управления темами.

### 4. Первый запуск без TLS

```bash
docker compose up -d --build postgres redis api worker
docker compose ps
```

Сервис `migrate` подтянется автоматически как зависимость: соберёт образ, накатит миграции
и завершится, после чего стартуют приложения. Бот в этот момент работает на polling —
сертификаты ещё не нужны.

### 5. Сертификаты и вебхук

Порядок важен: `nginx` резолвит адрес `api` при старте, поэтому он должен быть
уже поднят (шаг 4).

```bash
./deploy/init-letsencrypt.sh api.<домен> admin.<домен> admin@<домен>
```

Скрипт кладёт временные самоподписанные сертификаты, поднимает nginx и запрашивает боевые
у Let's Encrypt. Тренировочный прогон без расхода лимитов: `STAGING=1 ./deploy/init-letsencrypt.sh ...`.

Когда сертификаты выпущены — переключаем бота на вебхук:

```bash
sed -i 's/^BOT_MODE=polling/BOT_MODE=webhook/' .env
make up
```

### 5а. Если 80 и 443 уже заняты другим прокси

Проверить:

```bash
ss -tlnp | grep -E ':(80|443)\s'
```

Если порты держит `docker-proxy`, на сервере уже работает обратный прокси
(nginx-proxy-manager, Traefik, Caddy). Тогда наши `nginx` и `certbot` не нужны —
встраиваемся за существующий:

```bash
docker network ls
```

Имя сети прокси записать в `.env` как `PROXY_NETWORK=...` и поднимать стек
с оверлеем:

```bash
docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d
```

В самом прокси отдать нам весь домен → `http://router-shop-api:8000`: подробности
в следующем разделе. Сертификаты выпускает прокси, `init-letsencrypt.sh` не запускается.

### 5б. Что отдавать нашему контейнеру

За нами остались `/webhooks` (колбэки оплаты), `/api` (каталог, заказы и парк
для бота и его админки), `/media` (картинки товаров — их тянет Telegram)
и корневые пути LuCI (`/cgi-bin`, `/luci-static`, `/ubus`, `/panel`) — по ним
проксируется панель роутера.

Корень домена нам не нужен: и сайт, и админка живут в стороннем продукте.
Для Caddy:

```caddyfile
vbotrouters.titanvps.click {
    @ours path /webhooks* /api* /media* /panel* /cgi-bin* /luci-static* /ubus*
    handle @ours {
        reverse_proxy router-shop-api:8000
    }

    handle {
        reverse_proxy 127.0.0.1:ПОРТ_ИХ_САЙТА
    }
}
```

Тело блока — только на отдельных строках: `handle ... { reverse_proxy ... }`
в одну строку Caddy не принимает и отвечает «Unexpected next token after '{'
on same line».

Правку применяет `docker exec -w /etc/caddy caddy caddy reload` в контейнере
прокси; перезапускать наш стек ради этого не нужно.

### 5в. Публикация админки бота

Админка стороннего бота — отдельный процесс на хосте: systemd-юнит
`vpn-webadmin.service`, `hypercorn` на порту 8181. Себя она отдаёт не в корне,
а по секретному пути из своей базы (`settings.admin_secret_path`, по умолчанию
`admin123`), поэтому с нашими `/webhooks` и `/api` не пересекается
и живёт на том же домене.

**Штатный `install_bot.sh` запускать нельзя.** Он ставит и перезапускает nginx
и просит сертификат через `certbot --standalone`, а порты 80 и 443 держит Caddy:
в лучшем случае скрипт сломается на середине, в худшем оставит сервер без TLS.
Нужные его шаги делаем руками, их немного.

Код ожидает себя в `/root/bot` — это зашито и в юнитах, и в установщике.
Симлинк оставляет один источник правды:

```bash
apt install -y sqlite3 python3-venv
ln -sfn /opt/router-shop/bot /root/bot
```

Окружения (создаются внутри дерева проекта, в `.gitignore` они закрыты):

```bash
cd /root/bot && python3 -m venv venv && ./venv/bin/pip install -q -r service/requirements.txt
```

```bash
cd /root/bot && python3 -m venv web_admin/venv && ./web_admin/venv/bin/pip install -q -r service/requirements.txt
```

Юниты. В `service/vpn-webadmin.service` перед копированием поправить строку
`ExecStart` (привязка на все интерфейсы — почему, ниже). Доступ к нашему API
дописывается в `[Service]` **обоим юнитам** — и админке, и боту: парк роутеров
показывает админка, каталог и заказы — бот, а ходят они в одни и те же ручки:

```
Environment="FLEET_API_URL=https://vbotrouters.titanvps.click"
Environment="FLEET_API_TOKEN=то_же_что_API_FLEET_TOKEN_в_нашем_.env"
```

Без этих строк у бота не откроется каталог, а в админке — раздел «Каталог»
и вкладка «Роутеры»: они скажут прямым текстом, чего не хватает.
Картинки товаров бот отдаёт ссылкой на наш `/media`, поэтому
`API_PUBLIC_BASE_URL` в нашем `.env` должен быть внешним адресом,
а не `localhost` — фото тянет Telegram, а он ходит снаружи.

```bash
cp /root/bot/service/vpn-bot.service /root/bot/service/vpn-webadmin.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now vpn-bot
```

**База создаётся сама, переносить её неоткуда и незачем.** `init_db()` в боте
создаёт все таблицы и заполняет настройки значениями из кода
(`populate_default_settings`). Веб-админка этого не делает — она только читает,
поэтому бот запускается первым:

```bash
sleep 5 && ls -la /root/bot/*.db && systemctl enable --now vpn-webadmin
```

Токен бота в свежей базе — заглушка `123`. Настоящий задаётся в веб-админке
после первого входа.

Узнать секретный путь (по умолчанию `admin123`):

```bash
sqlite3 /root/bot/*.db "SELECT value FROM settings WHERE key='admin_secret_path';"
```

**Caddy живёт в контейнере, и `127.0.0.1` для него — он сам.** Юнит по умолчанию
слушает только петлю, до неё прокси не достучится. Поэтому меняем в
`/etc/systemd/system/vpn-webadmin.service` привязку на все интерфейсы:

```
ExecStart=/root/bot/web_admin/venv/bin/hypercorn web_admin.run:app -w 1 --bind 0.0.0.0:8181 --keep-alive 30
```

и сразу закрываем порт снаружи, иначе админка окажется доступна по IP в обход
прокси и без TLS:

```bash
ufw deny 8181/tcp && systemctl daemon-reload && systemctl restart vpn-webadmin
```

Адрес, по которому контейнер Caddy видит хост, — шлюз его сети:

```bash
docker inspect $(docker ps --filter name=caddy -q | head -1) --format '{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}'
```

Дальше блок в `Caddyfile` — секретный путь и его API уходят боту, наши пути нам:

```caddyfile
vbotrouters.titanvps.click {
    handle /admin123* {
        reverse_proxy ШЛЮЗ:8181
    }

    handle {
        reverse_proxy router-shop-api:8000
    }
}
```

`handle` берёт первый совпавший блок, поэтому порядок важен: сначала путь бота,
потом всё остальное нам. Директиву на одной строке после `{` Caddy не принимает. Применить: `docker exec -w /etc/caddy caddy caddy reload`.

Проверка — 200 или редирект на форму входа, но не 502:

```bash
curl -o /dev/null -w '%{http_code}
' https://vbotrouters.titanvps.click/admin123/
```

502 значит, что Caddy не достучался до 8181: проверьте привязку юнита и шлюз.

### 6. Проверка

```bash
curl -fsS https://<домен>/healthz              # {"status":"ok"}
curl -fsS https://<домен>/readyz               # database:true, redis:true
curl -fsS https://<домен>/ | head -5           # витрина: <!DOCTYPE html>
curl -o /dev/null -w '%{http_code}\n' https://<домен>/login    # 200
curl -o /dev/null -w '%{http_code}\n' https://<домен>/cabinet  # 303 → /login
```

Бот в этот стек больше не входит: он поставлен отдельным процессом на хосте.
Порядок установки — в `docs/state.md`, раздел о смене основы.

### 7. Бэкапы

```bash
crontab -e
# 15 4 * * * /opt/router-shop/deploy/backup.sh >> /var/log/router-shop-backup.log 2>&1
```

Хранение — 14 дней (`BACKUP_KEEP_DAYS`), выгрузка в S3 включается переменными
`BACKUP_S3_BUCKET` / `BACKUP_S3_ENDPOINT` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.

## Обновление на сервере

```bash
make deploy      # git pull → сборка → миграции → перезапуск
```

## Разработка

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env    # BOT_MODE=polling, APP_ENV=dev
make dev                # postgres, redis, api, worker с примонтированным кодом
make dev-logs s=api
make test
make lint
```

Полезные команды:

| Команда | Что делает |
|---|---|
| `make up` / `make down` | поднять / остановить прод-стек |
| `make logs s=api` | логи сервиса |
| `make migrate` | накатить миграции |
| `make migration m="описание"` | создать миграцию по изменениям моделей |
| `make psql` | консоль базы |
| `make backup` | резервная копия |
| `make test` / `make cov` | тесты / покрытие |
| `make lint` / `make fmt` | проверка / форматирование |

## Структура

```
bot/       сторонний бот, принятый за основу: свой рантайм, ставится вне docker
api/       FastAPI: API устройств, вебхуки оплаты, веб-админка
core/      модели, БД, конфиг, безопасность, бизнес-логика
worker/    задачи по расписанию
migrations/ Alembic
deploy/    nginx, certbot, бэкапы
docs/      решения, API для роутеров, чеклист приёмки
scripts/   служебные скрипты
tests/     тесты
```

Бизнес-логика живёт в `core/`, хендлеры бота и маршруты API — тонкие.

## Эксплуатация

- Логи: JSON в stdout, `docker compose logs`. Токены и телефоны маскируются.
- Метрики Prometheus: `api:8000/metrics`, `bot:8081/metrics`, `worker:8082/metrics`
  (наружу через nginx не отдаются).
- Healthcheck: `/healthz` (жив) и `/readyz` (готов: БД + Redis) у каждого сервиса.
- Sentry включается заданием `SENTRY_DSN`.

Решения и допущения: [docs/decisions.md](docs/decisions.md).
