#!/bin/bash
# Переезд frps на новую машину. Запускать на СТАРОЙ.
#
# Копирует бинарь, конфиг и unit как есть — той же версии. Обновлять frp
# заодно нельзя: это вторая правка одновременно с первой, и при поломке
# не понять, какая виновата.
#
# Ничего не выключает и не удаляет. Старый frps продолжает работать, и весь
# парк остаётся на нём: переключение произойдёт только когда вы перенесёте
# IP. До этого момента откат — это просто ничего не делать.
#
#   bash move-frps.sh [IP новой машины]

NEW_HOST="${1:-94.228.166.98}"
UNIT="/etc/systemd/system/frps.service"
INI="/etc/frp/frps.ini"
SSH_OPTS=(-o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)

step() { printf '\n== %s\n' "$1"; }
die() { printf '\nОСТАНОВ: %s\n' "$1"; exit 1; }

step "Что переносим"
[ -f "$UNIT" ] || die "нет $UNIT — frps поставлен иначе, переносите руками"
[ -f "$INI" ] || die "нет $INI"

BIN="$(awk -F'=' '/^ExecStart=/{print $2}' "$UNIT" | awk '{print $1}')"
[ -n "$BIN" ] || die "в $UNIT не нашёлся ExecStart"
[ -x "$BIN" ] || die "бинарь $BIN не найден или не исполняемый"

printf 'бинарь : %s\n' "$BIN"
printf 'версия : %s\n' "$("$BIN" -v 2>&1 | head -1)"
printf 'конфиг : %s\n' "$INI"

# Токен — единственное, без чего новый сервер отвергнет весь парк разом,
# и выглядеть это будет как «роутеры пропали».
grep -qE '^[[:space:]]*token[[:space:]]*=' "$INI" \
  || printf 'ВНИМАНИЕ: в конфиге нет строки token — сверьте с FRP_TOKEN вручную\n'

step "Копия на случай отката"
BACKUP="/root/frps-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
tar -czf "$BACKUP" -C / "${BIN#/}" "${INI#/}" "${UNIT#/}" || die "не собралась копия"
printf 'копия: %s\n' "$BACKUP"

step "Связь с новой машиной"
ssh "${SSH_OPTS[@]}" "root@$NEW_HOST" 'echo ok' >/dev/null \
  || die "не зашёл по ssh на root@$NEW_HOST. Сначала выполните: ssh-copy-id root@$NEW_HOST"

# Занятый порт означает, что там уже что-то слушает, и наш frps молча
# не поднимется. Лучше узнать сейчас, чем после переноса адреса.
BUSY="$(ssh "${SSH_OPTS[@]}" "root@$NEW_HOST" \
  "ss -ltn 2>/dev/null | grep -E ':(8443|7500)[[:space:]]' | head -3")"
[ -z "$BUSY" ] || printf 'ВНИМАНИЕ: на новой машине уже занято:\n%s\n' "$BUSY"

step "Копирую файлы"
scp "${SSH_OPTS[@]}" "$BIN" "$INI" "$UNIT" "root@$NEW_HOST:/tmp/" || die "не скопировалось"

step "Ставлю и запускаю на новой машине"
ssh "${SSH_OPTS[@]}" "root@$NEW_HOST" "BIN='$BIN' bash -s" <<'REMOTE'
NAME="$(basename "$BIN")"
mkdir -p "$(dirname "$BIN")" /etc/frp || exit 1
# Останавливаем до подмены: Linux не даёт писать поверх работающего
# бинаря, и повторный запуск скрипта упирался бы в «Text file busy».
systemctl stop frps 2>/dev/null
install -m 755 "/tmp/$NAME" "$BIN" || exit 1
# 600, а не как лежало: в конфиге токен ко всему парку, а /etc/frp
# на многих машинах читаем всем.
install -m 600 /tmp/frps.ini /etc/frp/frps.ini || exit 1
install -m 644 /tmp/frps.service /etc/systemd/system/frps.service || exit 1
rm -f "/tmp/$NAME" /tmp/frps.ini /tmp/frps.service
systemctl daemon-reload
systemctl enable frps >/dev/null 2>&1
systemctl restart frps
sleep 3
printf 'служба : %s\n' "$(systemctl is-active frps)"
printf 'слушает:\n%s\n' "$(ss -ltn 2>/dev/null | grep -E ':(8443|7500)[[:space:]]' || echo '  ничего — смотрите journalctl -u frps -n 50')"
[ "$(systemctl is-active frps)" = "active" ] || exit 1
REMOTE

# Отказ на той стороне здесь виден, а не теряется в выводе: следующий шаг —
# перенос адреса, и делать его на неподнявшемся сервере нельзя.
[ $? -eq 0 ] || die "на новой машине frps не поднялся. IP не переносите: старый продолжает работать"

step "Готово. Дальше — руками, по порядку"
cat <<NEXT

Новый frps поднят и ждёт. Старый работает, парк на нём — сейчас ничего
не сломано, и откат это просто ничего не делать.

1. Откройте порты на новой машине (сама она этого не делает намеренно:
   включение ufw вслепую отрезает ssh):

   ufw status | head -1
   ufw allow 22/tcp comment 'ssh'
   ufw allow 8443/tcp comment 'frps: роутеры'
   ufw allow from 82.197.73.251 to any port 7500 proto tcp comment 'dashboard'
   ufw status numbered

   Если ufw был inactive — включать отдельно и только убедившись,
   что в списке есть 22/tcp ALLOW: иначе машина закроется от вас самих.

2. Перенесите IP на $NEW_HOST. Роутеры придут сами за минуту-две:
   имя frp.pandora361.online ведёт на этот адрес, и другого они не знают.

3. На машине с API перезапустите visitor, чтобы он не ждал своего таймаута:

   cd /opt/router-shop && docker compose restart frpc worker

4. Через минуту проверьте, что вернулись все:

   cd /opt/router-shop && docker compose exec -T postgres psql -U "\${POSTGRES_USER:-router_shop}" -d "\${POSTGRES_DB:-router_shop}" -tAc "select count(*) filter (where frp_online) || ' на связи из ' || count(*) from devices"

   Меньше, чем было, — старую машину не трогайте, смотрите на новой:
   journalctl -u frps -n 50

5. Старую машину не удаляйте неделю. Вернуть IP обратно — минута,
   поднять всё заново — вечер. Копия конфига лежит в $BACKUP.

NEXT
