#!/bin/bash

# Скрипт установки subpage
# Использование: ./install.sh

set -e

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Функция для вывода сообщений
info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Проверка, что скрипт запущен от root
if [ "$EUID" -ne 0 ]; then 
    error "Пожалуйста, запустите скрипт от root: sudo ./install.sh"
    exit 1
fi

# Определяем директорию установки
INSTALL_DIR=$(pwd)
info "Директория установки: $INSTALL_DIR"

# Ждём освобождения dpkg/apt lock (unattended-upgrades и др.)
wait_apt_lock() {
  local i=0
  while fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock /var/cache/apt/archives/lock >/dev/null 2>&1; do
    if [ $i -eq 0 ]; then
      warn "Ожидание освобождения apt lock (фоновые обновления)..."
    fi
    sleep 3
    i=$((i+1))
    if [ $i -ge 40 ]; then
      warn "apt lock занят слишком долго, завершаем unattended-upgrades..."
      systemctl stop unattended-upgrades 2>/dev/null || true
      killall unattended-upgrades 2>/dev/null || true
      sleep 2
      break
    fi
  done
}

# Проверка наличия Python 3
if ! command -v python3 &> /dev/null; then
    error "Python 3 не найден. Установите Python 3."
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
info "Найден Python: $PYTHON_VERSION"

# Проверка и установка необходимых пакетов
info "Проверка необходимых пакетов..."
NEED_INSTALL=false

# Определяем версию Python для установки правильного пакета venv
PYTHON_VERSION_FULL=$(python3 --version | awk '{print $2}')
PYTHON_MAJOR_MINOR=$(echo "$PYTHON_VERSION_FULL" | cut -d. -f1,2)
VENV_PACKAGE="python${PYTHON_MAJOR_MINOR}-venv"

# Проверка pip
if ! command -v pip3 &> /dev/null; then
    warn "pip3 не найден. Будет установлен."
    NEED_INSTALL=true
fi

# Проверка python3-venv через dpkg
if ! dpkg -l | grep -q "^ii.*${VENV_PACKAGE} "; then
    warn "${VENV_PACKAGE} не установлен. Будет установлен."
    NEED_INSTALL=true
fi

# Установка недостающих пакетов
if [ "$NEED_INSTALL" = true ]; then
    info "Обновление списка пакетов..."
    wait_apt_lock
    apt-get update
    
    PACKAGES=""
    if ! command -v pip3 &> /dev/null; then
        PACKAGES="$PACKAGES python3-pip"
    fi
    
    if ! dpkg -l | grep -q "^ii.*${VENV_PACKAGE} "; then
        PACKAGES="$PACKAGES $VENV_PACKAGE"
    fi
    
    if [ -n "$PACKAGES" ]; then
        info "Установка пакетов: $PACKAGES"
        wait_apt_lock
        apt-get install -y $PACKAGES
        info "Пакеты установлены"
    fi
fi

# Дополнительная проверка: пытаемся создать тестовое venv
info "Проверка возможности создания виртуального окружения..."
TEST_VENV_DIR="/tmp/test_venv_$$"
if python3 -m venv "$TEST_VENV_DIR" 2>/dev/null; then
    rm -rf "$TEST_VENV_DIR"
    info "Проверка прошла успешно"
else
    warn "Проверка создания venv не удалась. Устанавливаю ${VENV_PACKAGE}..."
    apt-get update
    apt-get install -y "$VENV_PACKAGE"
    info "Пакет ${VENV_PACKAGE} установлен"
fi

# Создание виртуального окружения
info "Создание виртуального окружения..."
# Проверяем не только наличие директории, но и ключевых файлов
if [ -d "venv" ] && [ -f "venv/bin/activate" ] && [ -f "venv/bin/python3" ]; then
    info "Виртуальное окружение уже существует и выглядит корректно"
else
    if [ -d "venv" ]; then
        warn "Обнаружена некорректная директория venv. Удаляю и пересоздаю..."
        rm -rf venv
    fi
    
    if python3 -m venv venv; then
        info "Виртуальное окружение создано"
    else
        error "Не удалось создать виртуальное окружение"
        error "Убедитесь, что установлен пакет python3-venv: apt install python3-venv"
        exit 1
    fi
fi

# Активация виртуального окружения и установка зависимостей
info "Установка зависимостей..."
if [ ! -f "venv/bin/activate" ]; then
    error "Файл venv/bin/activate не найден. Виртуальное окружение некорректно."
    error "Удалите директорию venv и запустите установку снова: rm -rf venv && ./install.sh"
    exit 1
fi

source venv/bin/activate
if [ $? -ne 0 ]; then
    error "Не удалось активировать виртуальное окружение"
    exit 1
fi

pip install --upgrade pip
pip install -r requirements.txt
info "Зависимости установлены"

# Установка и настройка Redis
info "Проверка Redis..."
if ! command -v redis-server &> /dev/null; then
    warn "Redis не найден. Устанавливаю Redis..."
    wait_apt_lock
    apt-get update
    wait_apt_lock
    apt-get install -y redis-server
    info "Redis установлен"
else
    info "Redis уже установлен"
fi

# Установка UFW (файрвол)
info "Проверка UFW..."
if ! command -v ufw &> /dev/null; then
    warn "UFW не найден. Устанавливаю UFW..."
    wait_apt_lock
    apt-get update
    wait_apt_lock
    apt-get install -y ufw
    info "UFW установлен"
else
    info "UFW уже установлен"
fi

# Настройка Redis для production
info "Настройка Redis..."
REDIS_CONF="/etc/redis/redis.conf"
if [ -f "$REDIS_CONF" ]; then
    # Отключаем опасные команды
    if ! grep -q "^rename-command FLUSHALL" "$REDIS_CONF"; then
        echo "" >> "$REDIS_CONF"
        echo "# Security: disable dangerous commands" >> "$REDIS_CONF"
        echo "rename-command FLUSHALL \"\"" >> "$REDIS_CONF"
        echo "rename-command FLUSHDB \"\"" >> "$REDIS_CONF"
        echo "rename-command CONFIG \"\"" >> "$REDIS_CONF"
        info "Опасные команды Redis отключены"
    fi
    
    # Настраиваем память (максимум 256MB для кэша)
    if ! grep -q "^maxmemory" "$REDIS_CONF"; then
        echo "maxmemory 256mb" >> "$REDIS_CONF"
        echo "maxmemory-policy allkeys-lru" >> "$REDIS_CONF"
        info "Настроена политика памяти Redis"
    fi
    
    # Включаем защиту от записи (опционально, можно закомментировать если нужна запись)
    # if ! grep -q "^protected-mode" "$REDIS_CONF"; then
    #     echo "protected-mode yes" >> "$REDIS_CONF"
    # fi
fi

# Запуск и включение автозапуска Redis
info "Запуск Redis..."
systemctl enable redis-server
systemctl restart redis-server

# Проверка статуса Redis
sleep 1
if systemctl is-active --quiet redis-server; then
    info "Redis успешно запущен"
else
    warn "Redis не запустился. Проверьте конфигурацию: systemctl status redis-server"
fi

# Создание .env файла из примера, если его нет
if [ ! -f ".env" ]; then
    if [ -f "env.example" ]; then
        info "Создание .env файла из env.example..."
        cp env.example .env
        warn "Не забудьте отредактировать .env файл с вашими настройками!"
    else
        warn "env.example не найден. Создайте .env файл вручную."
    fi
else
    info ".env файл уже существует"
fi

# Создание systemd service файла
SERVICE_NAME="subpage"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

info "Создание systemd service файла..."

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Subpage Subscription Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment="PATH=$INSTALL_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=$INSTALL_DIR"
ExecStart=$INSTALL_DIR/venv/bin/gunicorn run:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:3004 --timeout 120 --graceful-timeout 30
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
LimitNOFILE=65536
MemoryMax=1G

[Install]
WantedBy=multi-user.target
EOF

info "Service файл создан: $SERVICE_FILE"

# Установка CLI утилиты subpage
info "Установка CLI утилиты управления..."
if [ -f "subpage_cli.py" ]; then
    # Исправляем окончания строк (CRLF -> LF) для Linux совместимости
    info "Исправление окончаний строк в subpage_cli.py..."
    sed -i 's/\r$//' subpage_cli.py
    
    chmod +x subpage_cli.py
    cp subpage_cli.py /usr/local/bin/subpage
    chmod +x /usr/local/bin/subpage
    
    # Убеждаемся, что установленный файл имеет правильные окончания строк
    sed -i 's/\r$//' /usr/local/bin/subpage
    
    info "CLI утилита установлена: /usr/local/bin/subpage"
    info "Использование: просто введите 'subpage' для открытия меню управления"
else
    warn "Файл subpage_cli.py не найден. CLI утилита не будет установлена."
fi

# Перезагрузка systemd
info "Перезагрузка systemd daemon..."
systemctl daemon-reload

# Включение автозапуска
info "Включение автозапуска сервиса..."
systemctl enable ${SERVICE_NAME}.service

# Запуск сервиса
info "Запуск сервиса..."
systemctl start ${SERVICE_NAME}.service

# Проверка статуса
sleep 2
if systemctl is-active --quiet ${SERVICE_NAME}.service; then
    info "Сервис успешно запущен!"
    info "Статус: $(systemctl status ${SERVICE_NAME}.service --no-pager -l | head -n 3 | tail -n 1)"
else
    error "Сервис не запустился. Проверьте логи: journalctl -u ${SERVICE_NAME}.service -n 50"
    exit 1
fi

# Информация о командах управления
echo ""
info "=========================================="
info "Установка завершена успешно!"
info "=========================================="
echo ""
info "Консоль управления subpage:"
echo "  subpage               - Открыть интерактивное меню управления"
echo "  subpage restart       - Перезапустить сервис"
echo "  subpage logs          - Показать последние 50 строк логов"
echo "  subpage logs -f       - Показать логи в реальном времени"
echo "  subpage logs -n 100   - Показать последние 100 строк логов"
echo "  subpage clear-cache   - Очистить кеш subscription_cache.db"
echo "  subpage edit-env      - Редактировать .env файл через nano"
echo "  subpage status        - Показать статус сервиса и информацию о кеше"
echo "  subpage help          - Показать справку"
echo ""
info "Прямое управление через systemctl:"
echo "  systemctl status ${SERVICE_NAME}.service"
echo "  systemctl restart ${SERVICE_NAME}.service"
echo "  journalctl -u ${SERVICE_NAME}.service -f"
echo ""

# Предложение открыть CLI меню
echo ""
read -p "Открыть консоль управления subpage сейчас? (y/n): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo ""
    info "Открываю консоль управления subpage..."
    echo ""
    # Запускаем CLI утилиту
    if [ -f "/usr/local/bin/subpage" ]; then
        /usr/local/bin/subpage
    else
        warn "CLI утилита не найдена. Запустите вручную: subpage"
    fi
else
    echo ""
    info "Для открытия консоли управления выполните: subpage"
    echo ""
fi

