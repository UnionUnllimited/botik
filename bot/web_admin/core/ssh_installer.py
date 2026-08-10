"""
Модуль для установки 3X-UI через SSH с использованием официального скрипта.
После успешной установки создает базовый инбаунд через API.
"""

import asyncio
import json
import re
import os
import uuid
import random
import string
import base64
from pathlib import Path
from typing import Dict, Optional, Tuple
from datetime import datetime

import asyncssh
import httpx
from loguru import logger
from py3xui.async_api.async_api import AsyncApi
from py3xui.inbound import Inbound, Settings, Sniffing, StreamSettings

try:
    from cryptography.hazmat.primitives.asymmetric import x25519
    from cryptography.hazmat.primitives import serialization
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False


def strip_ansi_codes(text: str) -> str:
    """Удаляет ANSI escape-коды из строки"""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def generate_reality_keys() -> Dict[str, str]:
    """
    Генерирует пару ключей для Reality (X25519).
    Возвращает словарь с 'publicKey' и 'privateKey' в формате base64url.
    """
    if CRYPTOGRAPHY_AVAILABLE:
        try:
            # Генерируем приватный ключ X25519
            private_key = x25519.X25519PrivateKey.generate()
            
            # Получаем публичный ключ из приватного
            public_key = private_key.public_key()
            
            # Сериализуем ключи в raw формат (32 байта)
            private_bytes = private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption()
            )
            public_bytes = public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw
            )
            
            # Кодируем в base64url (без padding, с заменой + на - и / на _)
            private_key_b64 = base64.urlsafe_b64encode(private_bytes).decode('utf-8').rstrip('=')
            public_key_b64 = base64.urlsafe_b64encode(public_bytes).decode('utf-8').rstrip('=')
            
            return {
                'privateKey': private_key_b64,
                'publicKey': public_key_b64
            }
        except Exception as e:
            logger.warning(f"Ошибка генерации X25519 ключей через cryptography: {e}, используем fallback")
    
    # Fallback: генерируем случайные строки нужной длины в формате base64url
    # Для Reality нужны ключи длиной примерно 43 символа (32 байта в base64url)
    private_bytes = os.urandom(32)
    public_bytes = os.urandom(32)
    
    private_key_b64 = base64.urlsafe_b64encode(private_bytes).decode('utf-8').rstrip('=')
    public_key_b64 = base64.urlsafe_b64encode(public_bytes).decode('utf-8').rstrip('=')
    
    return {
        'privateKey': private_key_b64,
        'publicKey': public_key_b64
    }


class SSH3XUIInstaller:
    """Установка 3X-UI через SSH с использованием официального скрипта"""

    # Фиксированная версия панели (install.sh из тега репозитория mhsanaei/3x-ui).
    # В expect для spawn строки нужны экранированные \\$ чтобы Tcl не съедал переменную bash $VERSION.
    XUI_PANEL_VERSION = "v2.9.4"
    _XUI_EXPECT_SPAWN_ONE_LINER = (
        rf'spawn bash -c "VERSION={XUI_PANEL_VERSION} && bash <(curl -Ls '
        rf'\"https://raw.githubusercontent.com/mhsanaei/3x-ui/\$VERSION/install.sh\") \$VERSION"'
    )

    @classmethod
    def manual_install_bash_one_liner(cls) -> str:
        """Ручная установка на сервере — та же строка, что и подстановка в SSH expect."""
        v = cls.XUI_PANEL_VERSION
        return (
            f'VERSION={v} && bash <(curl -Ls "https://raw.githubusercontent.com/mhsanaei/3x-ui/$VERSION/install.sh") $VERSION'
        )

    @classmethod
    def recommended_install_script_url(cls) -> str:
        """Прямая ссылка на install.sh зафиксированного тега (GitHub raw)."""
        return f'https://raw.githubusercontent.com/mhsanaei/3x-ui/{cls.XUI_PANEL_VERSION}/install.sh'

    @classmethod
    def recommended_panel_version_display(cls) -> str:
        """Подпись в UI без префикса v («2.9.4»)."""
        raw = (cls.XUI_PANEL_VERSION or '').strip()
        return raw[1:].strip() if raw.lower().startswith('v') else raw
    
    # Типы инбаундов (метаданные)
    INBOUND_TEMPLATES = {
        'vless_reality_tcp': {
            'name': 'VLESS + Reality (TCP)',
            'port': 443,
            'protocol': 'vless'
        },
        'vless_reality_xhttp': {
            'name': 'VLESS + Reality (XHTTP)',
            'port': 443,
            'protocol': 'vless'
        }
    }
    
    def __init__(self):
        """Инициализация установщика"""
        self.install_tasks: Dict[str, Dict] = {}  # Хранилище задач установки
    
    async def _tcp_probe(self, host: str, port: int, timeout: float = 5.0) -> Tuple[bool, str]:
        """
        Быстрая проба TCP-соединения до удалённого host:port.

        Возвращает (ok, message). Используется как pre-check перед SSH:
          • если TCP не открывается за `timeout` секунд — ясно, что проблема в
            сети/файрволе, а не в SSH/паролях. В этом случае SSH ждать 30 сек
            бесполезно — отдаём пользователю осмысленную диагностику сразу.
          • если TCP отвечает, а потом SSH всё равно падает с timeout —
            значит порт открыт, но handshake не идёт (несовместимость, idle drop,
            или sshd просто молчит).
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout,
            )
            try:
                # Достаточно одной строки — SSH сервер отдаёт banner вида
                # "SSH-2.0-OpenSSH_8.9" в первые ~100 мс. Если пришёл — точно SSH.
                # Если не пришёл за 2с — порт открыт, но сервис не SSH или висит.
                try:
                    banner = await asyncio.wait_for(reader.readline(), timeout=2.0)
                    banner_str = (banner or b'').decode('latin-1', errors='ignore').strip()
                except asyncio.TimeoutError:
                    banner_str = ''
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            if banner_str.startswith('SSH-'):
                return True, f'TCP OK · banner: {banner_str}'
            elif banner_str:
                return False, (
                    f'Порт открыт, но это не SSH-сервис (banner: {banner_str!r}). '
                    f'Возможно, на этом порту висит другая служба.'
                )
            else:
                return True, 'TCP OK (banner не получен — продолжаем SSH-handshake)'
        except asyncio.TimeoutError:
            return False, (
                f'TCP {host}:{port} не отвечает за {timeout:.0f} сек. '
                f'Проверьте: '
                f'\n  • открыт ли исходящий SSH из админки (некоторые VPS-провайдеры блокируют '
                f'исходящие на :22 для борьбы с brute-force);'
                f'\n  • верный ли порт SSH (часто меняют на 2222/22022/…);'
                f'\n  • не закрыт ли :{port} файрволом на удалённом сервере (UFW/iptables/cloud firewall).'
            )
        except OSError as e:
            return False, (
                f'Сетевая ошибка ({type(e).__name__}): {e}. '
                f'Хост недостижим / DNS не резолвится / connection refused.'
            )

    async def _fetch_subscription_template(
        self,
        server_ip: str,
        sub_id: str,
        client_uuid: str,
        sub_port: int = 2096,
        sub_path: str = 'sub',
        timeout: float = 10.0,
    ) -> Optional[str]:
        """
        Получить vless:// строку из subscription-сервиса 3X-UI и превратить её в
        key_template (с плейсхолдерами [UUID] и [Severname]).

        Алгоритм:
          1. GET https://{server_ip}:{sub_port}/{sub_path}/{sub_id} (verify=False —
             у subscription стоит самоподписной сертификат, как у самой панели).
          2. Тело — base64 от списка строк ``vless://...`` через `\\n`.
          3. Декодируем, ищем строку с нашим client_uuid (берём именно её, на случай
             если в подписке оказался не один inbound). Fallback — первая vless://.
          4. Заменяем `client_uuid` → `[UUID]`. Часть после последнего `#` (имя
             ключа в клиенте) → `[Severname]` — этот плейсхолдер уже понимает
             подписочный движок бота (см. button_helpers/keyboards).
          5. Возвращаем готовую строку или None — наверху она опционально пойдёт
             в server.key_template + server.use_key_template = True.

        Любая ошибка (404, base64 битый, нет vless:// в выдаче, таймаут) → None,
        чтобы установка не рухнула; админ всё равно получит сервер с заполненными
        legacy-полями (public_host/port/sub_path_prefix).
        """
        sub_path = (sub_path or '').strip().strip('/')
        url = f'https://{server_ip}:{sub_port}/{sub_path}/{sub_id}'
        try:
            # subscription может встать чуть позже create_inbound, дадим 1.5 сек
            await asyncio.sleep(1.5)
            async with httpx.AsyncClient(verify=False, timeout=timeout, follow_redirects=True) as http:
                r = await http.get(url)
            if r.status_code != 200:
                logger.warning(f'[sub-template] HTTP {r.status_code} for {url}')
                return None
            body = (r.text or '').strip()
            if not body:
                logger.warning(f'[sub-template] empty body from {url}')
                return None

            # Содержимое подписки — base64 от строк "vless://..."
            try:
                decoded = base64.b64decode(body, validate=False).decode('utf-8', errors='ignore')
            except Exception as e:
                logger.warning(f'[sub-template] base64 decode failed: {e}; first 80 chars: {body[:80]!r}')
                return None

            # Сначала ищем строку именно с нашим UUID
            line = ''
            for raw in decoded.splitlines():
                raw = raw.strip()
                if raw.startswith('vless://') and client_uuid in raw:
                    line = raw
                    break
            # Fallback: первая vless:// строка (на случай, если 3X-UI слегка
            # переформатировал uuid внутри URL — мы возьмём что есть).
            if not line:
                for raw in decoded.splitlines():
                    raw = raw.strip()
                    if raw.startswith('vless://'):
                        line = raw
                        break
            if not line:
                logger.warning(
                    f'[sub-template] no vless:// in decoded subscription. '
                    f'first 200 chars: {decoded[:200]!r}'
                )
                return None

            # Заменяем UUID → [UUID].
            line_with_uuid_ph = line.replace(client_uuid, '[UUID]')

            # Заменяем имя после последнего `#` → [Severname].
            hash_idx = line_with_uuid_ph.rfind('#')
            if hash_idx >= 0:
                template = line_with_uuid_ph[:hash_idx] + '#[Severname]'
            else:
                template = line_with_uuid_ph + '#[Severname]'

            logger.info(f'[sub-template] generated for {server_ip}: {template[:100]}…')
            return template
        except (httpx.TimeoutException, asyncio.TimeoutError):
            logger.warning(f'[sub-template] timeout fetching {url}')
            return None
        except httpx.RequestError as e:
            logger.warning(f'[sub-template] network error for {url}: {type(e).__name__}: {e}')
            return None
        except Exception as e:
            logger.warning(f'[sub-template] unexpected error: {type(e).__name__}: {e}')
            return None

    async def _create_ssh_connection(
        self, host: str, port: int, username: str, password: str, timeout: int = 30
    ) -> asyncssh.SSHClientConnection:
        """
        Создание асинхронного SSH соединения.

        Параметры подобраны под реальные проблемы:

        * **client_keys=()**  — отключаем publickey-auth. Иначе asyncssh
          сначала тащит ключи пользователя процесса (под `vpn-webadmin.service`
          обычно root), пробует их по очереди, и при несовместимости долго
          висит на KEX → пользователь видит «таймаут» при правильном пароле.

        * **preferred_auth='password,keyboard-interactive'** — пробуем оба
          парольных метода. На многих современных дистрибутивах SSHD выставлен
          `KbdInteractiveAuthentication yes` вместо/вместе с `PasswordAuthentication`,
          и одного `password` ему мало.

        * **timeout=30** — TCP handshake + KEX + auth на медленных или географически
          далёких серверах легко идёт 6–15 секунд. 30 с — комфортный запас.

        * **keepalive_interval/count** — чтобы за 10–20 минут установки сессия
          не отвалилась по idle (некоторые провайдеры/файрволы режут TCP без активности).
        """
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    host,
                    port=port,
                    username=username,
                    password=password,
                    known_hosts=None,                              # не проверяем host key
                    client_keys=(),                                # отключаем publickey-auth
                    preferred_auth='password,keyboard-interactive',
                    keepalive_interval=60,                         # каждые 60с пинг
                    keepalive_count_max=10,                        # 10 неответов до разрыва
                ),
                timeout=timeout
            )
            return conn
        except Exception as e:
            logger.error(
                f"Ошибка SSH подключения к {host}:{port} (user={username!r}): "
                f"{type(e).__name__}: {e}"
            )
            raise

    async def check_ssh_connection(self, host: str, port: int, username: str, password: str) -> Tuple[bool, str]:
        """Проверка SSH подключения. timeout=15 — реалистичный для login banner+auth."""
        try:
            conn = await self._create_ssh_connection(host, port, username, password, timeout=15)
            conn.close()
            return True, "SSH подключение успешно"
        except asyncssh.PermissionDenied:
            return False, "Неверные учетные данные SSH"
        except asyncssh.Error as e:
            return False, f"Ошибка SSH: {str(e)}"
        except asyncio.TimeoutError:
            return False, "Таймаут SSH подключения (>15 сек). Проверьте порт, фаервол и доступность сервера."
        except OSError as e:
            return False, f"Сетевая ошибка: {e}"
        except Exception as e:
            return False, f"Ошибка подключения: {str(e)}"
    
    async def check_os(self, conn: asyncssh.SSHClientConnection) -> Tuple[bool, str]:
        """Проверка ОС (только Ubuntu/Debian)"""
        try:
            result = await conn.run('cat /etc/os-release', check=False)
            output = result.stdout or ''

            if 'ubuntu' in output.lower() or 'debian' in output.lower():
                os_match = re.search(r'PRETTY_NAME="([^"]+)"', output)
                os_name = os_match.group(1) if os_match else "Ubuntu/Debian"
                return True, os_name
            else:
                return False, "Поддерживаются только Ubuntu и Debian"
        except Exception as e:
            logger.error(f"Ошибка проверки ОС: {e}")
            return False, f"Ошибка проверки ОС: {str(e)}"
    
    async def execute_install_script(
        self,
        conn: asyncssh.SSHClientConnection,
        certificate_type: str = 'ip',
        ssl_domain: Optional[str] = None,
        cert_port: int = 80
    ) -> Tuple[bool, str, str]:
        """Выполнение официального скрипта установки 3X-UI с автоматическим выбором типа сертификата"""
        # Используем expect для автоматической передачи ответов
        # Официальный скрипт спрашивает (обновлено для новой версии):
        # Опционально (только при первой установке с дефолтными учетными данными):
        #   - "Would you like to customize the Panel Port settings? (If not, a random port will be applied) [y/n]:" - отвечаем "n"
        # Обязательно:
        #   - "Choose an option (default 2 for IP):" - отвечаем "1" (Domain) или "2" (IP certificate)
        # Для Domain:
        #   - "Please enter your domain name:" - вводим домен
        #   - "Please choose which port to use (default is 80):" - вводим порт
        #   - "Would you like to modify --reloadcmd for ACME? (y/n):" - отвечаем "n"
        #   - "Would you like to set this certificate for the panel? (y/n):" - отвечаем "y"
        # Для IP (обновлено):
        #   - "Port to use for ACME HTTP-01 listener (default 80):" - вводим порт (по умолчанию 80)
        #   - Опционально: "Enter another port for acme.sh standalone listener (leave empty to abort):" - если порт занят
        #   - "Do you have an IPv6 address to include? (leave empty to skip):" - отвечаем пустую строку
        
        if certificate_type == 'domain' and ssl_domain:
            # Expect скрипт для доменного сертификата
            # Обновлен для работы с новой версией официального скрипта
            install_script = f'''#!/usr/bin/expect -f
set timeout 600
{self._XUI_EXPECT_SPAWN_ONE_LINER}
expect {{
    # Опциональный промпт - появляется только при первой установке с дефолтными учетными данными
    "Would you like to customize the Panel Port settings*" {{
        send "n\\r"
        exp_continue
    }}
    # Выбор типа SSL сертификата
    "Choose an option (default 2 for IP):" {{
        send "1\\r"
        exp_continue
    }}
    # Ввод доменного имени
    "Please enter your domain name:" {{
        send "{ssl_domain}\\r"
        exp_continue
    }}
    # Выбор порта для standalone режима acme.sh
    "Please choose which port to use (default is 80):" {{
        send "{cert_port}\\r"
        exp_continue
    }}
    # Настройка reloadcmd для ACME
    "Would you like to modify --reloadcmd for ACME*" {{
        send "n\\r"
        exp_continue
    }}
    # Установка сертификата для панели (несколько вариантов промпта для надежности)
    -re "(?i).*Would you like to set this certificate for the panel.*" {{
        send "y\\r"
        exp_continue
    }}
    -re "(?i).*set this certificate for the panel.*" {{
        send "y\\r"
        exp_continue
    }}
    -re "(?i).*certificate for the panel.*" {{
        send "y\\r"
        exp_continue
    }}
    "set this certificate*" {{
        send "y\\r"
        exp_continue
    }}
    "certificate for the panel*" {{
        send "y\\r"
        exp_continue
    }}
    eof {{
        catch wait result
        exit [lindex $result 3]
    }}
    timeout {{
        exit 1
    }}
}}'''
        else:
            # Expect скрипт для IP сертификата (по умолчанию)
            # Обновлен для работы с новой версией официального скрипта
            install_script = f'''#!/usr/bin/expect -f
set timeout 600
{self._XUI_EXPECT_SPAWN_ONE_LINER}
expect {{
    # Опциональный промпт - появляется только при первой установке с дефолтными учетными данными
    "Would you like to customize the Panel Port settings*" {{
        send "n\\r"
        exp_continue
    }}
    # Выбор типа SSL сертификата
    "Choose an option (default 2 for IP):" {{
        send "2\\r"
        exp_continue
    }}
    # Новый промпт: выбор порта для ACME HTTP-01 listener (появился в обновленной версии)
    "Port to use for ACME HTTP-01 listener (default 80):" {{
        send "{cert_port}\\r"
        exp_continue
    }}
    # Опциональный промпт: если порт занят, предлагается выбрать другой
    "Enter another port for acme.sh standalone listener (leave empty to abort):" {{
        send "\\r"
        exp_continue
    }}
    # Проверка IPv6 адреса
    "Do you have an IPv6 address to include*" {{
        send "\\r"
        exp_continue
    }}
    eof {{
        catch wait result
        exit [lindex $result 3]
    }}
    timeout {{
        exit 1
    }}
}}'''
        
        try:
            # Проверяем наличие expect
            check_result = await conn.run('which expect || command -v expect', check=False)
            expect_path = (check_result.stdout or '').strip()

            if not expect_path:
                logger.info("Установка expect...")
                install_cmd = 'apt-get update && apt-get install -y expect || yum install -y expect || dnf install -y expect'
                await conn.run(install_cmd, check=False)

            # Для доменного сертификата проверяем socat
            if certificate_type == 'domain' and ssl_domain:
                logger.info("Проверка naличия socat...")
                socat_result = await conn.run('which socat || command -v socat', check=False)
                socat_path = (socat_result.stdout or '').strip()

                if not socat_path:
                    logger.info("Установка socat...")
                    install_socat_result = await conn.run(
                        'apt-get update && apt-get install -y socat || yum install -y socat || dnf install -y socat',
                        check=False
                    )
                    if install_socat_result.exit_status != 0:
                        logger.warning(f"Ошибка установки socat: {install_socat_result.stderr}")
                    else:
                        logger.info("socat успешно установлен")

            # Записываем expect-скрипт на сервер через heredoc
            expect_script_path = '/tmp/install_3xui_auto.expect'
            write_script_cmd = f'''cat > {expect_script_path} << 'EXPECT_EOF'
{install_script}
EXPECT_EOF
chmod +x {expect_script_path}'''
            await conn.run(write_script_cmd, check=False)

            # Выполняем скрипт — читаем вывод асинхронно построчно
            execute_command = f'chmod +x {expect_script_path} && {expect_script_path}'
            output_lines = []
            error_lines = []

            async with conn.create_process(execute_command, term_type='xterm') as process:
                async for line in process.stdout:
                    decoded_line = line.strip() if isinstance(line, str) else line.decode('utf-8', errors='ignore').strip()
                    if decoded_line:
                        output_lines.append(decoded_line)
                        logger.debug(f"Install output: {decoded_line}")
                # Читаем stderr после завершения
                stderr_data = await process.stderr.read()
                if stderr_data:
                    for line in (stderr_data if isinstance(stderr_data, str) else stderr_data.decode('utf-8', errors='ignore')).splitlines():
                        stripped = line.strip()
                        if stripped:
                            error_lines.append(stripped)
                            logger.warning(f"Install stderr: {stripped}")
                await process.wait_closed()
                exit_status = process.exit_status

            # Удаляем временный скрипт
            try:
                await conn.run(f'rm -f {expect_script_path}', check=False)
            except Exception:
                pass

            full_output = '\n'.join(output_lines)
            full_errors  = '\n'.join(error_lines)

            if exit_status == 0:
                return True, full_output, full_errors
            else:
                return False, full_output, full_errors

        except Exception as e:
            logger.error(f"Ошибка выполнения скрипта установки: {e}")
            return False, "", str(e)
    
    def parse_install_output(self, output: str) -> Dict[str, any]:
        """Парсинг вывода официального скрипта установки"""
        # Удаляем ANSI escape-коды из вывода
        clean_output = strip_ansi_codes(output)
        
        result = {}
        
        # Username (улучшенный regex для обработки ANSI кодов)
        username_match = re.search(r'Username:\s+([^\s\x1b\n]+)', clean_output)
        if username_match:
            result['username'] = strip_ansi_codes(username_match.group(1)).strip()
        
        # Password (улучшенный regex)
        password_match = re.search(r'Password:\s+([^\s\x1b\n]+)', clean_output)
        if password_match:
            result['password'] = strip_ansi_codes(password_match.group(1)).strip()
        
        # Port
        port_match = re.search(r'Port:\s+(\d+)', clean_output)
        if port_match:
            result['port'] = int(port_match.group(1))
        
        # WebBasePath (улучшенный regex)
        webpath_match = re.search(r'WebBasePath:\s+([^\s\x1b\n]+)', clean_output)
        if webpath_match:
            result['webBasePath'] = strip_ansi_codes(webpath_match.group(1)).strip()
        
        # Access URL (для получения IP и проверки)
        url_match = re.search(r'Access URL:\s+https?://([^:/]+):(\d+)/(.+)', clean_output)
        if url_match:
            result['server_ip'] = strip_ansi_codes(url_match.group(1)).strip()
            result['port_from_url'] = int(url_match.group(2))
            result['webBasePath_from_url'] = strip_ansi_codes(url_match.group(3)).strip()
        
        return result
    
    async def verify_panel_access(
        self, server_ip: str, port: int, web_base_path: str, username: str, password: str
    ) -> Tuple[bool, str]:
        """
        Проверка доступности панели после установки.

        После того как `install.sh` дёрнул `systemctl restart x-ui`, панели нужно
        несколько секунд, чтобы открыть TLS-порт. Поэтому:

          1. Сразу после установки спим 3 секунды.
          2. До каждой HTTPS-попытки делаем TCP-probe (5с) — если TCP даже не
             открывается, ждать 30с на TLS-handshake бесполезно, лучше быстро
             уйти на retry.
          3. До 3 попыток с backoff 5с / 10с — обычно панель встаёт уже на 1-2.
        """
        # Очищаем все параметры от ANSI кодов и лишних символов
        server_ip     = strip_ansi_codes(server_ip).strip()
        web_base_path = strip_ansi_codes(web_base_path).strip().strip('/')
        username      = strip_ansi_codes(username).strip()
        password      = strip_ansi_codes(password).strip()

        url = f"https://{server_ip}:{port}/{web_base_path}"

        # Дай x-ui.service дошуршать после restart — обычно 2-3 секунды.
        await asyncio.sleep(3)

        backoffs = (0, 5, 10)        # 3 попытки, паузы перед каждой
        last_err: str = ''
        for attempt, wait_before in enumerate(backoffs, start=1):
            if wait_before:
                await asyncio.sleep(wait_before)

            # Сначала смотрим, открыт ли TCP — это быстрее, чем ждать TLS.
            tcp_ok, tcp_msg = await self._tcp_probe(server_ip, port, timeout=5.0)
            if not tcp_ok:
                last_err = (
                    f'TCP {server_ip}:{port} не открывается ({tcp_msg}). '
                    f'Возможно, панель ещё не запустилась после restart, либо '
                    f'облачный файрвол блокирует порт {port}.'
                )
                logger.warning(f'[verify_panel_access] attempt {attempt}/{len(backoffs)}: {last_err}')
                continue

            # TCP открыт — пробуем py3xui login.
            try:
                client = AsyncApi(url, username, password, use_tls_verify=False)
                await asyncio.wait_for(client.login(), timeout=15)
                return True, f"Панель доступна: {url}"
            except asyncio.TimeoutError:
                last_err = 'TLS handshake / login timeout (>15с) — панель отвечает на TCP, но HTTPS падает.'
                logger.warning(f'[verify_panel_access] attempt {attempt}/{len(backoffs)}: {last_err}')
            except Exception as e:
                last_err = f'{type(e).__name__}: {e}'
                logger.warning(f'[verify_panel_access] attempt {attempt}/{len(backoffs)}: {last_err}')

        # Все попытки исчерпаны.
        return False, (
            f'Не удалось проверить панель после {len(backoffs)} попыток '
            f'(retries: {", ".join(str(b) for b in backoffs)}с). Последняя ошибка:\n  {last_err}\n\n'
            f'Если панель установилась корректно (видно в SSH через `x-ui status`), '
            f'причиной может быть:\n'
            f'  • облачный файрвол провайдера (открыть порт {port}/tcp);\n'
            f'  • UFW/iptables на сервере не пускает входящий :{port};\n'
            f'  • с этого хостинга админки заблокированы исходящие на нестандартные порты.'
        )
    
    async def load_inbound_template(self, inbound_type: str) -> Dict:
        """Загрузка шаблона инбаунда из БД"""
        if inbound_type not in self.INBOUND_TEMPLATES:
            raise ValueError(f"Неизвестный тип инбаунда: {inbound_type}")
        
        # Импортируем здесь, чтобы избежать циклических зависимостей
        from web_admin.async_db import async_query_db
        
        # Загружаем шаблон из БД
        setting_key = f'ssh_inbound_template_{inbound_type}'
        template_row = await async_query_db(
            "SELECT value FROM settings WHERE key = ?", 
            (setting_key,), 
            one=True
        )
        
        if not template_row or not template_row.get('value'):
            # Если шаблона нет в БД, используем дефолтный из файла (fallback)
            logger.warning(f"Шаблон {inbound_type} не найден в БД, используем fallback")
            return await self._load_default_template(inbound_type)
        
        try:
            template = json.loads(template_row['value'])
            return template
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга шаблона {inbound_type} из БД: {e}")
            # Fallback на дефолтный шаблон
            return await self._load_default_template(inbound_type)
    
    async def _load_default_template(self, inbound_type: str) -> Dict:
        """Загрузка дефолтного шаблона из файла (fallback)"""
        web_admin_dir = Path(__file__).parent.parent
        templates_dir = web_admin_dir / 'ssh' / 'inbound_templates'
        
        template_files = {
            'vless_reality_tcp': 'vless_reality_tcp.json',
            'vless_reality_xhttp': 'vless_reality_xhttp.json'
        }
        
        template_file = templates_dir / template_files.get(inbound_type, 'vless_reality_tcp.json')
        
        if not template_file.exists():
            raise FileNotFoundError(f"Шаблон инбаунда не найден: {template_file}")
        
        with open(template_file, 'r', encoding='utf-8') as f:
            template = json.load(f)
        
        return template
    
    async def create_inbound_by_type(
        self,
        api_client: AsyncApi,
        inbound_type: str,
        server_ip: str,
        client_flow: str = ''
    ) -> Tuple[bool, str, Optional[int], Optional[Dict]]:
        """Создание инбаунда по выбранному типу.

        `client_flow` применяется к генерируемому тестовому клиенту. Для
        TCP+Reality по умолчанию имеет смысл передавать 'xtls-rprx-vision'.
        Для XHTTP — должно быть пустой строкой (vision несовместим с XHTTP).

        Returns:
            (ok, message, inbound_id, client_info)
            client_info: {'uuid': str, 'subId': str, 'email': str} — данные тестового
            клиента, нужны вызывающему для запроса subscription endpoint и сборки
            key_template. None — если не удалось.
        """
        try:
            # Загружаем шаблон из БД
            template = await self.load_inbound_template(inbound_type)
            
            # Защита: для XHTTP обнуляем flow вне зависимости от того, что пришло.
            effective_flow = client_flow if inbound_type == 'vless_reality_tcp' else ''
            
            # Если в шаблоне нет клиентов, добавляем тестового клиента
            settings_dict = template['settings'].copy()
            client_info: Optional[Dict] = None
            if not settings_dict.get('clients') or len(settings_dict['clients']) == 0:
                # Генерируем UUID для клиента
                client_uuid = str(uuid.uuid4())
                # Генерируем короткий идентификатор для email и subId
                short_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
                
                settings_dict['clients'] = [{
                    'id': client_uuid,
                    'flow': effective_flow,
                    'email': short_id,
                    'limitIp': 0,
                    'totalGB': 0,
                    'expiryTime': 0,
                    'enable': True,
                    'tgId': '',
                    'subId': short_id,
                    'comment': '',
                    'reset': 0
                }]
                client_info = {'uuid': client_uuid, 'subId': short_id, 'email': short_id}
                logger.info(f"Добавлен тестовый клиент в инбаунд: UUID={client_uuid}, email={short_id}, flow={effective_flow!r}")
            else:
                # В шаблоне УЖЕ были клиенты — берём первого как baseline для
                # запроса subscription endpoint (если у админа кастомный шаблон).
                first = settings_dict['clients'][0]
                if isinstance(first, dict) and first.get('id') and first.get('subId'):
                    client_info = {
                        'uuid':  str(first.get('id')),
                        'subId': str(first.get('subId')),
                        'email': str(first.get('email', '')),
                    }
            
            # Создаем объекты из шаблона
            settings = Settings(**settings_dict)
            sniffing = Sniffing(**template['sniffing'])
            
            # StreamSettings может содержать различные настройки в зависимости от network
            # Используем прямой словарь для передачи всех полей, включая xhttpSettings
            stream_settings_dict = template['streamSettings'].copy()
            stream_settings = StreamSettings(**stream_settings_dict)
            
            # Создаем объект Inbound
            inbound = Inbound(
                enable=template.get('enable', True),
                port=template['port'],
                protocol=template['protocol'],
                settings=settings,
                stream_settings=stream_settings,
                sniffing=sniffing,
                remark=template.get('remark', ''),
                listen=template.get('listen', '')
            )
            
            # Добавляем инбаунд через API
            await api_client.inbound.add(inbound)
            
            # Получаем список инбаундов чтобы найти созданный (по порту)
            inbounds = await api_client.inbound.get_list()
            created_inbound = next(
                (inb for inb in inbounds if inb.port == template['port']),
                None
            )
            
            if created_inbound:
                inbound_id = created_inbound.id
                logger.info(f"Инбаунд создан успешно: ID={inbound_id}, порт={template['port']}")
                return True, f"Инбаунд создан успешно (ID: {inbound_id})", inbound_id, client_info
            else:
                return True, "Инбаунд создан, но не удалось получить ID", None, client_info
                
        except Exception as e:
            logger.error(f"Ошибка создания инбаунда: {e}")
            return False, f"Ошибка создания инбаунда: {str(e)}", None, None
    
    async def install_3xui(
        self,
        task_id: str,
        host: str,
        ssh_port: int,
        ssh_username: str,
        ssh_password: str,
        inbound_type: str = 'vless_reality_tcp',
        server_name: str = '',
        certificate_type: str = 'ip',
        ssl_domain: Optional[str] = None,
        cert_port: int = 80,
        client_flow: str = 'xtls-rprx-vision'
    ) -> Dict:
        """Основной метод установки 3X-UI"""
        try:
            # Обновляем статус задачи
            self.install_tasks[task_id] = {
                'status': 'running',
                'progress': 0,
                'message': 'Начало установки...',
                'logs': [],
                'error': None,
                'result': None
            }
            
            def update_status(progress: int, message: str, log: str = None):
                """Обновление статуса задачи + всегда добавляем запись в логи.

                Раньше запись в logs[] происходила ТОЛЬКО когда вызывающий передавал
                третий аргумент log=... — большинство вызовов его не передавало,
                из-за чего в модалке UI лог был пустой, и при ошибках пользователь
                видел только тост (без подробностей).

                Теперь:
                  • каждое изменение прогресса дублируется в logs[];
                  • если передали `log` (расширенный текст с эмодзи/деталями) — он;
                  • иначе используем `message` (короткая фраза текущего шага).
                """
                self.install_tasks[task_id]['progress'] = progress
                self.install_tasks[task_id]['message'] = message
                self.install_tasks[task_id]['logs'].append({
                    'time': datetime.now().isoformat(),
                    'message': log if log else message,
                })

            def fail_status(error_msg: str):
                """Завершить задачу со status='error' и записать ошибку и в logs[],
                чтобы пользователь увидел её прямо в окне логов модалки, а не
                только во всплывающем toast."""
                self.install_tasks[task_id]['status'] = 'error'
                self.install_tasks[task_id]['error']  = error_msg
                self.install_tasks[task_id]['logs'].append({
                    'time': datetime.now().isoformat(),
                    'message': f'❌ Ошибка: {error_msg}',
                })
            
            # Шаг 0: TCP-проба до запуска SSH-handshake.
            # На медленных/закрытых сетях asyncssh может висеть всё 30 сек,
            # хотя сразу видно, что порт недоступен. Так пользователь сразу
            # понимает: дело в сети/файрволе, а не в логине/пароле.
            update_status(5, f'Проверка доступности {host}:{ssh_port}...')
            tcp_ok, tcp_msg = await self._tcp_probe(host, ssh_port, timeout=5.0)
            if not tcp_ok:
                error_msg = f'TCP-проба не прошла:\n{tcp_msg}'
                fail_status(error_msg)
                return {'success': False, 'error': error_msg}
            update_status(8, 'TCP-проба пройдена', tcp_msg)

            # Шаг 1: SSH handshake + auth.
            # ВАЖНО: ранее тут было ДВА захода (check_ssh_connection с timeout=5
            # и потом _create_ssh_connection) — теперь один.
            update_status(10, 'SSH handshake и аутентификация...')
            try:
                conn = await self._create_ssh_connection(
                    host, ssh_port, ssh_username, ssh_password, timeout=30
                )
            except asyncssh.PermissionDenied:
                error_msg = 'Неверные учётные данные SSH (логин или пароль).'
                fail_status(error_msg)
                return {'success': False, 'error': error_msg}
            except asyncio.TimeoutError:
                # Сюда мы попадаем уже ПОСЛЕ успешной TCP-пробы (банер SSH-2.0-* пришёл).
                # Значит порт реально открыт и это SSH-сервис, но handshake не идёт.
                # Самые частые причины: KEX-несовместимость, sshd MaxStartups,
                # tcp_wrappers/Match-блок не пускают наш IP.
                error_msg = (
                    'SSH-сервис ответил на TCP, но handshake завис (>30 сек).\n'
                    'Возможные причины:\n'
                    '  • PasswordAuthentication=no в /etc/ssh/sshd_config;\n'
                    '  • Match-блок / AllowUsers / DenyUsers режут IP админки;\n'
                    '  • MaxStartups переполнен (другие клиенты бьются параллельно);\n'
                    '  • несовместимые алгоритмы KEX (старая Ubuntu / новая asyncssh).\n\n'
                    'Проверка с самой админки: ssh -vvv -p {port} {user}@{host} 2>&1 | head -40'
                ).format(port=ssh_port, user=ssh_username, host=host)
                fail_status(error_msg)
                return {'success': False, 'error': error_msg}
            except OSError as e:
                # Сетевые ошибки: connection refused / no route / DNS-fail.
                error_msg = (
                    f'Сетевая ошибка ({type(e).__name__}): {e}.\n'
                    f'Проверьте: SSH порт ({ssh_port}), DNS/IP хоста, и что файрвол на сервере '
                    f'разрешает входящие на этот порт.'
                )
                fail_status(error_msg)
                return {'success': False, 'error': error_msg}
            except asyncssh.Error as e:
                # Любые ошибки уровня SSH (handshake/KEX/key/version mismatch).
                error_msg = (
                    f'Ошибка SSH-протокола ({type(e).__name__}): {e}.\n'
                    f'Возможные причины: SSH-сервер не отвечает, версия SSH несовместима, '
                    f'PasswordAuthentication=no в /etc/ssh/sshd_config (нужно включить).'
                )
                fail_status(error_msg)
                return {'success': False, 'error': error_msg}

            update_status(25, 'SSH подключение установлено', f'Подключились к {host}:{ssh_port}')

            try:
                # Шаг 3: Проверка ОС
                update_status(30, 'Проверка операционной системы...')
                os_ok, os_msg = await self.check_os(conn)
                if not os_ok:
                    fail_status(os_msg)
                    return {'success': False, 'error': os_msg}

                update_status(40, f'ОС проверена: {os_msg}', os_msg)

                # Шаг 3.5: Проверка — не установлен ли 3X-UI уже
                update_status(45, 'Проверка наличия существующей установки...')
                xui_check = await conn.run(
                    'systemctl is-active x-ui 2>/dev/null || (command -v x-ui >/dev/null 2>&1 && echo "found") || echo "not_found"',
                    check=False
                )
                xui_status = (xui_check.stdout or '').strip().lower()
                if xui_status in ('active', 'found'):
                    error_msg = (
                        '3X-UI уже установлен на этом сервере. '
                        'Установка остановлена. '
                        'Если хотите переустановить — переустановите ОС на сервере.'
                    )
                    logger.warning(f"[SSH-INSTALL] {error_msg} ({host})")
                    fail_status(error_msg)
                    return {'success': False, 'error': error_msg}

                update_status(45, '3X-UI не обнаружен — начинаем чистую установку.', 'Сервер чистый, 3X-UI не установлен')

                # Шаг 4: Выполнение скрипта установки
                update_status(50, 'Запуск установки 3X-UI...', 'Выполняется официальный скрипт установки...')
                install_ok, install_output, install_errors = await self.execute_install_script(
                    conn,
                    certificate_type=certificate_type,
                    ssl_domain=ssl_domain,
                    cert_port=cert_port
                )
                
                if not install_ok:
                    error_msg = f"Ошибка установки:\n{install_errors}"
                    fail_status(error_msg)
                    return {'success': False, 'error': error_msg}
                
                update_status(70, 'Установка завершена, парсинг данных...', 'Скрипт установки выполнен успешно')
                
                # Шаг 5: Парсинг вывода
                parsed_data = self.parse_install_output(install_output)
                
                if not parsed_data.get('username') or not parsed_data.get('password'):
                    error_msg = "Не удалось извлечь данные панели из вывода установки"
                    fail_status(error_msg)
                    return {'success': False, 'error': error_msg}
                
                # Очищаем данные от ANSI кодов
                username = strip_ansi_codes(parsed_data.get('username', '')).strip()
                password = strip_ansi_codes(parsed_data.get('password', '')).strip()
                server_ip = strip_ansi_codes(parsed_data.get('server_ip', host)).strip()
                port = parsed_data.get('port') or parsed_data.get('port_from_url', 54321)
                web_base_path = strip_ansi_codes(parsed_data.get('webBasePath') or parsed_data.get('webBasePath_from_url', '')).strip().strip('/')
                
                # Для доменного сертификата: проверяем, был ли установлен сертификат для панели
                # Если нет, устанавливаем вручную
                if certificate_type == 'domain' and ssl_domain:
                    update_status(75, 'Проверка установки SSL сертификата для панели...', 
                        'Проверяем, был ли установлен сертификат для панели...')
                    
                    cert_file = f"/root/cert/{ssl_domain}/fullchain.pem"
                    key_file = f"/root/cert/{ssl_domain}/privkey.pem"
                    
                    # Проверяем наличие файлов сертификата
                    check_cert_cmd = f'test -f "{cert_file}" && test -f "{key_file}" && echo "exists" || echo "not_found"'
                    cert_check_result = await conn.run(check_cert_cmd, check=False)
                    cert_check_output = (cert_check_result.stdout or '').strip()

                    if cert_check_output == "exists":
                        logger.info(f"Установка SSL сертификата для панели: {cert_file}")
                        install_cert_cmd = f'x-ui cert -webCert "{cert_file}" -webCertKey "{key_file}" 2>/dev/null || /usr/local/x-ui/x-ui cert -webCert "{cert_file}" -webCertKey "{key_file}" 2>/dev/null || /usr/bin/x-ui cert -webCert "{cert_file}" -webCertKey "{key_file}"'
                        cert_install_result = await conn.run(install_cert_cmd, check=False)

                        if cert_install_result.exit_status == 0:
                            logger.info("SSL сертификат успешно установлен для панели")
                            update_status(76, 'SSL сертификат установлен для панели',
                                f'✅ Сертификат установлен:\n   Cert: {cert_file}\n   Key: {key_file}')

                            # Перезапускаем панель
                            restart_cmd = 'systemctl restart x-ui 2>/dev/null || rc-service x-ui restart 2>/dev/null || true'
                            await conn.run(restart_cmd, check=False)
                            await asyncio.sleep(2)
                        else:
                            logger.warning(f"Не удалось установить SSL сертификат для панели: {cert_install_errors}")
                            update_status(76, 'Предупреждение: не удалось установить SSL сертификат автоматически', 
                                f'⚠️ Сертификат получен, но не установлен для панели.\n   Установите вручную: x-ui cert -webCert "{cert_file}" -webCertKey "{key_file}"')
                    else:
                        logger.warning(f"Файлы сертификата не найдены: {cert_file}, {key_file}")
                
                # Формируем полный URL панели
                access_url = f"https://{server_ip}:{port}/{web_base_path}"
                
                update_status(80, 'Данные панели получены', 
                    f"📋 Полная информация о панели:\n"
                    f"   URL: {access_url}\n"
                    f"   Username: {username}\n"
                    f"   Password: {password}\n"
                    f"   Port: {port}\n"
                    f"   WebBasePath: {web_base_path}")
                
                # Шаг 6: Проверка доступности панели
                update_status(85, 'Проверка доступности панели...')
                panel_ok, panel_msg = await self.verify_panel_access(
                    server_ip, port, web_base_path, username, password
                )
                
                if not panel_ok:
                    error_msg = f"Панель недоступна: {panel_msg}"
                    fail_status(error_msg)
                    return {'success': False, 'error': error_msg}
                
                update_status(90, 'Панель доступна', panel_msg)
                
                # Шаг 7: Создание инбаунда
                update_status(95, f'Создание инбаунда типа: {inbound_type}...')
                
                # Подключаемся к API для создания инбаунда
                api_client = AsyncApi(access_url, username, password, use_tls_verify=False)
                await api_client.login()
                
                inbound_ok, inbound_msg, inbound_id, client_info = await self.create_inbound_by_type(
                    api_client, inbound_type, server_ip,
                    client_flow=client_flow,
                )
                
                if not inbound_ok:
                    logger.warning(f"Не удалось создать инбаунд: {inbound_msg}")
                    # Не считаем это критической ошибкой - сервер установлен
                
                # ── Получение готового key_template из subscription endpoint ──
                # subscription по дефолту включена в свежей 3X-UI после install.sh.
                # Запрашиваем base64-список с подпиской тестового клиента,
                # достаём vless://, заменяем UUID/имя на плейсхолдеры.
                # Любая ошибка → key_template=None и админка добавит сервер с
                # legacy-полями (use_key_template=False).
                key_template: Optional[str] = None
                if client_info and client_info.get('subId'):
                    update_status(98, 'Получение шаблона ключа из подписки...')
                    key_template = await self._fetch_subscription_template(
                        server_ip=server_ip,
                        sub_id=client_info['subId'],
                        client_uuid=client_info['uuid'],
                        sub_port=2096,
                        sub_path='sub',
                    )
                    if key_template:
                        update_status(99, 'Шаблон ключа получен',
                                      f'✅ key_template:\n   {key_template[:120]}…')
                    else:
                        update_status(99, 'Не удалось получить шаблон через подписку — fallback на legacy-поля',
                                      '⚠️ subscription endpoint не вернул vless:// — добавим сервер с public_host/port.')

                update_status(100, 'Установка завершена успешно!', 
                    f"✅ Панель установлена и доступна!\n\n"
                    f"📋 Полная информация:\n"
                    f"   URL: {access_url}\n"
                    f"   Username: {username}\n"
                    f"   Password: {password}\n"
                    f"   Port: {port}\n"
                    f"   WebBasePath: {web_base_path}\n"
                    f"   Inbound ID: {inbound_id or 'N/A'}\n"
                    + (inbound_msg if inbound_ok else f"\n⚠️ Предупреждение: {inbound_msg}"))
                
                # Формируем результат. client_flow возвращаем актуальный
                # (для XHTTP всегда '' — даже если на входе был vision).
                effective_client_flow = client_flow if inbound_type == 'vless_reality_tcp' else ''
                result = {
                    'success': True,
                    'server_ip': server_ip,
                    'port': port,
                    'webBasePath': web_base_path,
                    'username': username,
                    'password': password,
                    'inbound_id': inbound_id,
                    'inbound_type': inbound_type,
                    'access_url': access_url,
                    'ssh_host': host,  # IP адрес для SSH (используется как public_host)
                    'client_flow': effective_client_flow,
                    # Если subscription отдала vless:// — выставляем шаблон,
                    # иначе пустая строка (use_key_template=False на фронте).
                    'key_template': key_template or '',
                    'use_key_template': bool(key_template),
                }
                
                self.install_tasks[task_id]['status'] = 'completed'
                self.install_tasks[task_id]['result'] = result
                
                return result
                
            finally:
                conn.close()
                
        except Exception as e:
            logger.error(f"Ошибка установки 3X-UI: {e}", exc_info=True)
            error_msg = f"Критическая ошибка: {type(e).__name__}: {e}"
            try:
                self.install_tasks[task_id]['status'] = 'error'
                self.install_tasks[task_id]['error'] = error_msg
                # Дублируем ошибку в logs[], чтобы фронт показал её в окне модалки.
                self.install_tasks[task_id].setdefault('logs', []).append({
                    'time': datetime.now().isoformat(),
                    'message': f'❌ {error_msg}',
                })
            except Exception:
                pass
            return {'success': False, 'error': error_msg}
    
    def get_task_status(self, task_id: str) -> Optional[Dict]:
        """Получение статуса задачи установки"""
        return self.install_tasks.get(task_id)


# Глобальный экземпляр установщика
ssh_installer_instance = SSH3XUIInstaller()

