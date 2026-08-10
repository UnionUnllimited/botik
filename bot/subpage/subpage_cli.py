#!/usr/bin/env python3
"""
CLI утилита для управления subpage сервисом
Использование: subpage
"""
import os
import sys
import subprocess
import random
import string
from pathlib import Path

# Определяем путь к директории subpage
SERVICE_NAME = 'subpage'
SUBPAGE_VERSION = '1.9'
UPDATE_URL = 'https://update.3xstore.ru/subpage/install.sh'

def find_subpage_dir():
    """Находит директорию subpage"""
    # Пытаемся получить WorkingDirectory из systemd service файла
    service_file = Path('/etc/systemd/system/subpage.service')
    if service_file.exists():
        try:
            with open(service_file, 'r') as f:
                for line in f:
                    if line.startswith('WorkingDirectory='):
                        work_dir = line.split('=', 1)[1].strip()
                        work_path = Path(work_dir)
                        if work_path.exists() and (work_path / 'run.py').exists():
                            return work_path
        except Exception:
            pass
    
    # Проверяем стандартные пути
    possible_paths = [
        Path('/root/subpage'),
        Path('/opt/subpage'),
        Path.home() / 'subpage',
    ]
    
    # Если скрипт запущен из директории subpage
    script_path = Path(__file__).parent.absolute()
    if (script_path / '.env').exists() or (script_path / 'run.py').exists():
        return script_path
    
    # Ищем по стандартным путям
    for path in possible_paths:
        if path.exists() and ((path / '.env').exists() or (path / 'run.py').exists()):
            return path
    
    # Если не нашли, используем директорию скрипта
    return script_path

SCRIPT_DIR = find_subpage_dir()
ENV_PATH = SCRIPT_DIR / '.env'

# Получаем порт из .env файла
def get_port_from_env():
    """Получает порт из .env файла"""
    default_port = 3004
    if ENV_PATH.exists():
        try:
            with open(ENV_PATH, 'r') as f:
                for line in f:
                    if line.startswith('PORT='):
                        port = line.split('=', 1)[1].strip()
                        try:
                            return int(port)
                        except ValueError:
                            return default_port
        except Exception:
            pass
    return default_port

SUBPAGE_PORT = get_port_from_env()


def run_command(cmd, check=True):
    """Выполняет команду и возвращает результат"""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            check=check,
            capture_output=True,
            text=True
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.CalledProcessError as e:
        return e.stdout, e.stderr, e.returncode


def clear_screen():
    """Очищает экран"""
    os.system('clear' if os.name != 'nt' else 'cls')


def show_menu():
    """Показывает главное меню"""
    clear_screen()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║          Subpage - Консоль управления сервисом           ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print(f"📁 Директория: {SCRIPT_DIR}")
    print(f"🔖 Текущая версия SUBPAGE: {SUBPAGE_VERSION}")
    print()
    print("Выберите действие:")
    print()
    print("  1) 🔄 Перезапустить сервис")
    print("  2) 📋 Просмотр логов (последние 50 строк)")
    print("  3) 📋 Просмотр логов (последние 100 строк)")
    print("  4) 📋 Просмотр логов в реальном времени")
    print("  5) 📊 Статус сервиса")
    print("  6) ⬆️  Обновить subpage до последней версии")
    print("  7) 📝 Редактировать .env файл")
    print("  8) 🔥 Управление файрволом")
    print("  9) 🌐 Управление Nginx")
    print(" 10) ❌ Выход")
    print()
    print("─" * 60)


def get_choice():
    """Получает выбор пользователя"""
    try:
        choice = input("Введите номер действия (1-10): ").strip()
        return choice
    except (EOFError, KeyboardInterrupt):
        print("\n\nВыход...")
        sys.exit(0)


def update_subpage():
    """Обновляет subpage через установочный скрипт с update.3xstore.ru"""
    print("\n⬆️  Обновление subpage")
    print("─" * 60)
    print(f"🔖 Текущая версия: {SUBPAGE_VERSION}")
    print(f"🌐 Источник:       {UPDATE_URL}")
    print()
    print("⚠️  Будет выполнена команда:")
    print(f"    bash <(curl -sSL {UPDATE_URL})")
    print()
    print("💡 Установщик может перезаписать файлы, перезапустить сервис")
    print("   и попросить ввести данные. Перед обновлением желательно")
    print("   сделать резервную копию .env.")
    print()
    try:
        response = input("Продолжить обновление? (y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n\nОтмена обновления.")
        return

    if response != 'y':
        print("\n❌ Обновление отменено.")
        input("\nНажмите Enter для продолжения...")
        return

    print("\n🚀 Запуск установщика...\n")
    # Используем os.system, чтобы пользователь видел вывод установщика в реальном
    # времени и мог отвечать на интерактивные вопросы. process substitution
    # `<(...)` требует bash, поэтому явно вызываем bash -c.
    cmd = f'bash -c "bash <(curl -sSL {UPDATE_URL})"'
    code = os.system(cmd)

    print()
    if code == 0:
        print("✅ Установщик завершился успешно.")
        print("💡 Если обновился сам CLI — перезапустите команду `subpage`.")
    else:
        print(f"❌ Установщик завершился с кодом {code}.")
    input("\nНажмите Enter для продолжения...")


def restart_service():
    """Перезапускает сервис subpage"""
    print("\n🔄 Перезапуск сервиса subpage...")
    stdout, stderr, code = run_command(f"systemctl restart {SERVICE_NAME}.service")
    if code == 0:
        print("✅ Сервис успешно перезапущен")
        print("\n📊 Текущий статус:")
        stdout, _, _ = run_command(f"systemctl status {SERVICE_NAME}.service --no-pager -l")
        print(stdout)
    else:
        print(f"❌ Ошибка при перезапуске: {stderr}")
    input("\nНажмите Enter для продолжения...")


def view_logs(lines=50, follow=False):
    """Просмотр логов сервиса"""
    if follow:
        print(f"\n📋 Просмотр логов сервиса {SERVICE_NAME} в реальном времени...")
        print("💡 Нажмите Ctrl+C для выхода\n")
        os.system(f"journalctl -u {SERVICE_NAME}.service -f")
    else:
        print(f"\n📋 Последние {lines} строк логов:\n")
        stdout, stderr, code = run_command(f"journalctl -u {SERVICE_NAME}.service -n {lines} --no-pager")
        if code == 0:
            print(stdout)
        else:
            print(f"❌ Ошибка при получении логов: {stderr}")
        input("\nНажмите Enter для продолжения...")


def show_status():
    """Показывает статус сервиса"""
    print("\n📊 Статус сервиса:")
    print("─" * 60)
    stdout, stderr, code = run_command(f"systemctl status {SERVICE_NAME}.service --no-pager -l")
    print(stdout)
    
    # Показываем информацию о директории и файлах
    print("\n📁 Информация о директории:")
    print(f"   Путь: {SCRIPT_DIR}")
    
    # Показываем информацию о .env
    print("\n⚙️  Конфигурация:")
    if ENV_PATH.exists():
        print(f"   Файл .env: {ENV_PATH} (существует)")
    else:
        print(f"   Файл .env: {ENV_PATH} (не найден)")

    input("\nНажмите Enter для продолжения...")


def edit_env():
    """Редактирует .env файл через nano"""
    # Проверяем наличие nano
    stdout, stderr, code = run_command("which nano", check=False)
    if code != 0:
        print("\n❌ Редактор nano не найден. Установите его: apt-get install nano")
        input("\nНажмите Enter для продолжения...")
        return
    
    if not ENV_PATH.exists():
        print(f"\n⚠️  Файл .env не найден: {ENV_PATH}")
        print("💡 Создать файл из env.example? (y/n): ", end='')
        try:
            response = input().strip().lower()
            if response == 'y':
                env_example = SCRIPT_DIR / 'env.example'
                if env_example.exists():
                    import shutil
                    shutil.copy(env_example, ENV_PATH)
                    print(f"✅ Файл .env создан из env.example")
                else:
                    print("❌ Файл env.example не найден")
                    input("\nНажмите Enter для продолжения...")
                    return
            else:
                input("\nНажмите Enter для продолжения...")
                return
        except (EOFError, KeyboardInterrupt):
            print("\n\nОтмена...")
            return
    
    print(f"\n📝 Открываю .env файл в nano: {ENV_PATH}")
    print("💡 После сохранения изменений перезапустите сервис (пункт 1 в меню)")
    print("\nНажмите Enter для открытия редактора...")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        return
    
    os.system(f"nano {ENV_PATH}")


def check_ufw_installed():
    """Проверяет, установлен ли ufw"""
    stdout, stderr, code = run_command("which ufw", check=False)
    return code == 0


def show_firewall_status():
    """Показывает статус файрвола"""
    if not check_ufw_installed():
        print("\n❌ UFW не установлен. Установите его: apt-get install ufw")
        input("\nНажмите Enter для продолжения...")
        return
    
    print("\n🔥 Статус файрвола:")
    print("─" * 60)
    stdout, stderr, code = run_command("ufw status verbose")
    if code == 0:
        print(stdout)
    else:
        print(f"❌ Ошибка при получении статуса: {stderr}")
    
    input("\nНажмите Enter для продолжения...")


def show_firewall_rules():
    """Показывает правила файрвола"""
    if not check_ufw_installed():
        print("\n❌ UFW не установлен. Установите его: apt-get install ufw")
        input("\nНажмите Enter для продолжения...")
        return
    
    print("\n🔥 Правила файрвола:")
    print("─" * 60)
    stdout, stderr, code = run_command("ufw status numbered")
    if code == 0:
        print(stdout)
    else:
        print(f"❌ Ошибка при получении правил: {stderr}")
    
    input("\nНажмите Enter для продолжения...")


def enable_firewall():
    """Включает файрвол"""
    if not check_ufw_installed():
        print("\n❌ UFW не установлен. Установите его: apt-get install ufw")
        input("\nНажмите Enter для продолжения...")
        return
    
    print("\n⚠️  Включение файрвола может заблокировать доступ к серверу!")
    print("💡 Убедитесь, что порт SSH (обычно 22) открыт.")
    print("💡 Введите 'yes' для подтверждения: ", end='')
    try:
        response = input().strip().lower()
        if response != 'yes':
            print("❌ Отменено")
            input("\nНажмите Enter для продолжения...")
            return
    except (EOFError, KeyboardInterrupt):
        print("\n\nОтмена...")
        return
    
    print("\n🔥 Включение файрвола...")
    stdout, stderr, code = run_command("ufw --force enable")
    if code == 0:
        print("✅ Файрвол успешно включен")
        stdout, _, _ = run_command("ufw status")
        print(stdout)
    else:
        print(f"❌ Ошибка при включении файрвола: {stderr}")
    
    input("\nНажмите Enter для продолжения...")


def disable_firewall():
    """Выключает файрвол"""
    if not check_ufw_installed():
        print("\n❌ UFW не установлен. Установите его: apt-get install ufw")
        input("\nНажмите Enter для продолжения...")
        return
    
    print("\n⚠️  Выключение файрвола снижает безопасность сервера!")
    print("💡 Введите 'yes' для подтверждения: ", end='')
    try:
        response = input().strip().lower()
        if response != 'yes':
            print("❌ Отменено")
            input("\nНажмите Enter для продолжения...")
            return
    except (EOFError, KeyboardInterrupt):
        print("\n\nОтмена...")
        return
    
    print("\n🔥 Выключение файрвола...")
    stdout, stderr, code = run_command("ufw --force disable")
    if code == 0:
        print("✅ Файрвол успешно выключен")
    else:
        print(f"❌ Ошибка при выключении файрвола: {stderr}")
    
    input("\nНажмите Enter для продолжения...")


def allow_web_ports():
    """Открывает веб-порты (80, 443) для Nginx"""
    if not check_ufw_installed():
        print("\n❌ UFW не установлен. Установите его: apt-get install ufw")
        input("\nНажмите Enter для продолжения...")
        return
    
    print("\n🔥 Открытие веб-портов для Nginx...")
    print("💡 Будет открыто:")
    print("   - Порт 80 (HTTP)")
    print("   - Порт 443 (HTTPS)")
    print()
    
    # Открываем порт 80
    stdout, stderr, code = run_command("ufw allow 80/tcp comment 'HTTP (Nginx)'")
    if code == 0:
        print("✅ Порт 80 (HTTP) успешно открыт")
    else:
        print(f"⚠️  Ошибка при открытии порта 80: {stderr}")
    
    # Открываем порт 443
    stdout, stderr, code = run_command("ufw allow 443/tcp comment 'HTTPS (Nginx)'")
    if code == 0:
        print("✅ Порт 443 (HTTPS) успешно открыт")
    else:
        print(f"⚠️  Ошибка при открытии порта 443: {stderr}")
    
    print("\n💡 Примечание: Порт subpage (3004) не нужно открывать - он работает только локально через Nginx")
    
    input("\nНажмите Enter для продолжения...")


def deny_web_ports():
    """Закрывает веб-порты (80, 443)"""
    if not check_ufw_installed():
        print("\n❌ UFW не установлен. Установите его: apt-get install ufw")
        input("\nНажмите Enter для продолжения...")
        return
    
    print("\n⚠️  Закрытие веб-портов заблокирует доступ к сайту!")
    print("💡 Будет закрыто:")
    print("   - Порт 80 (HTTP)")
    print("   - Порт 443 (HTTPS)")
    print("💡 Введите 'yes' для подтверждения: ", end='')
    try:
        response = input().strip().lower()
        if response != 'yes':
            print("❌ Отменено")
            input("\nНажмите Enter для продолжения...")
            return
    except (EOFError, KeyboardInterrupt):
        print("\n\nОтмена...")
        return
    
    print("\n🔥 Закрытие веб-портов...")
    
    # Закрываем порт 80
    stdout, stderr, code = run_command("ufw delete allow 80/tcp")
    if code == 0:
        print("✅ Порт 80 (HTTP) успешно закрыт")
    else:
        print(f"⚠️  Ошибка при закрытии порта 80: {stderr}")
        print("💡 Попробуйте удалить правило вручную из списка правил")
    
    # Закрываем порт 443
    stdout, stderr, code = run_command("ufw delete allow 443/tcp")
    if code == 0:
        print("✅ Порт 443 (HTTPS) успешно закрыт")
    else:
        print(f"⚠️  Ошибка при закрытии порта 443: {stderr}")
        print("💡 Попробуйте удалить правило вручную из списка правил")
    
    input("\nНажмите Enter для продолжения...")


def allow_ssh_port():
    """Открывает порт SSH (22)"""
    if not check_ufw_installed():
        print("\n❌ UFW не установлен. Установите его: apt-get install ufw")
        input("\nНажмите Enter для продолжения...")
        return
    
    print("\n🔥 Открытие порта SSH (22)...")
    print("⚠️  Убедитесь, что SSH настроен безопасно!")
    stdout, stderr, code = run_command("ufw allow 22/tcp comment 'SSH'")
    if code == 0:
        print("✅ Порт 22 (SSH) успешно открыт")
    else:
        print(f"❌ Ошибка при открытии порта: {stderr}")
    
    input("\nНажмите Enter для продолжения...")


def get_email_for_certbot():
    """Запрашивает email у пользователя для Let's Encrypt"""
    print("\n📧 Email для Let's Encrypt (обязателен для уведомлений о безопасности)")
    print("💡 Certbot требует реальный email адрес")
    print("💡 Если не хотите указывать email, нажмите Enter (будет использован флаг --register-unsafely-without-email)")
    
    try:
        email = input("Введите email (или Enter для пропуска): ").strip()
        
        # Простая валидация email
        if email:
            if '@' in email and '.' in email.split('@')[1]:
                return email
            else:
                print("⚠️  Email выглядит невалидным. Попробуйте еще раз или нажмите Enter для пропуска.")
                return get_email_for_certbot()  # Рекурсивно запрашиваем снова
        else:
            # Пользователь пропустил - вернем None, чтобы использовать флаг без email
            return None
    except (EOFError, KeyboardInterrupt):
        print("\n\nОтмена...")
        return None


def wait_for_dpkg_lock():
    """Ожидает освобождения блокировки dpkg"""
    max_wait = 300
    waited = 0
    
    while True:
        stdout, _, code1 = run_command("fuser /var/lib/dpkg/lock-frontend 2>/dev/null", check=False)
        stdout, _, code2 = run_command("fuser /var/lib/dpkg/lock 2>/dev/null", check=False)
        stdout, _, code3 = run_command("lsof /var/lib/dpkg/lock-frontend 2>/dev/null", check=False)
        stdout, _, code4 = run_command("lsof /var/lib/dpkg/lock 2>/dev/null", check=False)
        
        if code1 != 0 and code2 != 0 and code3 != 0 and code4 != 0:
            break
        
        if waited >= max_wait:
            print("\n❌ Превышено время ожидания освобождения блокировки dpkg")
            print("💡 Попробуйте запустить позже или завершите процесс обновления вручную")
            return False
        
        if waited == 0:
            print("⏳ Обнаружен активный процесс обновления системы, ожидаю завершения...")
        
        import time
        time.sleep(5)
        waited += 5
        print(".", end="", flush=True)
    
    if waited > 0:
        print("\n✅ Блокировка освобождена, продолжаю установку...")
    
    return True


def install_nginx():
    """Устанавливает и настраивает Nginx с SSL"""
    print("\n🌐 Установка и настройка Nginx...")
    print("─" * 60)
    
    # Проверка прав root
    if os.geteuid() != 0:
        print("❌ Для установки Nginx требуются права root")
        print("💡 Запустите CLI с sudo: sudo subpage")
        input("\nНажмите Enter для продолжения...")
        return
    
    # Запрос домена
    print("\n📝 Введите домен для subpage (например: sub.example.com)")
    print("💡 Домен должен указывать на IP этого сервера")
    try:
        domain = input("Домен: ").strip()
        if not domain:
            print("❌ Домен не может быть пустым")
            input("\nНажмите Enter для продолжения...")
            return
    except (EOFError, KeyboardInterrupt):
        print("\n\nОтмена...")
        return
    
    # Запрашиваем email у пользователя
    email = get_email_for_certbot()
    use_no_email_flag = email is None
    
    if email:
        print(f"📧 Email для Let's Encrypt: {email}")
    else:
        print("⚠️  Email не указан, будет использован флаг --register-unsafely-without-email")
        print("💡 Вы не будете получать уведомления о безопасности и истечении сертификатов")
    
    # Проверка наличия nginx
    print("\n🔍 Проверка наличия nginx...")
    stdout, stderr, code = run_command("which nginx", check=False)
    if code != 0:
        print("📦 Установка nginx...")
        if not wait_for_dpkg_lock():
            input("\nНажмите Enter для продолжения...")
            return
        
        stdout, stderr, code = run_command("apt-get update", check=False)
        if code != 0:
            print(f"❌ Ошибка при обновлении пакетов: {stderr}")
            input("\nНажмите Enter для продолжения...")
            return
        
        if not wait_for_dpkg_lock():
            input("\nНажмите Enter для продолжения...")
            return
        
        stdout, stderr, code = run_command("apt-get install -y nginx", check=False)
        if code != 0:
            print(f"❌ Ошибка при установке nginx: {stderr}")
            input("\nНажмите Enter для продолжения...")
            return
        print("✅ nginx установлен")
    else:
        stdout, _, _ = run_command("nginx -v 2>&1")
        print(f"✅ nginx уже установлен: {stdout.strip()}")
    
    # Создание конфигурации nginx
    print("\n📝 Создание конфигурации nginx...")
    nginx_conf = "/etc/nginx/sites-available/subpage"
    nginx_enabled = "/etc/nginx/sites-enabled/subpage"
    
    # Создаем временную конфигурацию для получения сертификата
    temp_config = f"""# Конфигурация nginx для subpage
# Домен: {domain}

server {{
    listen 80;
    listen [::]:80;
    server_name {domain};

    # Временная конфигурация для certbot
    location / {{
        proxy_pass http://127.0.0.1:{SUBPAGE_PORT};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Таймауты
        proxy_connect_timeout 30s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
    }}
}}
"""
    
    try:
        with open(nginx_conf, 'w') as f:
            f.write(temp_config)
        print(f"✅ Конфигурация создана: {nginx_conf}")
    except Exception as e:
        print(f"❌ Ошибка при создании конфигурации: {e}")
        input("\nНажмите Enter для продолжения...")
        return
    
    # Создаем симлинк
    if os.path.exists(nginx_enabled):
        os.remove(nginx_enabled)
    os.symlink(nginx_conf, nginx_enabled)
    print(f"✅ Симлинк создан: {nginx_enabled}")
    
    # Проверка конфигурации
    print("\n🔍 Проверка конфигурации nginx...")
    stdout, stderr, code = run_command("nginx -t", check=False)
    if code != 0:
        print(f"❌ Ошибка в конфигурации nginx: {stderr}")
        input("\nНажмите Enter для продолжения...")
        return
    print("✅ Конфигурация nginx корректна")
    
    # Перезапуск nginx
    print("\n🔄 Перезапуск nginx...")
    stdout, stderr, code = run_command("systemctl restart nginx", check=False)
    if code != 0:
        print(f"❌ Ошибка при перезапуске nginx: {stderr}")
        input("\nНажмите Enter для продолжения...")
        return
    stdout, stderr, code = run_command("systemctl enable nginx", check=False)
    print("✅ nginx запущен и включен в автозагрузку")
    
    # Установка certbot
    print("\n📦 Установка certbot...")
    stdout, stderr, code = run_command("which certbot", check=False)
    if code != 0:
        if not wait_for_dpkg_lock():
            input("\nНажмите Enter для продолжения...")
            return
        
        stdout, stderr, code = run_command("apt-get update", check=False)
        if not wait_for_dpkg_lock():
            input("\nНажмите Enter для продолжения...")
            return
        
        stdout, stderr, code = run_command("apt-get install -y certbot python3-certbot-nginx", check=False)
        if code != 0:
            print(f"❌ Ошибка при установке certbot: {stderr}")
            input("\nНажмите Enter для продолжения...")
            return
        print("✅ certbot установлен")
    else:
        print("✅ certbot уже установлен")
    
    # Получение SSL сертификата
    print(f"\n🔐 Получение SSL сертификата для {domain}...")
    print("💡 Убедитесь, что:")
    print("   1. Домен указывает на IP этого сервера")
    print("   2. Порт 80 открыт в файрволе")
    print("   3. Nginx доступен извне на порту 80")
    
    # Предварительные проверки
    print("\n🔍 Выполняю предварительные проверки...")
    
    # Проверка DNS резолвинга на сервере (критично для certbot)
    print("🔍 Проверка DNS резолвинга на сервере...")
    stdout, stderr, code = run_command("nslookup acme-v02.api.letsencrypt.org 2>&1 || dig acme-v02.api.letsencrypt.org +short 2>&1 || echo 'DNS не работает'", check=False)
    if 'DNS не работает' in stdout or code != 0:
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: DNS резолвинг не работает на сервере!")
        print("💡 Certbot не сможет подключиться к Let's Encrypt API")
        print("\n💡 Решение:")
        print("   1. Проверьте настройки DNS в /etc/resolv.conf")
        print("   2. Убедитесь, что сервер имеет доступ в интернет")
        print("   3. Попробуйте установить DNS серверы вручную:")
        print("      echo 'nameserver 8.8.8.8' >> /etc/resolv.conf")
        print("      echo 'nameserver 1.1.1.1' >> /etc/resolv.conf")
        response = input("\nПродолжить все равно? (y/N): ").strip().lower()
        if response != 'y':
            print("❌ Установка отменена")
            input("\nНажмите Enter для продолжения...")
            return
    else:
        print("✅ DNS резолвинг работает")
    
    # Проверка доступности Let's Encrypt API
    print("🔍 Проверка доступности Let's Encrypt API...")
    stdout, stderr, code = run_command("curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://acme-v02.api.letsencrypt.org/directory || echo '000'", check=False)
    if stdout and stdout.strip() == '200':
        print("✅ Let's Encrypt API доступен")
    elif stdout and stdout.strip() == '000':
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: Let's Encrypt API недоступен!")
        print("💡 Возможные причины:")
        print("   1. Нет интернет-соединения")
        print("   2. DNS не работает")
        print("   3. Порт 443 заблокирован исходящим файрволом")
        print("   4. Провайдер блокирует доступ к Let's Encrypt")
        response = input("\nПродолжить все равно? (y/N): ").strip().lower()
        if response != 'y':
            print("❌ Установка отменена")
            input("\nНажмите Enter для продолжения...")
            return
    else:
        print(f"⚠️  Не удалось проверить доступность API (код: {stdout.strip() if stdout else 'N/A'})")
    
    # Проверка DNS записи домена
    print(f"\n🔍 Проверка DNS записи для {domain}...")
    import socket
    try:
        # Получаем IPv4 адрес домена
        domain_ipv4 = socket.gethostbyname(domain)
        print(f"✅ DNS резолвится (IPv4): {domain} -> {domain_ipv4}")
        
        # Получаем IPv4 и IPv6 адреса сервера
        stdout_ipv4, stderr_ipv4, code_ipv4 = run_command("curl -4 -s ifconfig.me 2>/dev/null || curl -4 -s icanhazip.com 2>/dev/null || echo ''", check=False)
        server_ipv4 = stdout_ipv4.strip() if stdout_ipv4 else ""
        
        stdout_ipv6, stderr_ipv6, code_ipv6 = run_command("curl -6 -s ifconfig.me 2>/dev/null || curl -6 -s icanhazip.com 2>/dev/null || echo ''", check=False)
        server_ipv6 = stdout_ipv6.strip() if stdout_ipv6 else ""
        
        # Проверяем соответствие IPv4
        ip_match = False
        if server_ipv4:
            print(f"🌐 IPv4 этого сервера: {server_ipv4}")
            if domain_ipv4 == server_ipv4:
                print("✅ DNS (IPv4) указывает на этот сервер")
                ip_match = True
            else:
                print(f"⚠️  DNS (IPv4) указывает на {domain_ipv4}, а сервер имеет IPv4 {server_ipv4}")
        else:
            print("⚠️  Не удалось определить IPv4 сервера")
        
        # Проверяем IPv6 (если есть)
        if server_ipv6:
            print(f"🌐 IPv6 этого сервера: {server_ipv6}")
            # Пытаемся получить IPv6 домена (может не быть)
            try:
                domain_ipv6 = socket.getaddrinfo(domain, None, socket.AF_INET6)[0][4][0]
                print(f"✅ DNS резолвится (IPv6): {domain} -> {domain_ipv6}")
                if domain_ipv6 == server_ipv6:
                    print("✅ DNS (IPv6) указывает на этот сервер")
                    ip_match = True
            except (socket.gaierror, IndexError):
                print("ℹ️  Домен не имеет IPv6 записи (это нормально)")
        
        # Если ни IPv4, ни IPv6 не совпадают, предупреждаем
        if not ip_match and server_ipv4:
            print("\n⚠️  ВНИМАНИЕ: DNS домена не указывает на IP этого сервера!")
            print("💡 Домен должен указывать на IP этого сервера для получения сертификата")
            print("💡 Проверьте A-запись в DNS настройках домена")
            response = input("\nПродолжить все равно? (y/N): ").strip().lower()
            if response != 'y':
                print("❌ Установка отменена")
                input("\nНажмите Enter для продолжения...")
                return
        elif not server_ipv4 and not server_ipv6:
            print("⚠️  Не удалось определить IP адреса сервера, пропускаю проверку DNS")
    except socket.gaierror:
        print(f"❌ Ошибка: домен {domain} не резолвится")
        print("💡 Проверьте правильность написания домена и настройки DNS")
        input("\nНажмите Enter для продолжения...")
        return
    except Exception as e:
        print(f"⚠️  Ошибка при проверке DNS: {e}")
    
    # Проверка доступности порта 80
    print("🔍 Проверка доступности порта 80...")
    stdout, stderr, code = run_command("netstat -tuln | grep ':80 ' || ss -tuln | grep ':80 '", check=False)
    if code == 0 and stdout:
        print(f"✅ Порт 80 прослушивается")
    else:
        print("⚠️  Порт 80 не найден в списке прослушиваемых портов")
    
    # Проверка статуса nginx
    stdout, stderr, code = run_command("systemctl is-active nginx", check=False)
    if code == 0:
        print("✅ Nginx активен")
    else:
        print("⚠️  Nginx не активен")
    
    # Проверка файрвола
    stdout, stderr, code = run_command("ufw status | grep -E '80|443' || iptables -L -n | grep -E ':80|:443' || echo 'Файрвол не проверен'", check=False)
    if stdout and '80' in stdout:
        print(f"📋 Статус файрвола для портов 80/443: {stdout[:100]}")
    
    print("\n🚀 Запускаю certbot в standalone режиме...")
    print("💡 Standalone режим более надежен - certbot временно остановит nginx и сам поднимет веб-сервер для верификации")
    
    # Останавливаем nginx для standalone режима
    print("\n⏸️  Временно останавливаю nginx...")
    stdout, stderr, code = run_command("systemctl stop nginx", check=False)
    if code != 0:
        print(f"⚠️  Предупреждение при остановке nginx: {stderr}")
    
    # Получаем сертификат в standalone режиме (как в install_bot.sh)
    if use_no_email_flag:
        certbot_cmd = f'certbot certonly --standalone -d "{domain}" --register-unsafely-without-email --agree-tos --non-interactive'
    else:
        certbot_cmd = f'certbot certonly --standalone -d "{domain}" --email "{email}" --agree-tos --non-interactive'
    
    stdout, stderr, code = run_command(certbot_cmd, check=False)
    cert_obtained = code == 0
    
    # Запускаем nginx обратно
    print("\n▶️  Запускаю nginx обратно...")
    stdout, stderr, code = run_command("systemctl start nginx", check=False)
    if code != 0:
        print(f"⚠️  Ошибка при запуске nginx: {stderr}")
    else:
        print("✅ Nginx запущен")
    
    # Выводим детальную информацию об ошибке
    if not cert_obtained:
        print("\n❌ Certbot вернул ошибку:")
        print("─" * 60)
        output_text = (stdout or "") + (stderr or "")
        
        if stdout:
            print("STDOUT:")
            print(stdout[-1000:] if len(stdout) > 1000 else stdout)  # Ограничиваем вывод
        if stderr:
            print("\nSTDERR:")
            print(stderr[-1000:] if len(stderr) > 1000 else stderr)  # Ограничиваем вывод
        print("─" * 60)
        
        # Проверяем логи certbot для более детальной информации
        print("\n🔍 Проверка логов certbot...")
        stdout_logs, stderr_logs, code_logs = run_command("tail -n 100 /var/log/letsencrypt/letsencrypt.log 2>/dev/null || echo 'Логи не найдены'", check=False)
        if stdout_logs and 'Логи не найдены' not in stdout_logs:
            print("Последние строки из логов certbot:")
            # Показываем только последние строки с ошибками
            lines = stdout_logs.split('\n')
            error_lines = [line for line in lines if any(keyword in line.upper() for keyword in ['ERROR', 'FAILED', 'CHALLENGE', 'AUTHORIZATION', 'EXCEPTION'])]
            if error_lines:
                print("\n".join(error_lines[-20:]))  # Последние 20 строк с ошибками
            else:
                print(stdout_logs[-500:] if len(stdout_logs) > 500 else stdout_logs)
        
        # Дополнительная диагностика
        print("\n💡 Диагностика проблемы:")
        print("─" * 60)
        
        # Проверяем, есть ли rate limit
        if 'too many' in output_text.lower() or 'rate limit' in output_text.lower() or '429' in output_text:
            print("⚠️  Превышен лимит запросов Let's Encrypt")
            print("💡 Let's Encrypt ограничивает количество запросов сертификатов (5 в неделю на домен)")
            print("💡 Подождите несколько часов или используйте staging окружение для тестирования:")
            print(f"   certbot certonly --standalone -d {domain} --staging --register-unsafely-without-email --agree-tos --non-interactive")
        
        # Проверяем проблемы с DNS
        elif 'dns' in output_text.lower() or 'no a record' in output_text.lower() or 'nxdomain' in output_text.lower():
            print("⚠️  Проблема с DNS")
            print(f"💡 Убедитесь, что домен {domain} указывает на IP этого сервера")
            print(f"💡 Проверьте DNS: dig {domain} или nslookup {domain}")
            print("💡 DNS изменения могут распространяться до 24 часов")
        
        # Проверяем проблемы с доступностью порта 80
        elif 'connection refused' in output_text.lower() or 'timeout' in output_text.lower() or 'connection' in output_text.lower():
            print("⚠️  Проблема с доступностью порта 80")
            print("💡 Убедитесь, что:")
            print("   1. Порт 80 открыт в файрволе: ufw allow 80/tcp")
            print("   2. Nginx может слушать порт 80 (standalone режим сам поднимает сервер)")
            print(f"   3. Домен доступен извне: curl -I http://{domain}")
            print("   4. Файрвол провайдера не блокирует порт 80")
        
        # Проверяем проблемы с авторизацией
        elif 'authorization' in output_text.lower() or 'challenge' in output_text.lower():
            print("⚠️  Проблема с прохождением challenge (проверки домена)")
            print("💡 Возможные причины:")
            print("   1. Домен недоступен из интернета на порту 80")
            print("   2. Файрвол блокирует входящие соединения на порт 80")
            print("   3. DNS не указывает на этот сервер")
            print("   4. Провайдер блокирует порт 80")
            print("\n💡 Попробуйте:")
            print(f"   - Проверить доступность: curl http://{domain}")
            print(f"   - Проверить DNS: dig {domain} +short")
            print("   - Открыть порт 80: ufw allow 80/tcp")
        
        else:
            print("⚠️  Общие рекомендации:")
            print("   1. Убедитесь, что домен указывает на IP этого сервера")
            print("   2. Проверьте, что порт 80 открыт: ufw allow 80/tcp")
            print(f"   3. Проверьте доступность домена: curl http://{domain}")
            print("   4. Подождите несколько минут и попробуйте снова")
            print("   5. Используйте staging для тестирования: certbot --staging ...")
        
        print("─" * 60)
    
    # Проверяем наличие сертификата (даже если certbot вернул ошибку, сертификат мог быть получен ранее)
    cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
    cert_exists = os.path.exists(cert_path)
    
    if cert_obtained or cert_exists:
        if cert_exists:
            print("✅ SSL сертификат найден!")
        else:
            print("✅ SSL сертификат успешно получен!")
        
        # В standalone режиме certbot не обновляет конфигурацию nginx автоматически
        # Нужно создать конфигурацию вручную (как в install_bot.sh)
        print("\n📝 Настраиваю nginx для использования SSL сертификата...")
        
        # Создаем конфигурацию с HTTPS
        https_config = f"""# Конфигурация nginx для subpage
# Домен: {domain}

# HTTP сервер (редирект на HTTPS)
server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    return 301 https://$server_name$request_uri;
}}

# HTTPS сервер
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};

    # SSL сертификаты
    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    ssl_trusted_certificate /etc/letsencrypt/live/{domain}/chain.pem;

    # SSL настройки
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # Логирование
    access_log /var/log/nginx/subpage-access.log;
    error_log /var/log/nginx/subpage-error.log;

    # Таймауты на уровне сервера для стабильности соединений с приложениями
    client_body_timeout 30s;
    client_header_timeout 30s;
    keepalive_timeout 60s;
    send_timeout 30s;
    
    # Ограничение размера запросов
    client_max_body_size 10k;
    client_body_buffer_size 1k;

    # Проксирование на subpage
    location / {{
        proxy_pass http://127.0.0.1:{SUBPAGE_PORT};
        
        # Заголовки
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Таймауты прокси
        proxy_connect_timeout 30s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
    }}
}}
"""
        
        try:
            with open(nginx_conf, 'w') as f:
                f.write(https_config)
            print("✅ Конфигурация nginx с HTTPS создана")
        except Exception as e:
            print(f"❌ Ошибка при создании конфигурации: {e}")
            input("\nНажмите Enter для продолжения...")
            return
        
        # Проверка конфигурации
        print("\n🔍 Проверка конфигурации nginx...")
        stdout, stderr, code = run_command("nginx -t", check=False)
        if code == 0:
            stdout, stderr, code = run_command("systemctl reload nginx", check=False)
            if code == 0:
                print("✅ Nginx перезагружен с HTTPS конфигурацией")
            else:
                print(f"⚠️  Ошибка при перезагрузке nginx: {stderr}")
        else:
            print(f"❌ Ошибка в конфигурации nginx: {stderr}")
            print("💡 Исправляю конфигурацию...")
            fix_nginx_config()
            return
    else:
        print("⚠️  Не удалось получить SSL сертификат")
        print("💡 Возможные причины:")
        print("   1. Домен не указывает на этот сервер")
        print("   2. Порт 80 заблокирован файрволом")
        print("   3. Nginx не доступен извне")
        print(f"\n💡 Попробуйте получить сертификат вручную:")
        print(f"   certbot --nginx -d {domain} --email {email} --agree-tos")
        print("\n📝 Создаю конфигурацию с HTTPS (сертификат будет добавлен позже)...")
        
        # Создаем конфигурацию с HTTPS блоком, но без активных сертификатов
        # Когда сертификат будет получен, его можно будет просто раскомментировать
        https_config_without_cert = f"""# Конфигурация nginx для subpage
# Домен: {domain}
# ВАЖНО: SSL сертификат еще не получен
# После получения сертификата раскомментируйте строки с ssl_certificate ниже
# Или используйте пункт 'Принудительно обновить SSL сертификаты' в меню Nginx

# HTTP сервер (пока без редиректа, так как HTTPS еще не настроен)
server {{
    listen 80;
    listen [::]:80;
    server_name {domain};

    # Логирование
    access_log /var/log/nginx/subpage-access.log;
    error_log /var/log/nginx/subpage-error.log;

    # Таймауты на уровне сервера для стабильности соединений с приложениями
    client_body_timeout 30s;
    client_header_timeout 30s;
    keepalive_timeout 60s;
    send_timeout 30s;
    
    # Ограничение размера запросов
    client_max_body_size 10k;
    client_body_buffer_size 1k;

    # Проксирование на subpage
    location / {{
        proxy_pass http://127.0.0.1:{SUBPAGE_PORT};
        
        # Заголовки
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Таймауты прокси
        proxy_connect_timeout 30s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
    }}
}}

# HTTPS сервер (закомментирован до получения сертификата)
# Раскомментируйте этот блок после получения SSL сертификата
# И раскомментируйте редирект в HTTP блоке выше (строка return 301)
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};

    # SSL сертификаты (раскомментируйте после получения сертификата)
    # ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    # ssl_trusted_certificate /etc/letsencrypt/live/{domain}/chain.pem;

    # SSL настройки
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # Логирование
    access_log /var/log/nginx/subpage-access.log;
    error_log /var/log/nginx/subpage-error.log;

    # Таймауты на уровне сервера для стабильности соединений с приложениями
    client_body_timeout 30s;
    client_header_timeout 30s;
    keepalive_timeout 60s;
    send_timeout 30s;
    
    # Ограничение размера запросов
    client_max_body_size 10k;
    client_body_buffer_size 1k;

    # Проксирование на subpage
    location / {{
        proxy_pass http://127.0.0.1:{SUBPAGE_PORT};
        
        # Заголовки
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Таймауты прокси
        proxy_connect_timeout 30s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
    }}
}}
"""
        
        try:
            with open(nginx_conf, 'w') as f:
                f.write(https_config_without_cert)
            print("✅ Конфигурация создана с HTTPS блоком (сертификат нужно получить позже)")
            print("💡 После получения сертификата используйте пункт 'Принудительно обновить SSL сертификаты'")
        except Exception as e:
            print(f"❌ Ошибка при создании конфигурации: {e}")
            input("\nНажмите Enter для продолжения...")
            return
        
        # Проверка и перезагрузка
        stdout, stderr, code = run_command("nginx -t", check=False)
        if code == 0:
            stdout, stderr, code = run_command("systemctl reload nginx", check=False)
            if code == 0:
                print("✅ nginx перезагружен с HTTP конфигурацией")
            else:
                print(f"⚠️  Ошибка при перезагрузке nginx: {stderr}")
        else:
            print(f"❌ Ошибка в конфигурации nginx: {stderr}")
            input("\nНажмите Enter для продолжения...")
            return
    
    print("\n" + "=" * 60)
    print("✅ Установка Nginx завершена!")
    print("=" * 60)
    print(f"\n📋 Домен: {domain}")
    
    # Проверяем финальный статус сертификата
    cert_path_final = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
    if os.path.exists(cert_path_final):
        print(f"🌐 HTTPS: https://{domain} ✅")
        print(f"🔗 HTTP: http://{domain} (редирект на HTTPS)")
    else:
        print(f"🌐 HTTP: http://{domain} ⚠️")
        print("⚠️  SSL сертификат не настроен")
        print("💡 Для получения сертификата используйте:")
        print(f"   certbot --nginx -d {domain} --email {email} --agree-tos")
        print("   Или пункт 'Принудительно обновить SSL сертификаты' в меню Nginx")
    
    print(f"🔗 Проксирование на: http://127.0.0.1:{SUBPAGE_PORT}")
    print(f"\n💡 Полезные команды:")
    print("   Проверка конфигурации: nginx -t")
    print("   Перезагрузка: systemctl reload nginx")
    print("   Статус: systemctl status nginx")
    print("   Логи: tail -f /var/log/nginx/subpage-error.log")
    
    input("\nНажмите Enter для продолжения...")


def fix_nginx_config():
    """Исправляет конфигурацию Nginx, если сертификат отсутствует"""
    print("\n🔧 Исправление конфигурации Nginx...")
    print("─" * 60)
    
    # Проверка прав root
    if os.geteuid() != 0:
        print("❌ Для исправления конфигурации требуются права root")
        print("💡 Запустите CLI с sudo: sudo subpage")
        input("\nНажмите Enter для продолжения...")
        return
    
    nginx_conf = "/etc/nginx/sites-available/subpage"
    
    if not os.path.exists(nginx_conf):
        print(f"❌ Файл конфигурации не найден: {nginx_conf}")
        print("💡 Сначала установите Nginx через пункт 'Установить и настроить Nginx с SSL'")
        input("\nНажмите Enter для продолжения...")
        return
    
    # Читаем текущую конфигурацию
    try:
        with open(nginx_conf, 'r') as f:
            current_config = f.read()
    except Exception as e:
        print(f"❌ Ошибка при чтении конфигурации: {e}")
        input("\nНажмите Enter для продолжения...")
        return
    
    # Ищем домен в конфигурации
    import re
    domain_match = re.search(r'server_name\s+([^;]+);', current_config)
    if not domain_match:
        print("❌ Не удалось определить домен из конфигурации")
        input("\nНажмите Enter для продолжения...")
        return
    
    domain = domain_match.group(1).strip()
    print(f"📋 Найден домен: {domain}")
    
    # Проверяем наличие сертификата
    cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
    cert_exists = os.path.exists(cert_path)
    
    if cert_exists:
        print("✅ SSL сертификат найден, конфигурация должна быть корректной")
        print("💡 Если есть ошибки, проверьте конфигурацию вручную")
        input("\nНажмите Enter для продолжения...")
        return
    
    print("⚠️  SSL сертификат не найден, создаю конфигурацию с HTTPS блоком (сертификат нужно получить)...")
    
    # Создаем конфигурацию с HTTPS блоком, но без активных сертификатов
    https_config_without_cert = f"""# Конфигурация nginx для subpage
# Домен: {domain}
# ВАЖНО: SSL сертификат еще не получен
# После получения сертификата раскомментируйте строки с ssl_certificate ниже

# HTTP сервер (пока без редиректа, так как HTTPS еще не настроен)
server {{
    listen 80;
    listen [::]:80;
    server_name {domain};

    # Логирование
    access_log /var/log/nginx/subpage-access.log;
    error_log /var/log/nginx/subpage-error.log;

    # Таймауты на уровне сервера для стабильности соединений с приложениями
    client_body_timeout 30s;
    client_header_timeout 30s;
    keepalive_timeout 60s;
    send_timeout 30s;
    
    # Ограничение размера запросов
    client_max_body_size 10k;
    client_body_buffer_size 1k;

    # Проксирование на subpage
    location / {{
        proxy_pass http://127.0.0.1:{SUBPAGE_PORT};
        
        # Заголовки
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Таймауты прокси
        proxy_connect_timeout 30s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
    }}
}}

# HTTPS сервер (закомментирован до получения сертификата)
# Раскомментируйте этот блок после получения SSL сертификата
# И раскомментируйте редирект в HTTP блоке выше (строка return 301)
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};

    # SSL сертификаты (раскомментируйте после получения сертификата)
    # ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    # ssl_trusted_certificate /etc/letsencrypt/live/{domain}/chain.pem;

    # SSL настройки
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # Логирование
    access_log /var/log/nginx/subpage-access.log;
    error_log /var/log/nginx/subpage-error.log;

    # Таймауты на уровне сервера для стабильности соединений с приложениями
    client_body_timeout 30s;
    client_header_timeout 30s;
    keepalive_timeout 60s;
    send_timeout 30s;
    
    # Ограничение размера запросов
    client_max_body_size 10k;
    client_body_buffer_size 1k;

    # Проксирование на subpage
    location / {{
        proxy_pass http://127.0.0.1:{SUBPAGE_PORT};
        
        # Заголовки
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Таймауты прокси
        proxy_connect_timeout 30s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
    }}
}}
"""
    
    try:
        with open(nginx_conf, 'w') as f:
            f.write(https_config_without_cert)
        print("✅ Конфигурация обновлена с HTTPS блоком (сертификат нужно получить)")
    except Exception as e:
        print(f"❌ Ошибка при обновлении конфигурации: {e}")
        input("\nНажмите Enter для продолжения...")
        return
    
    # Проверка конфигурации
    stdout, stderr, code = run_command("nginx -t", check=False)
    if code == 0:
        stdout, stderr, code = run_command("systemctl reload nginx", check=False)
        if code == 0:
            print("✅ nginx перезагружен с исправленной конфигурацией")
            print(f"🌐 Доступен по адресу: http://{domain}")
        else:
            print(f"⚠️  Ошибка при перезагрузке nginx: {stderr}")
    else:
        print(f"❌ Ошибка в конфигурации nginx: {stderr}")
    
    input("\nНажмите Enter для продолжения...")


def remove_nginx():
    """Удаляет конфигурацию Nginx для subpage"""
    print("\n🗑️  Удаление конфигурации Nginx для subpage...")
    print("─" * 60)
    
    # Проверка прав root
    if os.geteuid() != 0:
        print("❌ Для удаления конфигурации требуются права root")
        print("💡 Запустите CLI с sudo: sudo subpage")
        input("\nНажмите Enter для продолжения...")
        return
    
    nginx_conf = "/etc/nginx/sites-available/subpage"
    nginx_enabled = "/etc/nginx/sites-enabled/subpage"
    
    print("\n⚠️  Это удалит конфигурацию subpage из Nginx")
    print("💡 Nginx и certbot останутся установленными")
    print("💡 Введите 'yes' для подтверждения: ", end='')
    try:
        response = input().strip().lower()
        if response != 'yes':
            print("❌ Отменено")
            input("\nНажмите Enter для продолжения...")
            return
    except (EOFError, KeyboardInterrupt):
        print("\n\nОтмена...")
        return
    
    # Удаляем конфигурацию
    removed = False
    
    if os.path.exists(nginx_enabled):
        try:
            os.remove(nginx_enabled)
            print(f"✅ Удален симлинк: {nginx_enabled}")
            removed = True
        except Exception as e:
            print(f"⚠️  Ошибка при удалении симлинка: {e}")
    
    if os.path.exists(nginx_conf):
        try:
            os.remove(nginx_conf)
            print(f"✅ Удален файл конфигурации: {nginx_conf}")
            removed = True
        except Exception as e:
            print(f"⚠️  Ошибка при удалении конфигурации: {e}")
    
    if removed:
        # Проверка конфигурации
        stdout, stderr, code = run_command("nginx -t", check=False)
        if code == 0:
            stdout, stderr, code = run_command("systemctl reload nginx", check=False)
            if code == 0:
                print("✅ Nginx перезагружен")
            else:
                print(f"⚠️  Ошибка при перезагрузке nginx: {stderr}")
        else:
            print(f"⚠️  Ошибка в конфигурации nginx: {stderr}")
        print("\n✅ Конфигурация subpage удалена")
    else:
        print("\n⚠️  Конфигурация subpage не найдена")
    
    input("\nНажмите Enter для продолжения...")


def renew_certificates():
    """Принудительно обновляет SSL сертификаты"""
    print("\n🔐 Принудительное обновление SSL сертификатов...")
    print("─" * 60)
    
    # Проверка прав root
    if os.geteuid() != 0:
        print("❌ Для обновления сертификатов требуются права root")
        print("💡 Запустите CLI с sudo: sudo subpage")
        input("\nНажмите Enter для продолжения...")
        return
    
    # Проверка наличия certbot
    stdout, stderr, code = run_command("which certbot", check=False)
    if code != 0:
        print("❌ certbot не установлен")
        print("💡 Установите его: apt-get install certbot python3-certbot-nginx")
        input("\nНажмите Enter для продолжения...")
        return
    
    # Показываем список существующих сертификатов
    print("\n📋 Проверка существующих сертификатов...")
    stdout, stderr, code = run_command("certbot certificates", check=False)
    if code == 0 and stdout:
        print("Найденные сертификаты:")
        print(stdout[:500])  # Показываем первые 500 символов
    else:
        print("⚠️  Не удалось получить список сертификатов или сертификаты не найдены")
    
    print("\n⚠️  Принудительное обновление всех сертификатов...")
    print("💡 Это может занять некоторое время")
    print("💡 Certbot автоматически определит метод обновления (standalone или nginx)")
    
    # Проверяем статус nginx перед обновлением
    stdout, stderr, code = run_command("systemctl is-active nginx", check=False)
    nginx_was_active = code == 0
    
    if nginx_was_active:
        print("✅ Nginx активен, certbot может использовать nginx плагин")
    else:
        print("⚠️  Nginx не активен, certbot будет использовать standalone режим")
    
    # Принудительное обновление
    # Certbot автоматически определит метод обновления на основе того, как был получен сертификат
    stdout, stderr, code = run_command("certbot renew --force-renewal", check=False)
    
    if code == 0:
        print("\n✅ Сертификаты успешно обновлены!")
        print("─" * 60)
        if stdout:
            # Показываем только важные строки из вывода
            lines = stdout.split('\n')
            for line in lines:
                if any(keyword in line.lower() for keyword in ['successfully', 'renewed', 'expires', 'certificate']):
                    print(line)
        
        # Перезагрузка nginx
        print("\n🔄 Перезагрузка nginx...")
        if nginx_was_active:
            stdout, stderr, code = run_command("systemctl reload nginx", check=False)
            if code == 0:
                print("✅ nginx перезагружен")
            else:
                print(f"⚠️  Ошибка при перезагрузке nginx: {stderr}")
                print("💡 Попробуйте перезапустить вручную: systemctl restart nginx")
        else:
            print("⚠️  Nginx не был активен, пропускаю перезагрузку")
    else:
        print("\n❌ Ошибка при обновлении сертификатов")
        print("─" * 60)
        if stdout:
            print("STDOUT:")
            print(stdout[-500:] if len(stdout) > 500 else stdout)
        if stderr:
            print("\nSTDERR:")
            print(stderr[-500:] if len(stderr) > 500 else stderr)
        
        print("\n💡 Возможные причины:")
        print("   1. Сертификаты не найдены")
        print("   2. Домен не доступен извне")
        print("   3. Порт 80 заблокирован")
        print("   4. Проблемы с DNS")
        
        print("\n💡 Попробуйте обновить вручную:")
        print("   certbot renew --force-renewal --verbose")
        print("\n💡 Или проверьте логи:")
        print("   tail -n 100 /var/log/letsencrypt/letsencrypt.log")
        print("   journalctl -u certbot.timer -n 50")
    
    input("\nНажмите Enter для продолжения...")


def nginx_menu():
    """Подменю управления Nginx"""
    while True:
        clear_screen()
        print("╔══════════════════════════════════════════════════════════╗")
        print("║                     Управление Nginx                     ║")
        print("╚══════════════════════════════════════════════════════════╝")
        print()
        
        # Проверка наличия nginx
        stdout, stderr, code = run_command("which nginx", check=False)
        if code == 0:
            stdout, _, _ = run_command("nginx -v 2>&1")
            print(f"📊 Статус: nginx установлен ({stdout.strip()})")
        else:
            print("⚠️  nginx не установлен")
        print()
        
        print("Выберите действие:")
        print()
        print("  1) 🌐 Установить и настроить Nginx с SSL")
        print("  2) 🔐 Принудительно обновить SSL сертификаты")
        print("  3) 🔧 Исправить конфигурацию (если сертификат отсутствует)")
        print("  4) 📊 Статус Nginx")
        print("  5) 🔄 Перезагрузить Nginx")
        print("  6) 📋 Проверить конфигурацию Nginx")
        print("  7) 📝 Просмотр логов Nginx (error)")
        print("  8) 📝 Просмотр логов Nginx (access)")
        print("  9) 🗑️  Удалить конфигурацию subpage")
        print(" 10) ⬅️  Назад в главное меню")
        print()
        print("─" * 60)
        
        try:
            choice = input("Введите номер действия (1-10): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        
        if choice == '1':
            install_nginx()
        elif choice == '2':
            renew_certificates()
        elif choice == '3':
            fix_nginx_config()
        elif choice == '4':
            stdout, stderr, code = run_command("systemctl status nginx --no-pager -l", check=False)
            print("\n📊 Статус Nginx:")
            print("─" * 60)
            print(stdout)
            input("\nНажмите Enter для продолжения...")
        elif choice == '5':
            print("\n🔄 Перезагрузка Nginx...")
            stdout, stderr, code = run_command("systemctl reload nginx", check=False)
            if code == 0:
                print("✅ Nginx успешно перезагружен")
            else:
                print(f"❌ Ошибка при перезагрузке: {stderr}")
            input("\nНажмите Enter для продолжения...")
        elif choice == '6':
            print("\n🔍 Проверка конфигурации Nginx...")
            stdout, stderr, code = run_command("nginx -t", check=False)
            if code == 0:
                print("✅ Конфигурация корректна")
                print(stdout)
            else:
                print("❌ Ошибка в конфигурации!")
                print(stderr)
            input("\nНажмите Enter для продолжения...")
        elif choice == '7':
            print("\n📋 Последние 50 строк error логов Nginx:")
            print("─" * 60)
            os.system("tail -n 50 /var/log/nginx/subpage-error.log 2>/dev/null || tail -n 50 /var/log/nginx/error.log")
            input("\nНажмите Enter для продолжения...")
        elif choice == '8':
            print("\n📋 Последние 50 строк access логов Nginx:")
            print("─" * 60)
            os.system("tail -n 50 /var/log/nginx/subpage-access.log 2>/dev/null || tail -n 50 /var/log/nginx/access.log")
            input("\nНажмите Enter для продолжения...")
        elif choice == '9':
            remove_nginx()
        elif choice == '10':
            return
        else:
            print("\n❌ Неверный выбор. Пожалуйста, введите число от 1 до 10.")
            input("\nНажмите Enter для продолжения...")


def firewall_menu():
    """Подменю управления файрволом"""
    while True:
        clear_screen()
        print("╔══════════════════════════════════════════════════════════╗")
        print("║              Управление файрволом (UFW)                  ║")
        print("╚══════════════════════════════════════════════════════════╝")
        print()
        
        if not check_ufw_installed():
            print("⚠️  UFW не установлен")
            print("💡 Установите его: apt-get install ufw")
            print()
        else:
            # Показываем краткий статус
            stdout, _, code = run_command("ufw status", check=False)
            if code == 0:
                status_line = stdout.split('\n')[0] if stdout else "Неизвестно"
                print(f"📊 Статус: {status_line}")
                print()
        
        print("Выберите действие:")
        print()
        print("  1) 📊 Статус файрвола")
        print("  2) 📋 Просмотр правил")
        print("  3) ✅ Включить файрвол")
        print("  4) ❌ Выключить файрвол")
        print("  5) 🌐 Открыть веб-порты (80, 443) для Nginx")
        print("  6) 🔒 Закрыть веб-порты (80, 443)")
        print("  7) 🔑 Открыть порт SSH (22)")
        print("  8) ⬅️  Назад в главное меню")
        print()
        print("─" * 60)
        
        try:
            choice = input("Введите номер действия (1-8): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        
        if choice == '1':
            show_firewall_status()
        elif choice == '2':
            show_firewall_rules()
        elif choice == '3':
            enable_firewall()
        elif choice == '4':
            disable_firewall()
        elif choice == '5':
            allow_web_ports()
        elif choice == '6':
            deny_web_ports()
        elif choice == '7':
            allow_ssh_port()
        elif choice == '8':
            return
        else:
            print("\n❌ Неверный выбор. Пожалуйста, введите число от 1 до 8.")
            input("\nНажмите Enter для продолжения...")


def main():
    """Главная функция с интерактивным меню"""
    while True:
        show_menu()
        choice = get_choice()

        if choice == '1':
            restart_service()
        elif choice == '2':
            view_logs(lines=50)
        elif choice == '3':
            view_logs(lines=100)
        elif choice == '4':
            view_logs(follow=True)
        elif choice == '5':
            show_status()
        elif choice == '6':
            update_subpage()
        elif choice == '7':
            edit_env()
        elif choice == '8':
            firewall_menu()
        elif choice == '9':
            nginx_menu()
        elif choice == '10':
            print("\n👋 До свидания!")
            sys.exit(0)
        else:
            print("\n❌ Неверный выбор. Пожалуйста, введите число от 1 до 10.")
            input("\nНажмите Enter для продолжения...")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 До свидания!")
        sys.exit(0)
