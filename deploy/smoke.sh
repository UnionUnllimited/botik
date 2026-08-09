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

check() {
  local path="$1" expected="$2" what="$3"
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$BASE$path" 2>/dev/null)"
  if [[ "$code" == "$expected" ]]; then
    printf '  ok   %-28s %s (%s)\n' "$path" "$code" "$what"
  else
    printf '  ПЛОХО %-27s %s, ждали %s (%s)\n' "$path" "${code:-нет ответа}" "$expected" "$what"
    failed=1
  fi
}

check /healthz  200 "процесс жив"
check /readyz   200 "база и redis на связи"
check /         200 "витрина"
check /login    200 "форма входа"
check /register 200 "форма регистрации"
# Кабинет без сессии обязан уводить на вход, а не показывать чужие данные.
check /cabinet  303 "кабинет закрыт без входа"
check /admin/   303 "админка закрыта без входа"

if [[ "$failed" -ne 0 ]]; then
  cat >&2 <<'HINT'

Если не отвечает только корень, а /healthz жив — дело в обратном прокси:
он должен отдавать контейнеру api весь домен, а не отдельные пути.
Нужный блок Caddy — в README, раздел «Сайт занимает корень домена».
HINT
  exit 1
fi

echo "Всё отвечает."
