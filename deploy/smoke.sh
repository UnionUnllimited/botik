#!/usr/bin/env bash
# Проверка снаружи: отвечают ли сайт, админка и служебные пути после выката.
#
# Адрес берётся из API_PUBLIC_BASE_URL в .env — это тот же домен, что видит
# клиент. Проверять контейнер напрямую смысла мало: чаще ломается не он,
# а маршрут в обратном прокси, и снаружи это выглядит как несобранный сайт.
#
#   bash deploy/smoke.sh            # адрес из .env
#   bash deploy/smoke.sh https://...  # явный адрес
set -uo pipefail

cd "$(dirname "$0")/.."

BASE="${1:-}"
if [[ -z "$BASE" && -f .env ]]; then
  line="$(grep -E '^API_PUBLIC_BASE_URL=' .env | tail -1)"
  BASE="${line#*=}"
  BASE="${BASE%$'\r'}"   # .env, отредактированный в Windows, приносит возврат каретки
  BASE="${BASE//\"/}"
  BASE="${BASE//\'/}"
fi
BASE="${BASE%/}"

if [[ -z "$BASE" ]]; then
  echo "Не знаю, что проверять: задайте API_PUBLIC_BASE_URL в .env или передайте адрес аргументом." >&2
  exit 2
fi

echo "Проверяю $BASE"
failed=0
health_ok=0

status_of() {
  curl -sS -o /dev/null -w '%{http_code}' --max-time "${2:-15}" "$BASE$1" 2>/dev/null
}

# make deploy заканчивается раньше, чем приложение успевает подняться, и запуск
# проверки сразу за ним ловил 502 на всём. Ждём готовности, а не «пробуем разок».
# Ждём около полуминуты: столько занимает старт api вместе с проверкой базы.
printf '  жду готовности'
for _ in $(seq 1 10); do
  if [[ "$(status_of /healthz 5)" == "200" ]]; then
    health_ok=1
    break
  fi
  printf '.'
  sleep 3
done
printf '\n'

check() {
  local path="$1" expected="$2" what="$3"
  local code
  code="$(status_of "$path")"
  if [[ "$code" == "$expected" ]]; then
    printf '  ok   %-28s %s (%s)\n' "$path" "$code" "$what"
  else
    printf '  ПЛОХО %-27s %s, ждали %s (%s)\n' "$path" "${code:-нет ответа}" "$expected" "$what"
    failed=1
  fi
}

check /healthz  200 "процесс жив"
check /readyz   200 "база и redis на связи"
# Ручки каталога и парка закрыты общим секретом. Ждём 401 — «токен не подошёл»:
# это ответ настроенного сервера. Если пришло 404, значит API_FLEET_TOKEN пуст,
# и тогда не работают ни каталог в боте, ни вкладка роутеров в его админке.
check /api/v1/catalog/products 401 "каталог закрыт без токена"
check /api/v1/fleet/routers    401 "парк закрыт без токена"
# Панель роутера без билета объясняется, а не пускает.
check /cgi-bin/luci/ 409 "панель закрыта без билета"

if [[ "$failed" -ne 0 ]]; then
  if [[ "$health_ok" -eq 1 ]]; then
    # Приложение живо, а какой-то путь нет — значит запрос до него не доходит.
    cat >&2 <<'HINT'

/healthz отвечает, а остальное нет — дело в обратном прокси: он должен отдавать
контейнеру api пути /api, /webhooks и корневые пути панели роутера
(/cgi-bin, /luci-static, /ubus). Своей админки у нас больше нет — интерфейс
один, в стороннем продукте, и он ходит к нам по /api.
HINT
  else
    cat >&2 <<'HINT'

Приложение не отвечает целиком. Смотреть логи:
  docker compose ps api && docker compose logs api --tail=50
HINT
  fi
  exit 1
fi

echo "Всё отвечает."
