import httpx
import json
import re
from datetime import date, datetime, timezone
from typing import Optional

from quart import Blueprint, render_template, request, jsonify, redirect, url_for, flash
from loguru import logger
from app_config import app_conf
from remnawave_manager import normalize_remnawave_node_uuid

# Настройки, перенесённые из «Основные» → «Remnawave → Настройки»
_REMNAWAVE_TOGGLE_KEYS = frozenset({
    'remnawave_enabled',
    'traffic_renewal_enabled',
    'sub_link_dedup_enabled',
    'remnawave_webhook_enabled',
    'remnawave_health_check_enabled',
    'xray_json_enabled',
    'xray_json_client_happ',
    'xray_json_client_incy',
    'xray_json_client_v2raytun',
    'xray_balancer_enabled',
    'xray_balancer_node_stats_enabled',
    'xray_balancer_routing_enabled',
})
_REMNAWAVE_VALUE_KEYS = frozenset({
    'remnawave_base_url',
    'remnawave_api_token',
    'remnawave_default_internal_squad_uuid',
    'remnawave_default_traffic_limit_gb',
    'remnawave_webhook_secret',
    'remnawave_traffic_exhausted_squad_uuid',
    'xray_balancer_max_users_per_gb',
    'xray_balancer_max_users_per_cpu',
    'xray_balancer_node_load_threshold',
    'xray_balancer_node_stats_interval_sec',
    'xray_balancer_routing_config',
    'xray_balancer_strategy',
    'xray_balancer_mode',
    'xray_balancer_auto_group_name',
    'xray_balancer_lte_auto_group_name',
    'xray_balancer_lte_triggers',
    'xray_balancer_auto_exclude',
    'xray_balancer_probe_url',
    'xray_balancer_probe_interval',
    'xray_balancer_probe_sampling',
    'xray_balancer_probe_timeout',
    'xray_balancer_tolerance',
    'xray_balancer_tolerance_fallback',
    'xray_balancer_load_expected',
    'xray_balancer_main_lte_baseline_ms',
    'xray_balancer_single_baseline_ms',
    'xray_balancer_lte_baseline_ms',
    'xray_balancer_lte_plus_baseline_ms',
    'xray_balancer_lte_minus_baseline_ms',
    'xray_balancer_dns_servers',
    'xray_balancer_dns_query_strategy',
    'xray_balancer_domain_strategy',
    'sub_link_dedup_mode',
    'sub_link_dedup_online_threshold',
    'sub_link_dedup_online_interval_sec',
})


# Дата продления во внутреннем имени ноды: «🇵🇱 Poland [19.06.2026]»
_RENEWAL_DATE_IN_NAME_RE = re.compile(r'\[(\d{2})\.(\d{2})\.(\d{4})\]')
_RENEWAL_SOON_DAYS = 5


def _parse_renewal_date_from_node_name(name: Optional[str]) -> Optional[date]:
    """Извлекает последнюю дату [DD.MM.YYYY] из имени ноды."""
    if not name:
        return None
    matches = list(_RENEWAL_DATE_IN_NAME_RE.finditer(str(name)))
    if not matches:
        return None
    m = matches[-1]
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _enrich_node_renewal(node: dict) -> None:
    """Добавляет renewal_* поля для сортировки и подсветки карточки."""
    renewal = _parse_renewal_date_from_node_name(node.get('name'))
    if renewal is None:
        node['renewal_date'] = None
        node['renewal_days_left'] = None
        node['renewal_soon'] = False
        node['renewal_overdue'] = False
        return
    today = datetime.now(timezone.utc).date()
    days_left = (renewal - today).days
    node['renewal_date'] = renewal.isoformat()
    node['renewal_days_left'] = days_left
    node['renewal_soon'] = 0 <= days_left <= _RENEWAL_SOON_DAYS
    node['renewal_overdue'] = days_left < 0


def _sort_nodes_by_renewal(nodes: list) -> list:
    """Сначала ноды с датой продления (ближайшие сверху), без даты — внизу по имени."""
    for n in nodes:
        _enrich_node_renewal(n)

    def _key(n: dict) -> tuple:
        rd = n.get('renewal_date')
        if rd:
            return (0, rd, (n.get('name') or '').lower())
        return (1, '', (n.get('name') or '').lower())

    return sorted(nodes, key=_key)


def _apply_renewal_date_to_node_name(name: Optional[str], renewal: date) -> str:
    """Подставляет или заменяет [DD.MM.YYYY] во внутреннем имени ноды."""
    block = f'[{renewal.strftime("%d.%m.%Y")}]'
    base = (name or '').strip()
    matches = list(_RENEWAL_DATE_IN_NAME_RE.finditer(base))
    if matches:
        m = matches[-1]
        return base[:m.start()] + block + base[m.end():]
    if not base:
        return block
    return f'{base} {block}'


def _renewal_fields_from_name(name: str) -> dict:
    """Словарь renewal_* для JSON-ответа после смены даты."""
    stub = {'name': name}
    _enrich_node_renewal(stub)
    return {
        'name': name,
        'renewal_date': stub.get('renewal_date'),
        'renewal_days_left': stub.get('renewal_days_left'),
        'renewal_soon': stub.get('renewal_soon'),
        'renewal_overdue': stub.get('renewal_overdue'),
    }


_REMNAWAVE_NODE_NAME_MAX_LEN = 30

DEFAULT_RW_PROFILE_CONFIG = {
    'log': {'loglevel': 'warning'},
    'inbounds': [],
    'outbounds': [
        {'tag': 'DIRECT', 'protocol': 'freedom'},
        {'tag': 'BLOCK', 'protocol': 'blackhole'},
    ],
    'routing': {'domainStrategy': 'AsIs', 'rules': []},
}


def _aggregate_node_stats(merged_nodes: list) -> dict:
    """Считает агрегаты по уже объединённому списку нод.

    Возвращает:
      total_nodes, online_nodes, disabled_nodes — счётчики
      total_users — суммарно подключённых юзеров на всех нодах
      total_upload_bps, total_download_bps — текущая скорость по всем нодам в bits/sec
      total_bandwidth_bps — TX+RX
    """
    total_nodes = len(merged_nodes)
    online_nodes = 0
    disabled_nodes = 0
    total_users = 0
    total_up_bytes = 0  # bytes/sec
    total_dn_bytes = 0
    metrics_in_up = 0.0
    metrics_in_dn = 0.0
    for n in merged_nodes:
        if n.get('is_disabled'):
            disabled_nodes += 1
        if n.get('is_connected'):
            online_nodes += 1
        cu = n.get('connected_users')
        if cu is None:
            cu = n.get('users_online')
        try:
            total_users += int(cu or 0)
        except (TypeError, ValueError):
            pass
        try:
            total_up_bytes += float(n.get('network_upload') or 0)
        except (TypeError, ValueError):
            pass
        try:
            total_dn_bytes += float(n.get('network_download') or 0)
        except (TypeError, ValueError):
            pass
        try:
            metrics_in_up += float(n.get('metrics_inbound_upload_bytes') or 0)
            metrics_in_dn += float(n.get('metrics_inbound_download_bytes') or 0)
        except (TypeError, ValueError):
            pass
    return {
        'total_nodes': total_nodes,
        'online_nodes': online_nodes,
        'disabled_nodes': disabled_nodes,
        'total_users': total_users,
        'total_upload_bps': total_up_bytes * 8,
        'total_download_bps': total_dn_bytes * 8,
        'total_bandwidth_bps': (total_up_bytes + total_dn_bytes) * 8,
        'metrics_inbound_upload_bytes': metrics_in_up,
        'metrics_inbound_download_bytes': metrics_in_dn,
    }


def _format_uptime(val):
    """Форматирует uptime: секунды (int/str) -> 'Xd Xч' или 'Xч'."""
    if val is None:
        return ''
    try:
        sec = int(float(val))
    except (ValueError, TypeError):
        return str(val) if val else ''
    if sec < 60:
        return f'{sec}с'
    if sec < 3600:
        return f'{sec // 60}м'
    hours = sec / 3600
    if hours < 24:
        return f'{hours:.1f}ч'
    days = int(sec // 86400)
    hrs = int((sec % 86400) // 3600)
    if hrs:
        return f'{days}д {hrs}ч'
    return f'{days}д'


def _apply_realtime_speed_to_merged_nodes(merged_nodes: list, speed_by_uuid: dict) -> None:
    """Подмешивает ``network_upload`` / ``network_download`` (байт/с) с realtime API."""
    if not speed_by_uuid or not merged_nodes:
        return

    norm_speeds = {normalize_remnawave_node_uuid(k): v for k, v in speed_by_uuid.items()}
    for mn in merged_nodes:
        uid = normalize_remnawave_node_uuid(mn.get('uuid'))
        sp = norm_speeds.get(uid)
        if not sp:
            continue
        mn['network_upload'] = float(sp.get('network_upload') or 0)
        mn['network_download'] = float(sp.get('network_download') or 0)


def _routing_text_to_list(text: str | None) -> list[str]:
    return [line.strip() for line in (text or '').splitlines() if line.strip()]


def _routing_list_to_text(items) -> str:
    if not items:
        return ''
    if isinstance(items, str):
        return items
    return '\n'.join(str(x) for x in items if str(x).strip())


def _default_routing_config() -> dict:
    return {
        'route_order': 'block-proxy-direct',
        'udp_block_quic': True,
        'block_sites': [],
        'block_ip': [],
        'proxy_sites': [],
        'proxy_ip': [],
        'direct_sites': [],
        'direct_ip': [],
        'domain_strategy': 'IPIfNonMatch',
    }


def _parse_routing_config_display(raw: str | None) -> dict:
    cfg = _default_routing_config()
    if not raw or not str(raw).strip():
        return cfg
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return cfg
    if not isinstance(data, dict):
        return cfg
    for key in cfg:
        if key in data and data[key] is not None:
            cfg[key] = data[key]
    for list_key in (
        'block_sites', 'block_ip', 'proxy_sites', 'proxy_ip', 'direct_sites', 'direct_ip',
    ):
        val = cfg.get(list_key)
        if isinstance(val, str):
            cfg[list_key] = _routing_text_to_list(val)
        elif isinstance(val, list):
            cfg[list_key] = [str(x).strip() for x in val if str(x).strip()]
        else:
            cfg[list_key] = []
    cfg['udp_block_quic'] = bool(cfg.get('udp_block_quic', True))
    return cfg


def _build_routing_config_json(form) -> str:
    payload = {
        'enabled': 'xray_balancer_routing_enabled' in form,
        'route_order': (form.get('xray_balancer_route_order') or 'block-proxy-direct').strip(),
        'udp_block_quic': 'xray_balancer_udp_block_quic' in form,
        'block_sites': _routing_text_to_list(form.get('xray_balancer_routing_block')),
        'block_ip': _routing_text_to_list(form.get('xray_balancer_routing_block_ip')),
        'proxy_sites': _routing_text_to_list(form.get('xray_balancer_routing_proxy')),
        'proxy_ip': _routing_text_to_list(form.get('xray_balancer_routing_proxy_ip')),
        'direct_sites': _routing_text_to_list(form.get('xray_balancer_routing_direct')),
        'direct_ip': _routing_text_to_list(form.get('xray_balancer_routing_direct_ip')),
        'domain_strategy': (form.get('xray_balancer_domain_strategy') or 'IPIfNonMatch').strip(),
    }
    return json.dumps(payload, ensure_ascii=False)


async def _upsert_setting(key: str, value: str) -> None:
    from web_admin.async_db import async_execute_db

    await async_execute_db(
        'INSERT INTO settings (key, value, description) VALUES (?, ?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, value, key),
    )


def attach_remnawave_routes(admin_bp_instance):
    """Прикрепляет роуты для раздела Remnawave"""

    @admin_bp_instance.route('/remnawave/settings', methods=['GET', 'POST'])
    async def remnawave_settings():
        from web_admin.async_db import async_execute_db, async_query_db

        if request.method == 'POST':
            form = await request.form
            restart_keys = (
                'xray_json_enabled',
                'xray_balancer_enabled',
                'xray_balancer_node_stats_enabled',
                'xray_balancer_routing_enabled',
            )
            prev_rows = await async_query_db(
                f"SELECT key, value FROM settings WHERE key IN ({','.join(['?'] * len(restart_keys))})",
                restart_keys,
            )
            prev_vals = {r['key']: r['value'] for r in (prev_rows or [])}
            restart_needed = False
            for key in _REMNAWAVE_TOGGLE_KEYS:
                val = '1' if key in form else '0'
                if key in restart_keys and prev_vals.get(key, '0') != val:
                    restart_needed = True
                await _upsert_setting(key, val)
            for key in _REMNAWAVE_VALUE_KEYS:
                if key not in form:
                    continue
                value = form.get(key, '')
                await _upsert_setting(key, value)
            routing_json = _build_routing_config_json(form)
            await _upsert_setting('xray_balancer_routing_config', routing_json)
            domain_strategy = (form.get('xray_balancer_domain_strategy') or 'IPIfNonMatch').strip()
            await _upsert_setting('xray_balancer_domain_strategy', domain_strategy)
            try:
                await app_conf.load_settings()
            except Exception as e:
                logger.error(f'[REMNAWAVE] reload app_conf after settings: {e}')
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post('http://127.0.0.1:8081/api/reload-settings')
            except Exception as e:
                logger.warning(f'[REMNAWAVE] bot reload-settings: {e}')
            restart_hint = restart_needed
            if restart_hint:
                await flash(
                    'Настройки Remnawave сохранены. Перезапустите сервис xuiweb: '
                    'sudo systemctl restart xuiweb.service',
                    'success',
                )
            else:
                await flash('Настройки Remnawave сохранены.', 'success')
            return redirect(url_for('admin.remnawave_settings'))

        # Автоинициализация ключей, если ещё не созданы
        _default_settings = [
            ('xray_json_enabled', '0',
             'Добавлять Xray JSON (xrayConfig) в ответ подписки. Каким клиентам — см. переключатели Happ / INCY / v2raytun ниже.'),
            ('xray_json_client_happ', '1',
             'Отдавать JSON подписку клиенту Happ (User-Agent содержит happ).'),
            ('xray_json_client_incy', '1',
             'Отдавать JSON подписку клиенту INCY (User-Agent содержит incy).'),
            ('xray_json_client_v2raytun', '1',
             'Отдавать JSON подписку клиенту v2raytun (User-Agent содержит v2raytun).'),
            ('xray_balancer_enabled', '0',
             'Пересборка Xray JSON: группы стран, AUTO и failover между серверами. '
             'Хосты и их имена настраиваются в Remnawave. Требует JSON-подписку.'),
            ('xray_balancer_node_stats_enabled', '0',
             'Не отдавать в балансировщик серверы на перегруженных или offline-нодах Remnawave.'),
            ('xray_balancer_max_users_per_gb', '50',
             'Порог нагрузки: макс. онлайн-пользователей на 1 GB RAM ноды.'),
            ('xray_balancer_max_users_per_cpu', '80',
             'Порог нагрузки: макс. онлайн-пользователей на 1 CPU ноды.'),
            ('xray_balancer_node_load_threshold', '1.0',
             'Нормализованный load выше этого значения — сервер исключается из подписки.'),
            ('xray_balancer_node_stats_interval_sec', '120',
             'Интервал опроса Remnawave /api/nodes/ (секунды).'),
            ('xray_balancer_routing_enabled', '0',
             'Свои правила routing (block / proxy / direct) вместо шаблона Remnawave. Требует умную балансировку.'),
            ('xray_balancer_routing_config', '{}',
             'JSON: правила routing (block / proxy / direct) для балансировщика xuiweb.'),
            ('xray_balancer_strategy', 'leastLoad',
             'Стратегия Xray balancer: leastLoad, random, roundRobin или leastPing.'),
            ('xray_balancer_mode', 'full',
             'Режим балансировки: full — все группы пересобираются; auto_only — только AUTO и LTE AUTO, остальное от Remnawave.'),
            ('xray_balancer_auto_group_name', '🇪🇺 AUTO | Самые быстрые',
             'Название группы AUTO в Happ (remarks конфига).'),
            ('xray_balancer_lte_auto_group_name', '🇷🇺 LTE AUTO | Самые быстрые',
             'Название группы LTE AUTO — все хосты с LTE в имени.'),
            ('xray_balancer_lte_triggers', 'LTE',
             'LTE-триггеры и baseline: LTE:900, MOBILE:1500 (порядок = приоритет каскада). Без :мс — fallback из полей LTE+/LTE/LTE- ниже.'),
            ('xray_balancer_auto_exclude', '',
             'Исключения из 🇪🇺 AUTO: подстроки в имени хоста (через запятую). Россия 🇷🇺 исключается всегда.'),
            ('xray_balancer_probe_url', 'https://www.gstatic.com/generate_204',
             'URL для burstObservatory ping (проверка доступности серверов).'),
            ('xray_balancer_probe_interval', '3m',
             'Интервал опроса серверов burstObservatory (например 3m, 1m).'),
            ('xray_balancer_probe_sampling', '3',
             'Число проб ping на каждый сервер за интервал.'),
            ('xray_balancer_probe_timeout', '3s',
             'Таймаут одной пробы ping (например 3s).'),
            ('xray_balancer_tolerance', '0.5',
             'Tolerance основного балансера (main): насколько хуже ping допустим без переключения.'),
            ('xray_balancer_tolerance_fallback', '0.8',
             'Tolerance резервных балансеров (LTE, all, одиночная группа).'),
            ('xray_balancer_load_expected', '3',
             'expected: сколько лучших серверов держать в пуле leastLoad/leastPing.'),
            ('xray_balancer_main_lte_baseline_ms', '500',
             'RTT baseline (мс) для main-балансера в группе с LTE-fallback.'),
            ('xray_balancer_single_baseline_ms', '800',
             'RTT baseline (мс) для группы без LTE (одна страна).'),
            ('xray_balancer_lte_baseline_ms', '1500',
             'Fallback baseline (мс) для 2-го и следующих триггеров без :мс в списке LTE-триггеров.'),
            ('xray_balancer_lte_plus_baseline_ms', '900',
             'Fallback baseline (мс) для 1-го триггера без :мс в списке LTE-триггеров.'),
            ('xray_balancer_lte_minus_baseline_ms', '2800',
             'Fallback baseline (мс) для 3-го триггера без :мс в списке LTE-триггеров.'),
            ('xray_balancer_dns_servers', '1.1.1.1,1.0.0.1',
             'DNS-серверы в JSON конфиге балансировщика (через запятую). Перезаписывают servers из шаблона Remnawave.'),
            ('xray_balancer_dns_query_strategy', 'UseIP',
             'dns.queryStrategy в JSON: AsIs, UseIP, UseIPv4 или UseIPv6.'),
            ('xray_balancer_domain_strategy', 'IPIfNonMatch',
             'routing.domainStrategy во всех JSON-конфигах балансировщика (AsIs / IPIfNonMatch / IPOnDemand).'),
            ('sub_link_dedup_enabled', '0',
             'Текстовая подписка: один ключ на «логическое» имя хоста (суффиксы Remnawave … 1/2/3 '
             'и [PC]/[MOBILE] учитываются). Xray JSON не затрагивается.'),
            ('sub_link_dedup_mode', 'random',
             'Режим выбора ключа: random (равный шанс) или online (offline/выше порога — исключить; '
             'среди остальных — случайно, но чаще менее загруженные: вес ∝ порог − usersOnline).'),
            ('sub_link_dedup_online_threshold', '100',
             'Режим online: не выдавать ключ, если usersOnline на привязанной ноде хоста выше порога.'),
            ('sub_link_dedup_online_interval_sec', '60',
             'Режим online: интервал опроса /api/hosts/ и /api/nodes/ в xuiweb (сек).'),
        ]
        for key, val, desc in _default_settings:
            exists = await async_query_db('SELECT 1 FROM settings WHERE key = ?', (key,), one=True)
            if not exists:
                await async_execute_db(
                    'INSERT INTO settings (key, value, description) VALUES (?, ?, ?)',
                    (key, val, desc),
                )

        keys = sorted(_REMNAWAVE_TOGGLE_KEYS | _REMNAWAVE_VALUE_KEYS)
        placeholders = ','.join(['?'] * len(keys))
        rows = await async_query_db(
            f'SELECT key, value, description FROM settings WHERE key IN ({placeholders})',
            tuple(keys),
        )
        by_key = {r['key']: r for r in rows} if rows else {}
        routing_cfg = _parse_routing_config_display(
            (by_key.get('xray_balancer_routing_config') or {}).get('value'),
        )
        if 'xray_balancer_domain_strategy' not in by_key:
            legacy_ds = routing_cfg.get('domain_strategy') or 'IPIfNonMatch'
            await async_execute_db(
                'INSERT INTO settings (key, value, description) VALUES (?, ?, ?)',
                (
                    'xray_balancer_domain_strategy',
                    legacy_ds,
                    'routing.domainStrategy во всех JSON-конфигах балансировщика (AsIs / IPIfNonMatch / IPOnDemand).',
                ),
            )
            by_key['xray_balancer_domain_strategy'] = {
                'key': 'xray_balancer_domain_strategy',
                'value': legacy_ds,
                'description': '',
            }

        connect_page_url = ''
        try:
            row = await async_query_db(
                "SELECT value FROM settings WHERE key = 'connect_page_url'", (), one=True,
            )
            if row and row.get('value'):
                connect_page_url = row['value'].strip().rstrip('/')
        except Exception:
            pass

        return await render_template(
            'remnawave_settings.html',
            settings_by_key=by_key,
            connect_page_url=connect_page_url,
            routing_cfg=routing_cfg,
        )

    @admin_bp_instance.route('/remnawave', methods=['GET'])
    async def remnawave_dashboard():
        """Главная страница Remnawave с общей статистикой и нодами"""
        try:
            # Загружаем настройки из БД перед использованием
            await app_conf.load_settings()
            
            # Проверяем наличие настроек Remnawave
            base_url = app_conf.get('remnawave_base_url', '')
            api_token = app_conf.get('remnawave_api_token', '')
            
            if not base_url or not api_token:
                error_msg = (
                    f"Remnawave не настроен. Откройте «Remnawave → Настройки». "
                    f"Base URL: {'указан' if base_url else 'не указан'}, "
                    f"API Token: {'указан' if api_token else 'не указан'}"
                )
                logger.error(f"[REMNAWAVE] {error_msg}")
                return await render_template(
                    'remnawave_dashboard.html',
                    system_stats=None,
                    nodes_metrics=None,
                    all_nodes=None,
                    error=error_msg
                )
            
            from remnawave_manager import remnawave_manager_instance
            
            # Получаем статистику системы
            system_stats = await remnawave_manager_instance.get_system_stats()
            
            # Получаем метрики нод
            nodes_metrics = await remnawave_manager_instance.get_nodes_metrics()
            
            # Получаем список всех нод
            all_nodes = await remnawave_manager_instance.get_all_nodes()
            
            # Объединяем данные: используем all_nodes как основу и добавляем метрики
            merged_nodes = []
            if all_nodes:
                metrics_dict = {}
                metrics_by_name = {}
                if nodes_metrics and nodes_metrics.get('nodes'):
                    for metric_node in nodes_metrics['nodes']:
                        metric_uuid = normalize_remnawave_node_uuid(
                            str(metric_node.get('uuid') or metric_node.get('nodeUuid') or '')
                        )
                        if metric_uuid:
                            metrics_dict[metric_uuid] = metric_node
                        if metric_node.get('name'):
                            metrics_by_name[metric_node.get('name')] = metric_node
                        logger.debug(f"[REMNAWAVE] Метрика: UUID={metric_uuid}, CPU={metric_node.get('cpu_usage')}, uptime={metric_node.get('uptime')}")
                    logger.info(f"[REMNAWAVE] Загружено {len(nodes_metrics['nodes'])} метрик нод")
                else:
                    logger.warning(f"[REMNAWAVE] Метрики нод не получены или пусты. nodes_metrics: {nodes_metrics}")
                
                logger.info(f"[REMNAWAVE] Всего нод для объединения: {len(all_nodes)}")
                for node in all_nodes:
                    # Нормализуем UUID для сравнения
                    node_uuid = normalize_remnawave_node_uuid(node.get('uuid'))
                    merged_node = node.copy()
                    
                    logger.debug(f"[REMNAWAVE] Обработка ноды: name={node.get('name', 'Unknown')}, UUID={node_uuid}")
                    
                    # Добавляем метрики: сначала по UUID, затем по имени
                    metric_data = None
                    if node_uuid and node_uuid in metrics_dict:
                        metric_data = metrics_dict[node_uuid]
                    elif node.get('name') and node.get('name') in metrics_by_name:
                        metric_data = metrics_by_name[node.get('name')]
                    if metric_data:
                        # Поддержка snake_case и camelCase (API может возвращать оба варианта)
                        def _m(k, ck):
                            return metric_data.get(k) if metric_data.get(k) is not None else metric_data.get(ck)
                        metric_updates = {
                            'cpu_usage': _m('cpu_usage', 'cpuUsage'),
                            'memory_usage': _m('memory_usage', 'memoryUsage'),
                            'network_upload': _m('network_upload', 'networkUpload'),
                            'network_download': _m('network_download', 'networkDownload'),
                            'last_seen': metric_data.get('last_seen') or metric_data.get('lastSeen'),
                            'upload': metric_data.get('upload'),
                            'download': metric_data.get('download'),
                            'metrics_inbound_upload_bytes': metric_data.get('metrics_inbound_upload_bytes'),
                            'metrics_inbound_download_bytes': metric_data.get('metrics_inbound_download_bytes'),
                            'metrics_outbound_upload_bytes': metric_data.get('metrics_outbound_upload_bytes'),
                            'metrics_outbound_download_bytes': metric_data.get('metrics_outbound_download_bytes'),
                        }
                        conn_users = _m('connected_users', 'connectedUsers')
                        if conn_users is not None:
                            metric_updates['connected_users'] = int(conn_users)
                        if metric_data.get('uptime') is not None:
                            metric_updates['uptime'] = metric_data.get('uptime')
                        # Не перетираем системные метрики из /nodes значениями None
                        # (в 2.7.x /system/nodes/metrics не содержит cpu/ram/network).
                        merged_node.update({k: v for k, v in metric_updates.items() if v is not None})
                        if merged_node.get('connected_users') is None:
                            merged_node['connected_users'] = int(merged_node.get('users_online') or 0)
                        if merged_node.get('is_connected') is None:
                            merged_node['is_connected'] = metric_data.get('is_online', False)
                    else:
                        # Fallback: используем данные из nodes API (xray_uptime, users_online)
                        raw_cu = merged_node.get('connected_users') or merged_node.get('users_online') or 0
                        merged_node['connected_users'] = int(raw_cu)
                        logger.debug(f"[REMNAWAVE] Метрики не найдены для {node.get('name')} (UUID: {node_uuid}), используем данные nodes")
                    
                    # Приводим users_online к int (NodeMetric в 2.7.0 возвращает float)
                    if merged_node.get('users_online') is not None:
                        merged_node['users_online'] = int(merged_node['users_online'])

                    # uptime_display — форматированная строка для отображения (без Jinja-фильтра)
                    uptime_val = merged_node.get('uptime')
                    xray_val = merged_node.get('xray_uptime')
                    if uptime_val is not None:
                        merged_node['uptime_display'] = _format_uptime(uptime_val)
                    elif xray_val:
                        fmt = _format_uptime(xray_val)
                        merged_node['uptime_display'] = fmt if fmt else str(xray_val)
                    else:
                        merged_node['uptime_display'] = ''
                    
                    merged_nodes.append(merged_node)
                
                speed_map = await remnawave_manager_instance.get_nodes_realtime_speed_by_uuid()
                _apply_realtime_speed_to_merged_nodes(merged_nodes, speed_map)

            merged_nodes = _sort_nodes_by_renewal(merged_nodes)
            agg = _aggregate_node_stats(merged_nodes)
            return await render_template(
                'remnawave_dashboard.html',
                system_stats=system_stats,
                nodes=merged_nodes,
                agg=agg,
            )
        except ValueError as e:
            # Ошибка инициализации Remnawave (нет настроек)
            error_msg = str(e)
            logger.error(f"[REMNAWAVE] {error_msg}")
            return await render_template(
                'remnawave_dashboard.html',
                system_stats=None,
                nodes_metrics=None,
                all_nodes=None,
                error=error_msg
            )
        except Exception as e:
            logger.error(f"Ошибка при загрузке страницы Remnawave: {e}")
            import traceback
            traceback.print_exc()
            return await render_template(
                'remnawave_dashboard.html',
                system_stats=None,
                nodes_metrics=None,
                all_nodes=None,
                error=str(e)
            )
    
    @admin_bp_instance.route('/api/remnawave/stats', methods=['GET'])
    async def remnawave_stats_api():
        """API endpoint для получения статистики Remnawave"""
        try:
            # Загружаем настройки из БД перед использованием
            await app_conf.load_settings()
            
            from remnawave_manager import remnawave_manager_instance
            
            system_stats = await remnawave_manager_instance.get_system_stats()
            nodes_metrics = await remnawave_manager_instance.get_nodes_metrics()
            all_nodes = await remnawave_manager_instance.get_all_nodes()
            
            # Объединяем данные: используем all_nodes как основу и добавляем метрики
            merged_nodes = []
            if all_nodes:
                metrics_dict = {}
                metrics_by_name = {}
                if nodes_metrics and nodes_metrics.get('nodes'):
                    for metric_node in nodes_metrics['nodes']:
                        metric_uuid = normalize_remnawave_node_uuid(
                            str(metric_node.get('uuid') or metric_node.get('nodeUuid') or '')
                        )
                        if metric_uuid:
                            metrics_dict[metric_uuid] = metric_node
                        if metric_node.get('name'):
                            metrics_by_name[metric_node.get('name')] = metric_node
                
                for node in all_nodes:
                    node_uuid = normalize_remnawave_node_uuid(node.get('uuid'))
                    merged_node = node.copy()
                    
                    metric_data = None
                    if node_uuid and node_uuid in metrics_dict:
                        metric_data = metrics_dict[node_uuid]
                    elif node.get('name') and node.get('name') in metrics_by_name:
                        metric_data = metrics_by_name[node.get('name')]
                    
                    if metric_data:
                        def _m(k, ck):
                            return metric_data.get(k) if metric_data.get(k) is not None else metric_data.get(ck)
                        metric_updates = {
                            'cpu_usage': _m('cpu_usage', 'cpuUsage'),
                            'memory_usage': _m('memory_usage', 'memoryUsage'),
                            'network_upload': _m('network_upload', 'networkUpload'),
                            'network_download': _m('network_download', 'networkDownload'),
                            'last_seen': metric_data.get('last_seen') or metric_data.get('lastSeen'),
                            'upload': metric_data.get('upload'),
                            'download': metric_data.get('download'),
                            'metrics_inbound_upload_bytes': metric_data.get('metrics_inbound_upload_bytes'),
                            'metrics_inbound_download_bytes': metric_data.get('metrics_inbound_download_bytes'),
                            'metrics_outbound_upload_bytes': metric_data.get('metrics_outbound_upload_bytes'),
                            'metrics_outbound_download_bytes': metric_data.get('metrics_outbound_download_bytes'),
                        }
                        conn_users = _m('connected_users', 'connectedUsers')
                        if conn_users is not None:
                            metric_updates['connected_users'] = int(conn_users)
                        if metric_data.get('uptime') is not None:
                            metric_updates['uptime'] = metric_data.get('uptime')
                        # Не перетираем системные метрики из /nodes значениями None
                        # (в 2.7.x /system/nodes/metrics не содержит cpu/ram/network).
                        merged_node.update({k: v for k, v in metric_updates.items() if v is not None})
                        if merged_node.get('connected_users') is None:
                            merged_node['connected_users'] = int(merged_node.get('users_online') or 0)
                        if merged_node.get('is_connected') is None:
                            merged_node['is_connected'] = metric_data.get('is_online', False)
                    else:
                        raw_cu = merged_node.get('connected_users') or merged_node.get('users_online') or 0
                        merged_node['connected_users'] = int(raw_cu)

                    if merged_node.get('users_online') is not None:
                        merged_node['users_online'] = int(merged_node['users_online'])

                    # uptime_display для JS (при обновлении через API)
                    uptime_val = merged_node.get('uptime')
                    xray_val = merged_node.get('xray_uptime')
                    if uptime_val is not None:
                        merged_node['uptime_display'] = _format_uptime(uptime_val)
                    elif xray_val:
                        fmt = _format_uptime(xray_val)
                        merged_node['uptime_display'] = fmt if fmt else str(xray_val)
                    else:
                        merged_node['uptime_display'] = ''
                    
                    merged_nodes.append(merged_node)
                
                speed_map = await remnawave_manager_instance.get_nodes_realtime_speed_by_uuid()
                _apply_realtime_speed_to_merged_nodes(merged_nodes, speed_map)

            merged_nodes = _sort_nodes_by_renewal(merged_nodes)
            agg = _aggregate_node_stats(merged_nodes)
            return jsonify({
                'ok': True,
                'system_stats': system_stats,
                'nodes': merged_nodes,
                'agg': agg,
            })
        except Exception as e:
            logger.error(f"Ошибка при получении статистики Remnawave: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({
                'ok': False,
                'error': str(e)
            }), 500
    
    @admin_bp_instance.route('/api/remnawave/nodes', methods=['GET'])
    async def remnawave_nodes_api():
        """API endpoint для получения списка нод"""
        try:
            # Загружаем настройки из БД перед использованием
            await app_conf.load_settings()
            
            from remnawave_manager import remnawave_manager_instance
            
            all_nodes = await remnawave_manager_instance.get_all_nodes()
            
            return jsonify({
                'ok': True,
                'nodes': all_nodes
            })
        except Exception as e:
            logger.error(f"Ошибка при получении списка нод: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({
                'ok': False,
                'error': str(e)
            }), 500

    # ---- Действия с нодами ---------------------------------------------------
    # Все возвращают { ok: bool, error?: str }. Фронт после успешного ответа
    # перезапрашивает /api/remnawave/stats, чтобы UI отрисовал актуальное состояние.

    async def _node_action(action_callable, *args):
        """Универсальный обёрточный хелпер: вызывает менеджер, форматирует ответ."""
        try:
            await app_conf.load_settings()
            from remnawave_manager import remnawave_manager_instance
            ok, err = await action_callable(remnawave_manager_instance, *args)
            if ok:
                return jsonify({'ok': True}), 200
            return jsonify({'ok': False, 'error': err or 'Неизвестная ошибка'}), 502
        except Exception as e:
            logger.error(f"Remnawave node action error: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'ok': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/api/remnawave/nodes/<uuid>/restart', methods=['POST'])
    async def remnawave_node_restart(uuid: str):
        return await _node_action(lambda m, u: m.restart_node(u), uuid)

    @admin_bp_instance.route('/api/remnawave/nodes/<uuid>/enable', methods=['POST'])
    async def remnawave_node_enable(uuid: str):
        return await _node_action(lambda m, u: m.enable_node(u), uuid)

    @admin_bp_instance.route('/api/remnawave/nodes/<uuid>/disable', methods=['POST'])
    async def remnawave_node_disable(uuid: str):
        return await _node_action(lambda m, u: m.disable_node(u), uuid)

    @admin_bp_instance.route('/api/remnawave/nodes/<uuid>/renewal-date', methods=['POST'])
    async def remnawave_node_renewal_date(uuid: str):
        """Обновить дату продления во внутреннем имени ноды ([DD.MM.YYYY])."""
        try:
            payload = await request.get_json(silent=True) or {}
        except Exception:
            payload = {}

        date_raw = (payload.get('date') or '').strip()
        if not date_raw:
            return jsonify({'ok': False, 'error': 'Укажите date (YYYY-MM-DD)'}), 400

        try:
            renewal = date.fromisoformat(date_raw[:10])
        except ValueError:
            return jsonify({'ok': False, 'error': 'Неверный формат date, нужен YYYY-MM-DD'}), 400

        current_name = (payload.get('name') or '').strip()

        try:
            await app_conf.load_settings()
            from remnawave_manager import remnawave_manager_instance

            if not current_name:
                one = await remnawave_manager_instance.get_node_by_uuid(uuid)
                if not one:
                    return jsonify({'ok': False, 'error': 'Нода не найдена'}), 404
                current_name = one.get('name') or ''

            new_name = _apply_renewal_date_to_node_name(current_name, renewal)
            if len(new_name) > _REMNAWAVE_NODE_NAME_MAX_LEN:
                return jsonify({
                    'ok': False,
                    'error': (
                        f'Имя после изменения ({len(new_name)} симв.) длиннее лимита Remnawave '
                        f'({_REMNAWAVE_NODE_NAME_MAX_LEN}). Сократите название ноды.'
                    ),
                }), 400

            ok, err = await remnawave_manager_instance.update_node_name(uuid, new_name)
            if not ok:
                return jsonify({'ok': False, 'error': err or 'Не удалось обновить имя'}), 502

            return jsonify({'ok': True, **_renewal_fields_from_name(new_name)}), 200
        except Exception as e:
            logger.error(f"Remnawave renewal-date error: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'ok': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/api/remnawave/nodes/restart-all', methods=['POST'])
    async def remnawave_nodes_restart_all():
        # body: { "force_restart": true|false }
        try:
            payload = await request.get_json(silent=True) or {}
        except Exception:
            payload = {}
        force = bool(payload.get('force_restart', False))
        try:
            await app_conf.load_settings()
            from remnawave_manager import remnawave_manager_instance
            ok, err = await remnawave_manager_instance.restart_all_nodes(force_restart=force)
            if ok:
                return jsonify({'ok': True}), 200
            return jsonify({'ok': False, 'error': err or 'Неизвестная ошибка'}), 502
        except Exception as e:
            logger.error(f"Remnawave restart-all error: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ---- Карточка клиента Remnawave (для user_details) -----------------------

    @admin_bp_instance.route('/api/remnawave/user/<int:telegram_id>/card', methods=['GET'])
    async def remnawave_user_card(telegram_id: int):
        """Общая инфа клиента + трафик по нодам (всё время и за сутки)."""
        try:
            await app_conf.load_settings()
            from remnawave_manager import remnawave_manager_instance
            from datetime import datetime, timezone

            card = await remnawave_manager_instance.get_user_card(telegram_id)
            if not card or not card.get('uuid'):
                return jsonify({'ok': False, 'error': 'not_found'}), 404

            uuid = str(card['uuid'])
            now = datetime.now(timezone.utc)
            end_iso = now.strftime('%Y-%m-%dT%H:%M:%S.000Z')
            today_iso = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            alltime_iso = '2020-01-01T00:00:00.000Z'

            nodes_total = await remnawave_manager_instance.get_user_node_usage(uuid, alltime_iso, end_iso)
            nodes_today = await remnawave_manager_instance.get_user_node_usage(uuid, today_iso, end_iso)

            return jsonify({
                'ok': True,
                'card': card,
                'nodes_total': nodes_total,
                'nodes_today': nodes_today,
                'sum_total': sum(int(n.get('total') or 0) for n in nodes_total),
                'sum_today': sum(int(n.get('total') or 0) for n in nodes_today),
            })
        except Exception as e:
            logger.error(f"remnawave_user_card error: {type(e).__name__}: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/api/remnawave/user/<int:telegram_id>/ips', methods=['GET'])
    async def remnawave_user_ips(telegram_id: int):
        """Активные IP-сессии клиента по нодам (ip-control fetch-ips, джоба+поллинг)."""
        try:
            await app_conf.load_settings()
            from remnawave_manager import remnawave_manager_instance

            card = await remnawave_manager_instance.get_user_card(telegram_id)
            if not card or not card.get('uuid'):
                return jsonify({'ok': False, 'error': 'not_found'}), 404

            nodes = await remnawave_manager_instance.fetch_user_ips(str(card['uuid']))
            if nodes is None:
                return jsonify({'ok': False, 'error': 'pending'}), 202
            return jsonify({'ok': True, 'nodes': nodes})
        except Exception as e:
            logger.error(f"remnawave_user_ips error: {type(e).__name__}: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ---- Config profiles (GUI-редактор) ----------------------------------------

    @admin_bp_instance.route('/remnawave/profiles', methods=['GET'])
    async def remnawave_profiles_list():
        await app_conf.load_settings()
        from remnawave_manager import remnawave_manager_instance

        error = None
        profiles = []
        base_url = app_conf.get('remnawave_base_url', '')
        api_token = app_conf.get('remnawave_api_token', '')
        if not base_url or not api_token:
            error = 'Укажите Remnawave Base URL и API Token в настройках.'
        else:
            loaded = await remnawave_manager_instance.get_config_profiles_full()
            if loaded is None:
                error = 'Не удалось загрузить профили из Remnawave.'
            else:
                profiles = loaded

        return await render_template(
            'remnawave_profiles.html',
            profiles=profiles,
            error=error,
        )

    @admin_bp_instance.route('/remnawave/profiles/new', methods=['GET'])
    async def remnawave_profile_new():
        await app_conf.load_settings()
        import json as _json
        return await render_template(
            'remnawave_profile_edit.html',
            mode='new',
            profile_uuid='',
            profile_name='',
            profile_config_json=_json.dumps(DEFAULT_RW_PROFILE_CONFIG, ensure_ascii=False, indent=2),
            error=None,
        )

    @admin_bp_instance.route('/remnawave/profiles/<profile_uuid>/edit', methods=['GET'])
    async def remnawave_profile_edit(profile_uuid: str):
        await app_conf.load_settings()
        import json as _json
        from remnawave_manager import remnawave_manager_instance

        profile = await remnawave_manager_instance.get_config_profile(profile_uuid)
        if not profile:
            await flash('Профиль не найден.', 'danger')
            return redirect(url_for('admin.remnawave_profiles_list'))

        cfg = profile.get('config') or DEFAULT_RW_PROFILE_CONFIG
        return await render_template(
            'remnawave_profile_edit.html',
            mode='edit',
            profile_uuid=profile.get('uuid') or profile_uuid,
            profile_name=profile.get('name') or '',
            profile_config_json=_json.dumps(cfg, ensure_ascii=False, indent=2),
            profile_meta=profile,
            error=None,
        )

    @admin_bp_instance.route('/api/remnawave/profiles', methods=['POST'])
    async def remnawave_profile_create_api():
        try:
            await app_conf.load_settings()
            from remnawave_manager import remnawave_manager_instance

            data = await request.get_json(silent=True) or {}
            name = (data.get('name') or '').strip()
            config = data.get('config')
            if not name:
                return jsonify({'ok': False, 'error': 'Укажите имя профиля'}), 400
            if not isinstance(config, dict):
                return jsonify({'ok': False, 'error': 'config должен быть объектом JSON'}), 400

            created, err = await remnawave_manager_instance.create_config_profile(name, config)
            if not created:
                return jsonify({'ok': False, 'error': err or 'Ошибка создания'}), 502
            return jsonify({'ok': True, 'profile': created})
        except Exception as e:
            logger.error(f"remnawave_profile_create_api: {type(e).__name__}: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/api/remnawave/profiles/<profile_uuid>', methods=['GET', 'PUT', 'DELETE'])
    async def remnawave_profile_api(profile_uuid: str):
        try:
            await app_conf.load_settings()
            from remnawave_manager import remnawave_manager_instance

            if request.method == 'GET':
                profile = await remnawave_manager_instance.get_config_profile(profile_uuid)
                if not profile:
                    return jsonify({'ok': False, 'error': 'not_found'}), 404
                return jsonify({'ok': True, 'profile': profile})

            if request.method == 'DELETE':
                ok, err = await remnawave_manager_instance.delete_config_profile(profile_uuid)
                if not ok:
                    return jsonify({'ok': False, 'error': err or 'Ошибка удаления'}), 502
                return jsonify({'ok': True})

            data = await request.get_json(silent=True) or {}
            name = data.get('name')
            config = data.get('config')
            if name is not None:
                name = str(name).strip()
                if not name:
                    return jsonify({'ok': False, 'error': 'Имя не может быть пустым'}), 400
            if config is not None and not isinstance(config, dict):
                return jsonify({'ok': False, 'error': 'config должен быть объектом JSON'}), 400

            updated, err = await remnawave_manager_instance.update_config_profile(
                profile_uuid,
                name=name,
                config=config,
            )
            if not updated:
                return jsonify({'ok': False, 'error': err or 'Ошибка обновления'}), 502
            return jsonify({'ok': True, 'profile': updated})
        except Exception as e:
            logger.error(f"remnawave_profile_api({profile_uuid}): {type(e).__name__}: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/api/remnawave/profiles/x25519', methods=['POST'])
    async def remnawave_profile_x25519_api():
        """Генерация пары ключей Reality через Remnawave API."""
        try:
            await app_conf.load_settings()
            from remnawave_manager import remnawave_manager_instance

            priv, pub, err = await remnawave_manager_instance.generate_x25519_keypair()
            if not priv:
                return jsonify({'ok': False, 'error': err or 'Не удалось сгенерировать ключи'}), 502
            return jsonify({'ok': True, 'privateKey': priv, 'publicKey': pub})
        except Exception as e:
            logger.error(f"remnawave_profile_x25519_api: {type(e).__name__}: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ---- SSH-установка Remnawave Node ----------------------------------------

    @admin_bp_instance.route('/api/remnawave/config-profiles', methods=['GET'])
    async def remnawave_config_profiles_api():
        """Список config profiles + inbounds для формы установки ноды."""
        try:
            await app_conf.load_settings()
            from remnawave_manager import remnawave_manager_instance

            profiles = await remnawave_manager_instance.get_config_profiles_list()
            if profiles is None:
                return jsonify({'ok': False, 'error': 'Не удалось загрузить профили'}), 502
            return jsonify({'ok': True, 'profiles': profiles})
        except Exception as e:
            logger.error(f"remnawave_config_profiles_api: {type(e).__name__}: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/api/remnawave/nodes/install-via-ssh', methods=['POST'])
    async def remnawave_node_install_via_ssh():
        """Запуск фоновой установки Remnawave Node по SSH."""
        import asyncio
        import uuid as uuid_mod

        try:
            from web_admin.core.rw_node_ssh_installer import rw_node_ssh_installer

            data = await request.get_json(silent=True) or {}
            host = (data.get('host') or data.get('ssh_host') or '').strip()
            ssh_port = int(data.get('ssh_port') or 22)
            ssh_username = (data.get('ssh_username') or 'root').strip()
            ssh_password = (data.get('ssh_password') or '').strip() or None
            ssh_private_key = (data.get('ssh_private_key') or '').strip() or None
            auth_method = (data.get('ssh_auth_method') or 'password').strip().lower()

            node_name = (data.get('node_name') or '').strip()
            node_address = (data.get('node_address') or host).strip()
            node_port = int(data.get('node_port') or 2222)
            country_code = (data.get('country_code') or 'XX').strip().upper()[:2]
            config_profile_uuid = (data.get('config_profile_uuid') or '').strip()
            inbound_uuids = data.get('inbound_uuids') or []

            if not host:
                return jsonify({'ok': False, 'error': 'Укажите SSH host'}), 400
            if not node_name or len(node_name) < 3:
                return jsonify({'ok': False, 'error': 'Внутреннее имя: минимум 3 символа'}), 400
            if len(node_name) > 30:
                return jsonify({'ok': False, 'error': 'Внутреннее имя: максимум 30 символов'}), 400
            if not node_address:
                return jsonify({'ok': False, 'error': 'Укажите адрес ноды для панели'}), 400
            if not config_profile_uuid:
                return jsonify({'ok': False, 'error': 'Выберите config profile'}), 400
            if not isinstance(inbound_uuids, list) or not inbound_uuids:
                return jsonify({'ok': False, 'error': 'Выберите хотя бы один inbound'}), 400

            if auth_method == 'key':
                if not ssh_private_key:
                    return jsonify({'ok': False, 'error': 'Вставьте приватный SSH-ключ (PEM)'}), 400
            elif not ssh_password:
                return jsonify({'ok': False, 'error': 'Укажите SSH пароль'}), 400

            await app_conf.load_settings()
            if not app_conf.get('remnawave_base_url') or not app_conf.get('remnawave_api_token'):
                return jsonify({'ok': False, 'error': 'Remnawave не настроен'}), 400

            task_id = str(uuid_mod.uuid4())
            asyncio.create_task(
                rw_node_ssh_installer.install_remnanode(
                    task_id,
                    host=host,
                    ssh_port=ssh_port,
                    ssh_username=ssh_username,
                    ssh_password=ssh_password,
                    ssh_private_key=ssh_private_key if auth_method == 'key' else None,
                    node_name=node_name,
                    node_address=node_address,
                    node_port=node_port,
                    country_code=country_code,
                    config_profile_uuid=config_profile_uuid,
                    inbound_uuids=[str(u) for u in inbound_uuids],
                )
            )
            return jsonify({'ok': True, 'task_id': task_id, 'message': 'Установка запущена'})
        except Exception as e:
            logger.error(f"remnawave_node_install_via_ssh: {type(e).__name__}: {e}", exc_info=True)
            return jsonify({'ok': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/api/remnawave/nodes/install-status/<task_id>', methods=['GET'])
    async def remnawave_node_install_status(task_id: str):
        """Статус SSH-установки Remnawave Node."""
        try:
            from web_admin.core.rw_node_ssh_installer import rw_node_ssh_installer

            status = rw_node_ssh_installer.get_task_status(task_id)
            if not status:
                return jsonify({'ok': False, 'error': 'Задача не найдена'}), 404
            return jsonify({
                'ok': True,
                'status': status.get('status', 'unknown'),
                'progress': status.get('progress', 0),
                'message': status.get('message', ''),
                'logs': status.get('logs', []),
                'error': status.get('error'),
                'result': status.get('result'),
            })
        except Exception as e:
            logger.error(f"remnawave_node_install_status: {type(e).__name__}: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ---- Балансировка текстовых ключей (хосты) --------------------------------

    def _require_host_balancer_enabled():
        if app_conf.get('sub_link_dedup_enabled', '0') not in ('1', 'true', 'yes', 'on'):
            return jsonify({'ok': False, 'error': 'Включите «Балансировка текстовых ключей» в настройках Remnawave'}), 403
        return None

    @admin_bp_instance.route('/remnawave/host-balancer', methods=['GET'])
    async def remnawave_host_balancer_page():
        await app_conf.load_settings()
        if app_conf.get('sub_link_dedup_enabled', '0') not in ('1', 'true', 'yes', 'on'):
            await flash('Сначала включите «Балансировка текстовых ключей» в настройках Remnawave.', 'warning')
            return redirect(url_for('admin.remnawave_settings'))
        dedup_mode = app_conf.get('sub_link_dedup_mode', 'random') or 'random'
        try:
            threshold = int(app_conf.get('sub_link_dedup_online_threshold', '100') or 100)
        except (TypeError, ValueError):
            threshold = 100
        return await render_template(
            'remnawave_host_balancer.html',
            dedup_mode=dedup_mode.strip().lower(),
            online_threshold=threshold,
            error=None,
        )

    @admin_bp_instance.route('/api/remnawave/host-balancer/groups', methods=['GET'])
    async def remnawave_host_balancer_groups_api():
        await app_conf.load_settings()
        blocked = _require_host_balancer_enabled()
        if blocked:
            return blocked
        try:
            from remnawave_manager import remnawave_manager_instance
            from web_admin.core.remnawave_host_balancer import build_host_balancer_groups

            hosts, hosts_err = await remnawave_manager_instance.get_all_hosts()
            if hosts is None:
                return jsonify({
                    'ok': False,
                    'error': hosts_err or 'Не удалось загрузить хосты Remnawave',
                }), 502
            squads = await remnawave_manager_instance.get_internal_squads() or []
            nodes = await remnawave_manager_instance.get_all_nodes() or []
            squad_filter = (request.args.get('squad') or '').strip() or None
            platform_filter = (request.args.get('platform') or '').strip() or None
            view = (request.args.get('view') or 'all').strip().lower()
            if view not in ('all', 'pools', 'singles'):
                view = 'all'
            try:
                threshold = int(app_conf.get('sub_link_dedup_online_threshold', '100') or 100)
            except (TypeError, ValueError):
                threshold = 100

            payload = build_host_balancer_groups(
                hosts,
                squads=squads,
                nodes=nodes,
                squad_filter=squad_filter,
                platform_filter=platform_filter if platform_filter != 'all' else None,
                view=view,
                online_threshold=threshold,
            )
            return jsonify({
                'ok': True,
                'dedup_mode': (app_conf.get('sub_link_dedup_mode', 'random') or 'random').strip().lower(),
                'online_threshold': threshold,
                'squads': squads,
                'nodes': [{
                    'uuid': n.get('uuid'),
                    'name': n.get('name'),
                    'address': n.get('address'),
                    'port': n.get('port'),
                    'is_connected': n.get('is_connected'),
                } for n in nodes],
                **payload,
            })
        except Exception as e:
            logger.error(f"remnawave_host_balancer_groups_api: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'ok': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/api/remnawave/host-balancer/init-pool', methods=['POST'])
    async def remnawave_host_balancer_init_pool_api():
        await app_conf.load_settings()
        blocked = _require_host_balancer_enabled()
        if blocked:
            return blocked
        data = await request.get_json(silent=True) or {}
        host_uuid = (data.get('host_uuid') or '').strip()
        if not host_uuid:
            return jsonify({'ok': False, 'error': 'Укажите host_uuid'}), 400
        try:
            from remnawave_manager import remnawave_manager_instance
            updated, err = await remnawave_manager_instance.init_balancer_pool(host_uuid)
            if not updated:
                return jsonify({'ok': False, 'error': err or 'Ошибка'}), 502
            return jsonify({'ok': True, 'host': updated})
        except Exception as e:
            logger.error(f"remnawave_host_balancer_init_pool_api: {type(e).__name__}: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/api/remnawave/host-balancer/add-member', methods=['POST'])
    async def remnawave_host_balancer_add_member_api():
        await app_conf.load_settings()
        blocked = _require_host_balancer_enabled()
        if blocked:
            return blocked
        data = await request.get_json(silent=True) or {}
        pool_uuid = (data.get('pool_uuid') or data.get('template_uuid') or '').strip()
        mode = (data.get('mode') or 'new').strip().lower()
        source_uuid = (data.get('source_uuid') or '').strip() or None
        address = (data.get('address') or '').strip() or None
        port_raw = data.get('port')
        port = int(port_raw) if port_raw not in (None, '') else None
        node_uuid = (data.get('node_uuid') or '').strip() or None
        clear_nodes = bool(data.get('clear_nodes'))
        if not pool_uuid:
            return jsonify({'ok': False, 'error': 'Укажите pool_uuid'}), 400
        if mode == 'duplicate' and not source_uuid:
            return jsonify({'ok': False, 'error': 'Выберите хост для дублирования'}), 400
        if mode == 'from_node' and not node_uuid:
            return jsonify({'ok': False, 'error': 'Выберите ноду'}), 400
        if mode == 'new' and not address:
            return jsonify({'ok': False, 'error': 'Укажите address'}), 400
        pool_remarks = data.get('pool_remarks')
        if pool_remarks is not None and not isinstance(pool_remarks, list):
            pool_remarks = None
        try:
            from remnawave_manager import remnawave_manager_instance
            created, err = await remnawave_manager_instance.add_balancer_pool_member(
                pool_uuid,
                mode=mode,
                source_uuid=source_uuid,
                address=address,
                port=port,
                node_uuid=node_uuid,
                clear_nodes=clear_nodes,
                pool_remarks=pool_remarks,
            )
            if not created:
                return jsonify({'ok': False, 'error': err or 'Ошибка создания'}), 502
            return jsonify({'ok': True, 'host': created})
        except Exception as e:
            logger.error(f"remnawave_host_balancer_add_member_api: {type(e).__name__}: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    @admin_bp_instance.route('/api/remnawave/host-balancer/members/<host_uuid>', methods=['PATCH', 'DELETE'])
    async def remnawave_host_balancer_member_api(host_uuid: str):
        await app_conf.load_settings()
        blocked = _require_host_balancer_enabled()
        if blocked:
            return blocked
        try:
            from remnawave_manager import remnawave_manager_instance
            if request.method == 'DELETE':
                ok, err = await remnawave_manager_instance.delete_host(host_uuid)
                if not ok:
                    return jsonify({'ok': False, 'error': err or 'Ошибка удаления'}), 502
                return jsonify({'ok': True})
            data = await request.get_json(silent=True) or {}
            updated, err = await remnawave_manager_instance.update_host_fields(
                host_uuid,
                remark=data.get('remark'),
                address=(data.get('address') or '').strip() or None,
                port=int(data['port']) if data.get('port') not in (None, '') else None,
                node_uuid=(data.get('node_uuid') or '').strip() or None,
                is_disabled=data.get('is_disabled') if 'is_disabled' in data else None,
                clear_nodes=bool(data.get('clear_nodes')),
            )
            if not updated:
                return jsonify({'ok': False, 'error': err or 'Ошибка обновления'}), 502
            return jsonify({'ok': True, 'host': updated})
        except Exception as e:
            logger.error(f"remnawave_host_balancer_member_api: {type(e).__name__}: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

