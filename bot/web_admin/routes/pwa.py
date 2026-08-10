"""
PWA-роуты: web app manifest, service worker и offline-fallback.

Все три эндпоинта прицеплены к admin_bp, у которого url_prefix = '/{secret}'.
Это даёт нам бесплатно нужные свойства:

  • manifest и SW отдаются по тому же origin/scope, что и сама админка;
  • Service Worker scope = '/{secret}/' — PWA «живёт» только в админке,
    не лезет в bot-API/subpage/xuiweb;
  • при смене секретного пути PWA становится «битой» (start_url ведёт
    в никуда) — пользователь должен переустановить ярлык, это нормально.

Аутентификация для этих трёх эндпоинтов отключена в _admin_auth_guard
(добавлены в allowed-set), потому что:
  • SW обязан быть доступен без cookies (браузер регистрирует его до логина);
  • manifest браузер тянет один раз при установке без сессии;
  • offline.html — статическая заглушка без чувствительной информации.

Сам секретный префикс уже защищает их от анонимного интернета.
"""
from quart import Response, current_app, request, url_for


# Версия кэша SW. При деплое НОВОЙ версии бампайте здесь — тогда старый
# Service Worker удалит свой кэш и подхватит свежие CSS/JS.
PWA_CACHE_VERSION = 'v3.9.5'

# Дефолт на случай, если в БД ещё не заполнен project_name.
PWA_APP_NAME_FALLBACK = 'Панель'

# Жёсткий «потолок» для short_name. По спеку рекомендуется до ~12 символов,
# иначе Android-launcher всё равно сам обрежет.
_SHORT_NAME_MAX = 12


def _get_project_name() -> str:
    """Берём project_name из конфига приложения; пусто — fallback."""
    try:
        name = (current_app.config.get('PROJECT_NAME') or '').strip()
    except Exception:
        name = ''
    return name or PWA_APP_NAME_FALLBACK


def _make_short_name(name: str) -> str:
    """Короткое имя для иконки на хоумскрине. Просто аккуратно обрезаем
    по словам, не уходя в многоточия."""
    n = (name or '').strip()
    if not n:
        return PWA_APP_NAME_FALLBACK
    if len(n) <= _SHORT_NAME_MAX:
        return n
    # Пытаемся обрезать по последнему пробелу, чтобы не разрывать слово.
    cut = n[:_SHORT_NAME_MAX].rstrip()
    sp = cut.rfind(' ')
    if sp >= 4:
        cut = cut[:sp].rstrip()
    return cut


def attach_pwa_routes(admin_bp_instance):

    # ─────────────────────────────────────────────────────────────
    #  Web App Manifest
    # ─────────────────────────────────────────────────────────────
    @admin_bp_instance.route('/manifest.webmanifest', methods=['GET'])
    async def pwa_manifest():
        # url_for внутри admin_bp автоматически подставит '/{secret}'.
        secret_prefix = (admin_bp_instance.url_prefix or '/').rstrip('/') or ''
        scope = (secret_prefix + '/') if not secret_prefix.endswith('/') else secret_prefix

        # start_url: дашборд (если модератор без доступа к дашборду —
        # бэкенд сам редиректит на доступную страницу).
        try:
            start_url = url_for('admin.dashboard')
        except Exception:
            start_url = scope

        # Иконки: PNG-набор под Android/iOS/Lighthouse + SVG как векторный фолбэк.
        # PNG'и сгенерены через tools/generate_pwa_icons.py из того же дизайна,
        # что и SVG-фавикон.
        def _ico(fn: str) -> str:
            return url_for('admin.admin_static', filename=f'icons/{fn}')

        # Имя берём из настройки project_name (синхронизировано с шапкой и login).
        app_name = _get_project_name()
        short_name = _make_short_name(app_name)

        manifest = {
            'name':             app_name,
            'short_name':       short_name,
            'description':      app_name,
            'start_url':        start_url,
            'scope':            scope,
            'display':          'standalone',
            'orientation':      'any',
            'background_color': '#0b0d12',
            'theme_color':      '#0b0d12',
            'lang':             'ru',
            'dir':              'ltr',
            'icons': [
                # PNG — обязательны для Android-launcher'а и Lighthouse PWA.
                {'src': _ico('icon-192.png'),          'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
                {'src': _ico('icon-512.png'),          'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
                {'src': _ico('icon-512-maskable.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
                # SVG — векторный фолбэк (Chrome/Edge скейлят без потерь под любые размеры).
                {'src': _ico('icon.svg'),              'sizes': 'any',     'type': 'image/svg+xml', 'purpose': 'any'},
                {'src': _ico('icon-maskable.svg'),     'sizes': 'any',     'type': 'image/svg+xml', 'purpose': 'maskable'},
            ],
        }

        # tojson через quart? используем стандартный json — manifest.webmanifest
        # должен отдаваться как application/manifest+json.
        import json as _json
        body = _json.dumps(manifest, ensure_ascii=False, indent=2)
        return Response(
            body,
            status=200,
            mimetype='application/manifest+json',
            headers={'Cache-Control': 'public, max-age=3600'},
        )

    # ─────────────────────────────────────────────────────────────
    #  Service Worker
    # ─────────────────────────────────────────────────────────────
    @admin_bp_instance.route('/sw.js', methods=['GET'])
    async def pwa_service_worker():
        secret_prefix = (admin_bp_instance.url_prefix or '/').rstrip('/') or ''
        scope = (secret_prefix + '/') if not secret_prefix.endswith('/') else secret_prefix
        offline_url = url_for('admin.pwa_offline')

        # Стратегии кэша:
        #   • CacheFirst для статики (CSS/JS/fonts/иконки) — мгновенно из кэша,
        #     ревалидация в фоне.
        #   • NetworkFirst для HTML и API — всегда свежие данные, при потере
        #     сети — фолбэк на offline.html (только для navigate).
        sw_js = f"""// Auto-generated by web_admin/routes/pwa.py
// Cache version: {PWA_CACHE_VERSION}
// Scope: {scope}

const CACHE_VERSION = '{PWA_CACHE_VERSION}';
const STATIC_CACHE  = 'admin-static-' + CACHE_VERSION;
const RUNTIME_CACHE = 'admin-runtime-' + CACHE_VERSION;
const OFFLINE_URL   = {offline_url!r};
const SCOPE         = {scope!r};

// При установке — прогреваем offline-страницу, чтобы она была
// доступна даже сразу после первого визита без интернета.
self.addEventListener('install', (event) => {{
  event.waitUntil((async () => {{
    const cache = await caches.open(STATIC_CACHE);
    try {{
      await cache.add(new Request(OFFLINE_URL, {{ cache: 'reload' }}));
    }} catch (_) {{}}
    self.skipWaiting();
  }})());
}});

// При активации — выкашиваем старые версии кэшей.
self.addEventListener('activate', (event) => {{
  event.waitUntil((async () => {{
    const names = await caches.keys();
    await Promise.all(
      names
        .filter((n) => n.startsWith('admin-') && !n.endsWith(CACHE_VERSION))
        .map((n) => caches.delete(n))
    );
    await self.clients.claim();
  }})());
}});

// Утилита: «это статика, которую можно кэшить»?
const isStaticAsset = (url) => {{
  if (!url.pathname.startsWith(SCOPE)) return false;
  const sub = url.pathname.slice(SCOPE.length);
  return sub.startsWith('static/');
}};

// Утилита: navigate-запрос (переход по ссылке/перезагрузка).
const isNavigation = (req) => req.mode === 'navigate'
  || (req.method === 'GET'
      && (req.headers.get('accept') || '').includes('text/html'));

self.addEventListener('fetch', (event) => {{
  const req = event.request;

  // POST/PUT/DELETE и другие — НЕ перехватываем (могут быть с CSRF и т.п.).
  if (req.method !== 'GET') return;

  let url;
  try {{ url = new URL(req.url); }} catch (_) {{ return; }}

  // Чужие домены и не-наш scope — пропускаем (пусть браузер делает как обычно).
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith(SCOPE)) return;

  // ── Статика: cache-first, ревалидация в фоне ──
  if (isStaticAsset(url)) {{
    event.respondWith((async () => {{
      const cache = await caches.open(STATIC_CACHE);
      const cached = await cache.match(req);
      const fetchPromise = fetch(req).then((resp) => {{
        if (resp && resp.ok) {{
          try {{ cache.put(req, resp.clone()); }} catch (_) {{}}
        }}
        return resp;
      }}).catch(() => cached);
      return cached || fetchPromise;
    }})());
    return;
  }}

  // ── Навигации: network-first, при ошибке — offline-страница ──
  if (isNavigation(req)) {{
    event.respondWith((async () => {{
      try {{
        const fresh = await fetch(req);
        // Кэшируем последнюю успешную страницу — на всякий, как фолбэк.
        if (fresh && fresh.ok && fresh.type !== 'opaqueredirect') {{
          const cache = await caches.open(RUNTIME_CACHE);
          try {{ cache.put(req, fresh.clone()); }} catch (_) {{}}
        }}
        return fresh;
      }} catch (_) {{
        const cache = await caches.open(RUNTIME_CACHE);
        const cached = await cache.match(req);
        if (cached) return cached;
        const offline = await caches.match(OFFLINE_URL);
        if (offline) return offline;
        return new Response('Offline', {{ status: 503, statusText: 'Offline' }});
      }}
    }})());
    return;
  }}

  // ── Всё остальное (XHR/fetch к /api/...) — network-only.
  //   Кэшировать ответы API нельзя: там могут быть приватные данные
  //   и состояние авторизации. Пусть приложение само решает что делать
  //   при offline.
}});

// Сообщение от страницы: SKIP_WAITING — мгновенно активируем новую версию.
self.addEventListener('message', (event) => {{
  if (event.data && event.data.type === 'SKIP_WAITING') {{
    self.skipWaiting();
  }}
}});

// ── Push-уведомления ────────────────────────────────────────────────────
// Получаем зашифрованное сообщение от push-сервиса браузера, расшифровывает
// его сам браузер, нам приходит уже распакованный JSON.
//
// Адреса иконок резолвим относительно SCOPE — бэкенду не нужно знать
// секретный префикс админки:
//   • 'static/icons/foo.png'    → SCOPE + 'static/icons/foo.png'
//   • '/static/icons/foo.png'   → как есть (абсолютный путь от origin)
//   • 'https://...'             → как есть (внешний)
function resolveAsset(p, fallback) {{
  if (!p) return fallback;
  if (/^https?:\\/\\//i.test(p)) return p;
  if (p.startsWith('/'))         return p;
  return SCOPE + p;
}}

self.addEventListener('push', (event) => {{
  let data = {{}};
  try {{
    if (event.data) data = event.data.json();
  }} catch (e) {{
    data = {{ title: 'Уведомление', body: (event.data && event.data.text()) || '' }};
  }}

  const title = data.title || 'Уведомление';
  const body  = data.body  || '';
  const url   = (data.data && data.data.url) ? (SCOPE.replace(/\\/$/, '') + data.data.url) : SCOPE;
  const fallbackIcon = SCOPE + 'static/icons/icon-192.png';

  const opts = {{
    body,
    icon:  resolveAsset(data.icon,  fallbackIcon),
    badge: resolveAsset(data.badge, fallbackIcon),
    tag:   data.tag   || 'general',
    renotify: !!data.renotify,
    requireInteraction: !!data.requireInteraction,
    data:  Object.assign({{}}, data.data || {{}}, {{ url }}),
    timestamp: Date.now(),
  }};

  event.waitUntil(self.registration.showNotification(title, opts));
}});

// Абсолютный URL внутри scope админки (push хранит /{{secret}}/payments).
function resolveAdminUrl(pathOrUrl) {{
  if (!pathOrUrl) return self.location.origin + SCOPE;
  try {{
    if (/^https?:\\/\\//i.test(pathOrUrl)) return pathOrUrl;
    const base = self.location.origin;
    const scopePath = SCOPE.endsWith('/') ? SCOPE.slice(0, -1) : SCOPE;
    if (pathOrUrl.startsWith(scopePath + '/') || pathOrUrl === scopePath) {{
      return base + pathOrUrl;
    }}
    if (pathOrUrl.startsWith('/')) {{
      return base + scopePath + pathOrUrl;
    }}
    return base + scopePath + '/' + pathOrUrl;
  }} catch (_) {{
    return self.location.origin + SCOPE;
  }}
}}

async function notifyPwaLayoutFix(clients) {{
  for (const client of clients) {{
    try {{ client.postMessage({{ type: 'PWA_LAYOUT_FIX' }}); }} catch (_) {{}}
  }}
}}

// Клик по уведомлению — открываем/фокусируем нужную страницу админки.
self.addEventListener('notificationclick', (event) => {{
  event.notification.close();
  const rawUrl = (event.notification.data && event.notification.data.url) || SCOPE;
  const targetUrl = resolveAdminUrl(rawUrl);
  event.waitUntil((async () => {{
    const allClients = await self.clients.matchAll({{
      type: 'window', includeUncontrolled: true,
    }});
    const scopeOrigin = self.location.origin + (SCOPE.endsWith('/') ? SCOPE : SCOPE + '/');
    // Если уже открыто окно с админкой — фокусируем его и переходим
    for (const client of allClients) {{
      try {{
        if (client.url.startsWith(scopeOrigin) || client.url.startsWith(self.location.origin + SCOPE.replace(/\\/$/, ''))) {{
          await client.focus();
          if ('navigate' in client) {{
            try {{
              await client.navigate(targetUrl);
              await notifyPwaLayoutFix([client]);
              return;
            }} catch (_) {{}}
          }}
          try {{ client.postMessage({{ type: 'PWA_NAVIGATE', url: targetUrl }}); }} catch (_) {{}}
          await notifyPwaLayoutFix([client]);
          return;
        }}
      }} catch (_) {{}}
    }}
    // Иначе — открываем новое окно (абсолютный URL обязателен для iOS PWA)
    try {{
      const opened = await self.clients.openWindow(targetUrl);
      if (opened) await notifyPwaLayoutFix([opened]);
    }} catch (_) {{}}
  }})());
}});

// Подписка устарела/обновлена — push-сервис может попросить перерегистрацию.
// Молча перерегистрируемся с тем же applicationServerKey (если он закэширован).
self.addEventListener('pushsubscriptionchange', (event) => {{
  event.waitUntil((async () => {{
    try {{
      const cfgResp = await fetch(SCOPE + 'api/pwa/push/config', {{ credentials: 'same-origin' }});
      if (!cfgResp.ok) return;
      const cfg = await cfgResp.json();
      if (!cfg.ok || !cfg.public_key) return;

      // Декодируем ключ
      const padding = '='.repeat((4 - cfg.public_key.length % 4) % 4);
      const b64 = (cfg.public_key + padding).replace(/-/g, '+').replace(/_/g, '/');
      const raw = atob(b64);
      const arr = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);

      const newSub = await self.registration.pushManager.subscribe({{
        userVisibleOnly: true,
        applicationServerKey: arr,
      }});
      const j = newSub.toJSON();
      await fetch(SCOPE + 'api/pwa/push/subscribe', {{
        method: 'POST',
        credentials: 'same-origin',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ endpoint: j.endpoint, keys: j.keys, events_mask: 1 }}),
      }});
    }} catch (_) {{}}
  }})());
}});
"""
        return Response(
            sw_js,
            status=200,
            mimetype='application/javascript',
            headers={
                # SW должен быть «свежим» — иначе обновления не приедут.
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                # Service-Worker-Allowed разрешает scope шире, чем путь SW.
                # У нас scope равен директории SW — заголовок необязателен,
                # но не помешает.
                'Service-Worker-Allowed': (admin_bp_instance.url_prefix or '/'),
            },
        )

    # ─────────────────────────────────────────────────────────────
    #  Offline-fallback страница
    # ─────────────────────────────────────────────────────────────
    @admin_bp_instance.route('/offline.html', methods=['GET'])
    async def pwa_offline():
        try:
            icon_svg = url_for('admin.admin_static', filename='icons/icon.svg')
            icon_png = url_for('admin.admin_static', filename='icons/icon-192.png')
        except Exception:
            icon_svg = ''
            icon_png = ''
        app_name = _get_project_name()
        # Минимальная защита от XSS внутри inline-HTML (project_name
        # редактируется только админом, но всё равно).
        from html import escape as _esc
        app_name_safe = _esc(app_name)
        html = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Нет связи · {app_name_safe}</title>
  <link rel="icon" type="image/svg+xml" href="{icon_svg}">
  <link rel="apple-touch-icon" href="{icon_png}">
  <meta name="theme-color" content="#0b0d12">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{
      margin: 0; padding: 0; height: 100%;
      background: #0b0d12; color: #e5e7eb;
      font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      -webkit-font-smoothing: antialiased;
    }}
    .wrap {{
      min-height: 100%; display: flex; align-items: center; justify-content: center;
      padding: 24px;
    }}
    .card {{
      max-width: 420px; width: 100%;
      background: #111418;
      border: 1px solid #20242c;
      border-radius: 16px;
      padding: 28px 24px;
      text-align: center;
    }}
    .icon {{
      width: 64px; height: 64px; margin: 0 auto 14px;
      border-radius: 14px;
      background: linear-gradient(135deg, #1f242c, #11141a);
      display: flex; align-items: center; justify-content: center;
      font-size: 32px; font-weight: 900;
      background-clip: padding-box;
      color: #9ca3af;
    }}
    h1 {{ margin: 0 0 8px; font-size: 18px; }}
    p  {{ margin: 0 0 18px; color: #9ca3af; font-size: 13px; }}
    button {{
      appearance: none; border: 1px solid #2a2f37;
      background: #181c22; color: #e5e7eb;
      padding: 9px 16px; border-radius: 10px; cursor: pointer;
      font: inherit;
      transition: background .15s, border-color .15s;
    }}
    button:hover {{ background: #1f242c; border-color: #353b45; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="icon">×</div>
      <h1>Нет связи</h1>
      <p>Не удалось получить страницу с сервера. Проверьте интернет и попробуйте снова.</p>
      <button onclick="location.reload()">Повторить</button>
    </div>
  </div>
</body>
</html>
"""
        return Response(
            html, status=200, mimetype='text/html; charset=utf-8',
            headers={'Cache-Control': 'public, max-age=300'},
        )
