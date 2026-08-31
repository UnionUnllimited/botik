# Витрина за прокси на titanvps.pro

Задача: покупатель видит только `routers.titanvps.pro`, а адрес origin
(`82.197.73.251`) и его прежнее имя не видит никто.

**Списки и прошивки в этот заход не входят** — решение заказчика от
31 августа. Ломать нечего: роутеры тянут списки со старого сервера
`vm171085`, а адрес манифеста в прошивку ещё не прописан (см. `docs/state.md`,
раздел про обновление прошивки). Но адрес им понадобится **раньше**, чем
образ уйдёт в партию: зашитый в прошивку адрес меняется перевыпуском
образа, а не правкой конфига. Заготовка — в комментариях
`stream-sni.conf` и `routers-site.conf`.

```
клиент ──443──▶ 5.34.214.243 (titanvps.pro)
                 │  nginx stream, читает SNI
                 ├─ routers.titanvps.pro ────▶ nginx http ─443─▶ 82.197.73.251 (origin)
                 └─ всё остальное ────────────▶ remnanode, как было
```

## Почему так, а не проще

На прокси-сервере уже стоит remnanode и держит 443 целиком: на неузнанный
запрос она отдаёт камуфляж — сертификат `amd.com`. Обычный vhost рядом
не встанет, порт один. Поэтому впереди ставится `ngx_stream_ssl_preread`:
он читает имя из приветствия TLS, не расшифровывая его, и разводит по
двум адресам. Для ноды это остаётся прозрачным TCP, Reality не ломается.

Скрытие origin держится не на прокси, а на трёх вещах сразу: старое имя
убирается из DNS, origin принимает только адрес прокси, и приложение
перестаёт собирать ссылки со старым именем. Достаточно пропустить одну —
и адрес всплывёт: в DNS, в CT-логах сертификата или в адресе картинки
на витрине.

## Порядок

Шаги 1–4 обратимы и ноду не трогают. Ломающий — только пятый.

### 1. DNS

В Spaceship завести запись:

```
routers.titanvps.pro.  A  5.34.214.243
```

Запись `vbotrouters.titanvps.click` пока **не трогать** — на ней всё ещё
работает боевой сервис.

### 2. Освободить 443 у ноды

Посмотреть, чем она опубликована:

```bash
docker ps --format '{{.Names}}\t{{.Ports}}' | grep -i remna
```

В её `docker-compose.yml` публикация меняется с `443:443` на
`127.0.0.1:8444:443` — снаружи порт закрывается, нода остаётся на месте:

```bash
docker compose up -d
```

С этой минуты 443 снаружи не отвечает. Клиенты ноды лежат до шага 4,
поэтому 2, 3 и 4 делаются подряд, а не с перерывом на обед.

### 3. Сертификаты

```bash
apt install -y nginx certbot python3-certbot-nginx
certbot certonly --standalone -d routers.titanvps.pro
```

`--standalone` берёт 80, который свободен. Если nginx уже слушает 80 —
`--webroot -w /var/www/certbot`.

### 4. Конфигурация nginx

`stream-sni.conf` → `/etc/nginx/stream.d/`, `routers-site.conf` →
`/etc/nginx/conf.d/`. В `/etc/nginx/nginx.conf` на верхнем уровне,
рядом с `http {}`, а не внутри:

```
stream {
    include /etc/nginx/stream.d/*.conf;
}
```

Сертификат origin (самоподписанный, из шага 5) кладётся в
`/etc/nginx/origin-ca.pem`. Проверить и поднять:

```bash
nginx -t && systemctl reload nginx
```

Сразу же убедиться, что нода вернулась:

```bash
openssl s_client -connect 127.0.0.1:443 -servername titanvps.pro </dev/null 2>/dev/null | openssl x509 -noout -subject
```

Должен снова быть `amd.com`. Если нет — откатить шаг 2 и разбираться,
клиенты ноды ждать не будут.

### 5. Закрыть origin

На origin (`82.197.73.251`) приложение перестаёт быть публичным.
Самоподписанный сертификат вместо Let's Encrypt — чтобы имени не было
и в CT-логах:

```bash
mkdir -p /opt/router-shop/deploy/origin-tls && cd /opt/router-shop/deploy/origin-tls && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout origin.key -out origin.crt -subj "/CN=routers.titanvps.pro" -addext "subjectAltName=DNS:routers.titanvps.pro,DNS:cdn.titanvps.pro"
```

Ключ в репозиторий не попадает — каталог в `.gitignore`. Второе имя в SAN
стоит с запасом, хотя списки и прошивки отложены: перевыпускать сертификат
ради добавления имени — это ещё и копировать его заново на прокси.

Дальше origin поднимается своим nginx на 8443, а 80 и 443 остаются чужому
Caddy. В `.env` меняется набор файлов compose:

```bash
cd /opt/router-shop && sed -i 's|^COMPOSE_FILE=.*|COMPOSE_FILE=docker-compose.yml:docker-compose.origin.yml|' .env && grep '^COMPOSE_FILE' .env
```

```bash
cd /opt/router-shop && docker compose up -d --remove-orphans
```

`deploy/origin-tls/origin.crt` копируется на прокси в
`/etc/nginx/origin-ca.pem`. Дальше порт закрывается на всех, кроме прокси:

```bash
ufw allow from 5.34.214.243 to any port 8443 proto tcp
ufw deny 8443/tcp
```

Проверить снаружи, что закрыто:

```bash
curl -sk --max-time 5 https://82.197.73.251:8443/healthz || echo "закрыто, как и надо"
```

### 6. Переписать адрес в приложении

Без этого витрина покажет старое имя в адресах картинок на первом же экране:
их собирает `API_PUBLIC_BASE_URL`.

```bash
cd /opt/router-shop && sed -i 's|^API_PUBLIC_BASE_URL=.*|API_PUBLIC_BASE_URL=https://routers.titanvps.pro|' .env && grep '^API_PUBLIC_BASE_URL' .env
```

```bash
cd /opt/router-shop && make deploy
```

Проверить, что старого имени в выдаче не осталось:

```bash
curl -s https://routers.titanvps.pro/ | grep -c "titanvps.click" || echo "чисто"
```

### 7. Убрать старое имя

Только после того, как витрина открылась по новому адресу и заказ прошёл
целиком: `vbotrouters.titanvps.click` удаляется из DNS, его vhost — из
Caddy на origin.

Имя останется в CT-логах навсегда — это уже выпущенный сертификат, отозвать
запись нельзя. Но без записи в DNS адрес по нему не берётся.

## Что ещё смотрит на старое имя

* **Бот и веб-админка** — `FLEET_API_URL` в окружении их служб. До сих пор
  они ходили к API через публичный домен, то есть через чужой Caddy; домен
  уезжает, Caddy из цепочки уходит. Оверлей `docker-compose.origin.yml`
  публикует API на петле, и адрес становится локальным:

  ```bash
  sed -i 's|^FLEET_API_URL=.*|FLEET_API_URL=http://127.0.0.1:8000|' /etc/router-bot.env /etc/router-webadmin.env 2>/dev/null; systemctl restart router-bot router-webadmin
  ```

  Путь к файлу окружения свой у каждой установки — посмотреть
  `systemctl cat router-bot | grep EnvironmentFile`.
* **Прошивка и списки** — отложены, но адрес им нужен до выпуска партии:
  зашитый в образ он меняется только перевыпуском. Когда дойдут руки —
  поднять `cdn.titanvps.pro` по заготовкам в конфигах и прописать
  `https://cdn.titanvps.pro/firmware/manifest.json`, а списки перевести
  туда же со старого `vm171085`.
* **Вебхук Platega** — адрес колбэка в личном кабинете провайдера.
* **Документы для клиента** — уже переписаны на `routers.titanvps.pro`.

## Откат

Шаги 4 → 2 в обратном порядке: убрать конфиги из `conf.d` и `stream.d`,
вернуть ноде публикацию `443:443`, поднять её. Витрина при этом остаётся
доступной по старому имени, пока не сделан шаг 7.
