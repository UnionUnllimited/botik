#!/usr/bin/env bash
# Первичный выпуск TLS-сертификатов. Запускать один раз на чистом сервере,
# когда A-записи доменов уже указывают на него.
#
#   ./deploy/init-letsencrypt.sh api.example.ru admin.example.ru admin@example.ru
#
# Скрипт сначала кладёт самоподписанные заглушки (иначе nginx не стартует и
# ACME-проверка не пройдёт), поднимает nginx и запрашивает боевые сертификаты.
set -euo pipefail

API_DOMAIN="${1:?Укажите домен API}"
ADMIN_DOMAIN="${2:?Укажите домен админки}"
EMAIL="${3:?Укажите e-mail для Let's Encrypt}"
STAGING="${STAGING:-0}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="$ROOT/deploy/certbot"
COMPOSE="docker compose"

mkdir -p "$CERT_DIR/conf" "$CERT_DIR/www"

for domain in "$API_DOMAIN" "$ADMIN_DOMAIN"; do
  live="$CERT_DIR/conf/live/$domain"
  if [ -f "$live/fullchain.pem" ]; then
    echo "== Сертификат для $domain уже есть, пропускаем заглушку"
    continue
  fi
  echo "== Временный самоподписанный сертификат для $domain"
  mkdir -p "$live"
  docker run --rm --entrypoint openssl -v "$CERT_DIR/conf:/etc/letsencrypt" certbot/certbot \
    req -x509 -nodes -newkey rsa:2048 -days 1 \
      -keyout "/etc/letsencrypt/live/$domain/privkey.pem" \
      -out "/etc/letsencrypt/live/$domain/fullchain.pem" \
      -subj "/CN=$domain"
done

echo "== Поднимаем nginx"
$COMPOSE up -d nginx
sleep 5

for domain in "$API_DOMAIN" "$ADMIN_DOMAIN"; do
  echo "== Запрашиваем сертификат Let's Encrypt для $domain"
  rm -rf "$CERT_DIR/conf/live/$domain" "$CERT_DIR/conf/archive/$domain" \
         "$CERT_DIR/conf/renewal/$domain.conf"
  staging_arg=""
  [ "$STAGING" != "0" ] && staging_arg="--staging"
  $COMPOSE run --rm --entrypoint certbot certbot \
    certonly --webroot -w /var/www/certbot \
    $staging_arg \
    -d "$domain" \
    --email "$EMAIL" \
    --agree-tos --no-eff-email --non-interactive --rsa-key-size 4096
done

echo "== Перечитываем конфиг nginx"
$COMPOSE exec nginx nginx -s reload
echo "Готово. Сертификаты в $CERT_DIR/conf/live"
