# Router Shop

Telegram-бот и бэкенд для продажи роутеров с подпиской на сервис стабильного доступа
к зарубежным ресурсам: каталог и заказы, оплата, активация устройств, выдача подписки
роутерам, поддержка и админка.

Состав:

| Сервис | Что делает |
|---|---|
| `bot` | клиентский бот на aiogram: покупка, активация, подписка, поддержка |
| `api` | API устройств, вебхуки платежей, веб-админка |
| `worker` | расписание: напоминания, статусы устройств, чистка данных |
| `postgres` | данные |
| `redis` | FSM, кэш, rate-limit, очередь рассылок |
| `nginx` + `certbot` | TLS, маршрутизация доменов, вебхук Telegram |

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
API_ADMIN_BASE_URL=https://admin.<домен>
NGINX_API_DOMAIN=api.<домен>
NGINX_ADMIN_DOMAIN=admin.<домен>
```

> Свой TG-id можно узнать у [@userinfobot](https://t.me/userinfobot). Для группы поддержки
> включите режим тем (Topics) и добавьте бота администратором с правом управления темами.

### 4. Первый запуск без TLS

```bash
docker compose up -d --build postgres redis api bot worker
docker compose ps
```

Сервис `migrate` подтянется автоматически как зависимость: соберёт образ, накатит миграции
и завершится, после чего стартуют приложения. Бот в этот момент работает на polling —
сертификаты ещё не нужны.

### 5. Сертификаты и вебхук

Порядок важен: `nginx` резолвит адреса `api` и `bot` при старте, поэтому они должны быть
уже подняты (шаг 4).

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

В самом прокси добавить хосты: `api.<домен>` и `admin.<домен>` → `http://router-shop-api:8000`,
а для вебхука Telegram — отдельный location `/tg/webhook` → `http://router-shop-bot:8081`.
Сертификаты в этом случае выпускает прокси, скрипт `init-letsencrypt.sh` не запускается.

### 5б. Сайт занимает корень домена

Витрина, вход и кабинет живут по корневым путям (`/`, `/catalog/...`, `/login`,
`/register`, `/cabinet`), админка — под `/admin`, вебхуки — под `/webhooks`.
Всё это один контейнер `api`, поэтому прокси должен отдавать ему **весь домен**,
а не отдельные пути. Для Caddy этого достаточно:

```caddyfile
vbotrouters.titanvps.click {
    reverse_proxy router-shop-api:8000
}
```

Если в конфиге прокси перечислены отдельные пути (`/admin`, `/webhooks`),
корень туда не попадёт и на месте витрины будет 404 самого прокси — снаружи
это неотличимо от несобранного сайта. Проверять отдельно от `/healthz`.

Правку Caddyfile применяет `docker exec -w /etc/caddy caddy caddy reload`
в контейнере прокси; перезапускать наш стек ради этого не нужно.

### 6. Проверка

```bash
curl -fsS https://<домен>/healthz              # {"status":"ok"}
curl -fsS https://<домен>/readyz               # database:true, redis:true
curl -fsS https://<домен>/ | head -5           # витрина: <!DOCTYPE html>
curl -o /dev/null -w '%{http_code}\n' https://<домен>/login    # 200
curl -o /dev/null -w '%{http_code}\n' https://<домен>/cabinet  # 303 → /login
docker compose exec bot python -c "import asyncio;from bot.loader import create_bot;print(asyncio.run(create_bot().get_me()))"
docker compose logs bot | grep webhook_set     # вебхук установлен
```

В Telegram: `/start` у бота — должно прийти приветствие и главное меню.

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
make dev                # postgres, redis, api, bot, worker с примонтированным кодом
make dev-logs s=bot
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
bot/       aiogram: хендлеры, клавиатуры, состояния, middlewares, тексты
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
