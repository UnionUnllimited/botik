#!/usr/bin/env bash
# Бэкап Postgres: дамп -> gzip -> ротация -> (опционально) выгрузка в S3.
# Ставится в cron на хосте:
#   15 4 * * * /opt/router-shop/deploy/backup.sh >> /var/log/router-shop-backup.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
set -a; [ -f .env ] && . ./.env; set +a

BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
FILE="$BACKUP_DIR/${POSTGRES_DB:-router_shop}-$STAMP.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date -u +%FT%TZ)] Дамп базы ${POSTGRES_DB:-router_shop}"
docker compose exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-router_shop}" -d "${POSTGRES_DB:-router_shop}" \
    --no-owner --no-privileges --format=plain \
  | gzip -9 > "$FILE"

SIZE="$(du -h "$FILE" | cut -f1)"
echo "[$(date -u +%FT%TZ)] Готово: $FILE ($SIZE)"

# Проверяем, что архив не битый и не пустой.
if ! gzip -t "$FILE"; then
  echo "ОШИБКА: архив повреждён, удаляю" >&2
  rm -f "$FILE"
  exit 1
fi
if [ "$(stat -c%s "$FILE")" -lt 1024 ]; then
  echo "ОШИБКА: дамп подозрительно мал" >&2
  exit 1
fi

echo "[$(date -u +%FT%TZ)] Ротация старше $KEEP_DAYS дней"
find "$BACKUP_DIR" -name '*.sql.gz' -type f -mtime "+$KEEP_DAYS" -print -delete

# Выгрузка в S3-совместимое хранилище, если настроено в .env:
#   BACKUP_S3_BUCKET, BACKUP_S3_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
  echo "[$(date -u +%FT%TZ)] Выгрузка в S3: $BACKUP_S3_BUCKET"
  docker run --rm \
    -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
    -v "$BACKUP_DIR:/backup:ro" \
    amazon/aws-cli:latest \
    s3 cp "/backup/$(basename "$FILE")" "s3://$BACKUP_S3_BUCKET/$(basename "$FILE")" \
    ${BACKUP_S3_ENDPOINT:+--endpoint-url "$BACKUP_S3_ENDPOINT"}
  echo "[$(date -u +%FT%TZ)] Выгружено"
fi
