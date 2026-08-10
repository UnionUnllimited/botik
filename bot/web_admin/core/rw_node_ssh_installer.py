"""
Установка Remnawave Node на VPS через SSH (docker compose).
"""

import asyncio
import base64
import shlex
import uuid
from datetime import datetime
from typing import Dict, Optional

import asyncssh
from loguru import logger

from web_admin.core.ssh_utils import check_os, tcp_probe

INSTALL_DIR = '/opt/remnanode'
COMPOSE_FILENAME = 'docker-compose.yml'


def build_remnanode_compose(node_port: int, secret_key: str) -> str:
    """Генерирует docker-compose.yml как в панели Remnawave."""
    key = secret_key.replace('\\', '\\\\').replace('"', '\\"')
    return f"""services:
  remnanode:
    container_name: remnanode
    hostname: remnanode
    image: remnawave/node:latest
    network_mode: host
    restart: always
    cap_add:
      - NET_ADMIN
    ulimits:
      nofile:
        soft: 1048576
        hard: 1048576
    volumes:
      - /etc/letsencrypt:/etc/letsencrypt:ro
      - /var/log/remnanode:/var/log/remnanode
    environment:
      - NODE_PORT={int(node_port)}
      - SECRET_KEY="{key}"
"""


class SSHRemnawaveNodeInstaller:
    """Установка Remnawave Node через SSH."""

    def __init__(self):
        self.install_tasks: Dict[str, Dict] = {}

    async def _create_ssh_connection(
        self,
        host: str,
        port: int,
        username: str,
        password: Optional[str] = None,
        private_key: Optional[str] = None,
        timeout: int = 30,
    ) -> asyncssh.SSHClientConnection:
        connect_kwargs = dict(
            host=host,
            port=port,
            username=username,
            known_hosts=None,
            keepalive_interval=60,
            keepalive_count_max=10,
        )
        if private_key and private_key.strip():
            key = asyncssh.import_private_key(private_key.strip())
            connect_kwargs['client_keys'] = [key]
            connect_kwargs['preferred_auth'] = 'publickey,password,keyboard-interactive'
            if password:
                connect_kwargs['password'] = password
        else:
            connect_kwargs['password'] = password or ''
            connect_kwargs['client_keys'] = ()
            connect_kwargs['preferred_auth'] = 'password,keyboard-interactive'

        return await asyncio.wait_for(asyncssh.connect(**connect_kwargs), timeout=timeout)

    async def _run_remote(self, conn: asyncssh.SSHClientConnection, cmd: str, timeout: int = 600):
        return await conn.run(cmd, check=False, timeout=timeout)

    async def install_remnanode(
        self,
        task_id: str,
        *,
        host: str,
        ssh_port: int,
        ssh_username: str,
        ssh_password: Optional[str],
        ssh_private_key: Optional[str],
        node_name: str,
        node_address: str,
        node_port: int,
        country_code: str,
        config_profile_uuid: str,
        inbound_uuids: list,
    ) -> Dict:
        from remnawave_manager import remnawave_manager_instance

        self.install_tasks[task_id] = {
            'status': 'running',
            'progress': 0,
            'message': 'Начало установки…',
            'logs': [],
            'error': None,
            'result': None,
        }

        def update_status(progress: int, message: str, log: str = None):
            self.install_tasks[task_id]['progress'] = progress
            self.install_tasks[task_id]['message'] = message
            self.install_tasks[task_id]['logs'].append({
                'time': datetime.now().isoformat(),
                'message': log if log else message,
            })

        def fail_status(error_msg: str):
            self.install_tasks[task_id]['status'] = 'error'
            self.install_tasks[task_id]['error'] = error_msg
            self.install_tasks[task_id]['logs'].append({
                'time': datetime.now().isoformat(),
                'message': f'❌ Ошибка: {error_msg}',
            })

        node_uuid: Optional[str] = None

        try:
            update_status(5, f'Проверка доступности {host}:{ssh_port}…')
            tcp_ok, tcp_msg = await tcp_probe(host, ssh_port, timeout=5.0)
            if not tcp_ok:
                fail_status(f'TCP-проба не прошла:\n{tcp_msg}')
                return {'success': False, 'error': tcp_msg}
            update_status(8, 'TCP-проба пройдена', tcp_msg)

            update_status(12, 'Генерация SECRET_KEY на панели…')
            secret_key, key_err = await remnawave_manager_instance.generate_node_secret_key()
            if not secret_key:
                fail_status(key_err or 'Не удалось получить SECRET_KEY (/keygen)')
                return {'success': False, 'error': key_err}

            update_status(18, 'SECRET_KEY получен', f'Длина ключа: {len(secret_key)} символов')

            update_status(22, 'Создание ноды на панели Remnawave…')
            node_data, create_err = await remnawave_manager_instance.create_node(
                name=node_name,
                address=node_address,
                port=node_port,
                country_code=country_code,
                config_profile_uuid=config_profile_uuid,
                inbound_uuids=inbound_uuids,
            )
            if not node_data:
                fail_status(create_err or 'POST /nodes не удался')
                return {'success': False, 'error': create_err}
            node_uuid = node_data.get('uuid')
            update_status(
                28,
                'Нода создана на панели',
                f'UUID: {node_uuid}\nИмя: {node_name}\nАдрес: {node_address}:{node_port}',
            )

            update_status(32, 'SSH подключение…')
            try:
                conn = await self._create_ssh_connection(
                    host, ssh_port, ssh_username,
                    password=ssh_password,
                    private_key=ssh_private_key,
                    timeout=30,
                )
            except asyncssh.PermissionDenied:
                fail_status('Неверные SSH учётные данные')
                return {'success': False, 'error': 'SSH auth failed', 'node_uuid': node_uuid}
            except Exception as e:
                fail_status(f'SSH: {type(e).__name__}: {e}')
                return {'success': False, 'error': str(e), 'node_uuid': node_uuid}

            try:
                update_status(36, 'Проверка ОС…')
                os_ok, os_msg = await check_os(conn)
                if not os_ok:
                    fail_status(os_msg)
                    return {'success': False, 'error': os_msg, 'node_uuid': node_uuid}
                update_status(40, f'ОС: {os_msg}', os_msg)

                update_status(44, 'Проверка существующей установки remnanode…')
                existing = await self._run_remote(
                    conn,
                    'docker ps -a --format "{{.Names}}" 2>/dev/null | grep -qx remnanode && echo yes || echo no',
                )
                if (existing.stdout or '').strip().lower() == 'yes':
                    fail_status(
                        'Контейнер remnanode уже есть на сервере. '
                        'Остановите/удалите его или переустановите ОС.'
                    )
                    return {'success': False, 'error': 'remnanode already exists', 'node_uuid': node_uuid}
                update_status(46, 'Сервер чистый — remnanode не найден')

                update_status(50, 'Проверка Docker…')
                docker_check = await self._run_remote(
                    conn,
                    'command -v docker >/dev/null 2>&1 && (docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1) && echo ok || echo missing',
                )
                if (docker_check.stdout or '').strip() != 'ok':
                    update_status(55, 'Установка Docker…', 'curl -fsSL https://get.docker.com | sh')
                    install_docker = await self._run_remote(
                        conn,
                        'curl -fsSL https://get.docker.com | sh',
                        timeout=900,
                    )
                    if install_docker.exit_status != 0:
                        err = (install_docker.stderr or install_docker.stdout or 'unknown')[:800]
                        fail_status(f'Не удалось установить Docker:\n{err}')
                        return {'success': False, 'error': err, 'node_uuid': node_uuid}
                    update_status(62, 'Docker установлен', 'Docker Engine готов')
                else:
                    update_status(58, 'Docker уже установлен')

                compose_text = build_remnanode_compose(node_port, secret_key)
                compose_b64 = base64.b64encode(compose_text.encode('utf-8')).decode('ascii')

                update_status(68, f'Запись {COMPOSE_FILENAME}…', f'Каталог: {INSTALL_DIR}')
                write_cmd = (
                    f'mkdir -p {shlex.quote(INSTALL_DIR)} /var/log/remnanode && '
                    f'echo {shlex.quote(compose_b64)} | base64 -d > {shlex.quote(INSTALL_DIR + "/" + COMPOSE_FILENAME)}'
                )
                write_res = await self._run_remote(conn, write_cmd)
                if write_res.exit_status != 0:
                    err = (write_res.stderr or write_res.stdout or '')[:500]
                    fail_status(f'Не удалось записать compose:\n{err}')
                    return {'success': False, 'error': err, 'node_uuid': node_uuid}
                update_status(72, 'docker-compose.yml создан')

                update_status(78, 'Запуск контейнера (pull + up)…', 'docker compose up -d')
                up_cmd = (
                    f'cd {shlex.quote(INSTALL_DIR)} && '
                    f'(docker compose up -d 2>&1 || docker-compose up -d 2>&1)'
                )
                up_res = await self._run_remote(conn, up_cmd, timeout=900)
                up_out = ((up_res.stdout or '') + (up_res.stderr or '')).strip()
                if up_res.exit_status != 0:
                    fail_status(f'docker compose up завершился с ошибкой:\n{up_out[:1200]}')
                    return {'success': False, 'error': up_out[:500], 'node_uuid': node_uuid}
                update_status(85, 'Контейнер запущен', up_out[-1500:] if up_out else 'docker compose up -d OK')

                ps_res = await self._run_remote(
                    conn,
                    f'cd {shlex.quote(INSTALL_DIR)} && (docker compose ps 2>/dev/null || docker-compose ps)',
                )
                if ps_res.stdout:
                    update_status(88, 'Статус контейнера', ps_res.stdout.strip()[:1500])

            finally:
                conn.close()

            update_status(90, 'Ожидание подключения ноды к панели…')
            connected = False
            last_msg = ''
            for attempt in range(36):
                await asyncio.sleep(5)
                status = await remnawave_manager_instance.get_node_connection_status(node_uuid)
                if not status:
                    continue
                connected = bool(status.get('is_connected'))
                is_connecting = bool(status.get('is_connecting'))
                last_msg = status.get('last_status_message') or ''
                pct = 90 + min(9, attempt // 4)
                state = 'online' if connected else ('connecting' if is_connecting else 'offline')
                update_status(
                    pct,
                    f'Панель: {state} ({attempt + 1}/36)',
                    last_msg or f'is_connected={connected}',
                )
                if connected:
                    break

            if not connected:
                fail_status(
                    'Контейнер запущен, но панель не видит ноду (is_connected=false). '
                    'Проверьте firewall: NODE_PORT должен быть доступен с IP панели. '
                    f'UUID ноды: {node_uuid}'
                )
                return {
                    'success': False,
                    'error': 'Node not connected',
                    'node_uuid': node_uuid,
                }

            result = {
                'node_uuid': node_uuid,
                'node_name': node_name,
                'address': node_address,
                'port': node_port,
                'country_code': country_code,
                'install_dir': INSTALL_DIR,
            }
            self.install_tasks[task_id]['status'] = 'completed'
            self.install_tasks[task_id]['progress'] = 100
            self.install_tasks[task_id]['message'] = 'Установка завершена'
            self.install_tasks[task_id]['result'] = result
            update_status(100, '✅ Remnawave Node установлена и подключена к панели')
            return {'success': True, **result}

        except Exception as e:
            logger.exception(f"[RW-SSH-INSTALL] {e}")
            fail_status(str(e))
            return {'success': False, 'error': str(e), 'node_uuid': node_uuid}

    def get_task_status(self, task_id: str) -> Optional[Dict]:
        return self.install_tasks.get(task_id)


rw_node_ssh_installer = SSHRemnawaveNodeInstaller()
