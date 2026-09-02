"""Приложение внутри Telegram: каталог, профиль, роутер, продление.

Данные берутся из тех же обработчиков, которыми живёт бот
(`api/routes/catalog_api.py`), — здесь только вход и оболочка. Дублировать
их логику нельзя: разъехавшись, экран бота и экран приложения показывали бы
клиенту разные вещи про один и тот же заказ.

Вход отличается принципиально. Каталожные ручки закрыты служебным токеном
и принимают `tg_id` параметром — так ходит бот, процесс, которому мы верим.
Из браузера так нельзя: токен уехал бы клиенту, а `tg_id` подставил бы
кто угодно. Поэтому здесь `tg_id` берётся исключительно из подписи Telegram
и подставляется в вызовы сам.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session, get_transaction
from api.routes import catalog_api
from core.config import settings
from core.services.miniapp_auth import InitDataError, TelegramUser, parse_init_data

log = structlog.get_logger("api.miniapp")

router = APIRouter(prefix="/app", tags=["miniapp"], include_in_schema=False)

INIT_DATA_HEADER = "X-Telegram-Init-Data"


async def current_user(
    init_data: str = Header(default="", alias=INIT_DATA_HEADER),
) -> TelegramUser:
    """Кто открыл приложение. Единственный источник `tg_id` во всём модуле."""
    if not settings.miniapp.is_configured:
        # Не настроено — ручки как будто нет. Иначе выключенная возможность
        # молча отвечала бы всем, кто нашёл адрес.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")

    try:
        user = parse_init_data(
            init_data,
            bot_token=settings.miniapp.bot_token.get_secret_value(),
            max_age_sec=settings.miniapp.init_data_max_age_sec,
        )
    except InitDataError as exc:
        log.info("miniapp.entry_rejected", reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    if not settings.miniapp.is_allowed(user.tg_id):
        # Обкатка: список закрыт, и отказ должен быть внятным — иначе первый же
        # позванный на тест решит, что приложение сломано.
        log.info("miniapp.not_in_allowlist", tg_id=user.tg_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Приложение пока открыто не всем.",
        )
    return user


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def app_page() -> HTMLResponse:
    """Оболочка приложения. Пускаем всех: данных здесь нет.

    Проверять вход на странице бессмысленно — она статическая, а подпись
    появляется только в браузере Telegram. Всё, что стоит денег и знает про
    клиента, лежит за `/app/api/*`, и там вход обязателен.
    """
    if not settings.miniapp.is_configured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return HTMLResponse(_PAGE)


@router.get("/api/home")
async def home(
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Профиль: кто это, что с подпиской, есть ли роутер, последние заказы.

    Состояние подписки берём у ручки продления, а не считаем заново: она уже
    отвечает и «есть ли клиент», и сроком, и делает это ровно так, как экран
    продления. Своим запросом мы завели бы второй источник правды про одно
    и то же число.
    """
    state = await catalog_api.renew_state(tg_id=user.tg_id, session=session)
    available = await catalog_api.my_router_available(tg_id=user.tg_id, session=session)
    orders = (
        await catalog_api.list_orders(tg_id=user.tg_id, limit=5, session=session)
        if state.get("has_client")
        else {"orders": []}
    )

    return {
        "user": {
            "tg_id": user.tg_id,
            "name": user.display_name,
            "username": user.username,
        },
        "has_client": bool(state.get("has_client")),
        "subscription": state.get("subscription") or {},
        "router_available": bool(available.get("show")),
        "orders": orders.get("orders", []),
    }


@router.get("/api/catalog")
async def catalog(
    _: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Витрина. `include_hidden` передаём явно: у обработчика это значение
    из `Query(...)`, и без него в выборку уехал бы сам объект параметра."""
    return await catalog_api.list_products(session=session, include_hidden=False)


@router.get("/api/router")
async def my_router(
    device_id: int = 0,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Экран роутера — тот же, что в боте, вплоть до срока из панели."""
    return await catalog_api.my_router(
        tg_id=user.tg_id, device_id=device_id, session=session
    )


@router.get("/api/renew")
async def renew_state(
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Сроки и цены продления."""
    return await catalog_api.renew_state(tg_id=user.tg_id, session=session)


@router.post("/api/renew")
async def renew_start(
    payload: dict,
    user: TelegramUser = Depends(current_user),
    session: AsyncSession = Depends(get_transaction),
) -> dict:
    """Ссылка на оплату продления.

    `tg_id` берётся из подписи и затирает то, что прислал браузер: иначе
    подставив чужой номер, можно было бы оплатить чужую подписку — или,
    что хуже, увидеть в ответе чужую платёжную ссылку.
    """
    safe = dict(payload or {})
    safe["tg_id"] = user.tg_id
    safe["username"] = user.username
    safe["first_name"] = user.first_name
    return await catalog_api.renew_start(payload=safe, session=session)


_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Titan Routers</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #17212b);
    --card: var(--tg-theme-secondary-bg-color, #232e3c);
    --text: var(--tg-theme-text-color, #f5f5f5);
    --muted: var(--tg-theme-hint-color, #8b98a5);
    --link: var(--tg-theme-link-color, #6ab3f3);
    --btn: var(--tg-theme-button-color, #2ea6ff);
    --btn-text: var(--tg-theme-button-text-color, #fff);
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.45 -apple-system, 'Segoe UI', Roboto, sans-serif;
    padding-bottom: calc(64px + env(safe-area-inset-bottom));
  }
  .wrap { padding: 14px; }
  .card { background: var(--card); border-radius: 14px; padding: 14px; margin-bottom: 10px; }
  .muted { color: var(--muted); }
  .small { font-size: 13px; }
  .row { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
  .row + .row { margin-top: 8px; }
  h1 { font-size: 20px; margin: 2px 0 12px; }
  h2 { font-size: 15px; margin: 0 0 8px; }
  .big { font-size: 22px; font-weight: 600; }
  button.act {
    background: var(--btn); color: var(--btn-text); border: 0; border-radius: 10px;
    padding: 11px 14px; font-size: 15px; font-weight: 500; width: 100%; cursor: pointer;
  }
  button.act[disabled] { opacity: .5; }
  .plan { display: flex; justify-content: space-between; align-items: center;
          padding: 12px 0; border-top: 1px solid rgba(255,255,255,.07); }
  .plan:first-of-type { border-top: 0; }
  .pill { font-size: 12px; padding: 2px 9px; border-radius: 999px; }
  .ok { color: #4fd18b; background: rgba(79,209,139,.13); }
  .off { color: var(--muted); background: rgba(139,152,165,.15); }
  .prod img { width: 100%; border-radius: 10px; display: block; margin-bottom: 10px; }
  nav {
    position: fixed; left: 0; right: 0; bottom: 0; display: flex;
    background: var(--card); padding-bottom: env(safe-area-inset-bottom);
    border-top: 1px solid rgba(255,255,255,.07);
  }
  nav button {
    flex: 1; background: none; border: 0; color: var(--muted);
    padding: 11px 4px 13px; font-size: 11px; cursor: pointer;
  }
  nav button.on { color: var(--link); }
  nav button i { display: block; font-size: 19px; font-style: normal; margin-bottom: 3px; }
  .err { color: #ff8f8f; }
</style>
</head>
<body>
<div class="wrap" id="screen"><div class="muted">Загружаем…</div></div>

<nav>
  <button data-tab="home" class="on"><i>&#128100;</i>Профиль</button>
  <button data-tab="router"><i>&#128246;</i>Мой роутер</button>
  <button data-tab="renew"><i>&#11088;</i>Подписка</button>
  <button data-tab="catalog"><i>&#128230;</i>Каталог</button>
</nav>

<script>
(function () {
  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) {
    document.getElementById('screen').innerHTML =
      '<div class="card err">Приложение открывают из Telegram — здесь нет подписи входа.</div>';
    return;
  }
  tg.ready();
  tg.expand();

  var screen = document.getElementById('screen');
  var tab = 'home';

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
    });
  }

  function date(iso) {
    if (!iso) { return '—'; }
    var d = new Date(iso);
    return isNaN(d) ? '—' : d.toLocaleDateString('ru-RU',
      { day: '2-digit', month: '2-digit', year: 'numeric' });
  }

  function money(value, currency) {
    var n = Number(value);
    if (isNaN(n)) { return String(value || ''); }
    return n.toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: 2 })
      + ' ' + (currency === 'RUB' || !currency ? '\\u20BD' : currency);
  }

  // Подпись входа уходит заголовком, а не в адресе: адреса попадают в журналы
  // прокси, и строка входа лежала бы там открытым текстом.
  function api(path, options) {
    var opts = options || {};
    opts.headers = Object.assign({}, opts.headers, {
      'X-Telegram-Init-Data': tg.initData || '',
      'Content-Type': 'application/json'
    });
    return fetch('/app/api' + path, opts).then(function (r) {
      // Ответ разбираем через текст, а не сразу как JSON: на пути стоит прокси,
      // и его страница на 502 или 504 роняла бы разбор с «Unexpected token '<'»
      // вместо внятного «сервер не ответил».
      return r.text().then(function (raw) {
        var body = null;
        try { body = raw ? JSON.parse(raw) : null; } catch (e) { body = null; }
        if (!r.ok) {
          // Наш обработчик кладёт причину в `error`, FastAPI по умолчанию —
          // в `detail`. Читаем оба, иначе отказ выглядит голым кодом.
          var reason = body && (body.error || body.detail);
          throw new Error(reason || 'Ошибка ' + r.status);
        }
        return body;
      });
    });
  }

  function show(html) { screen.innerHTML = html; }
  function loading() { show('<div class="muted">Загружаем…</div>'); }
  function failed(err) {
    show('<div class="card err">' + esc(err.message || err) + '</div>');
  }

  var views = {
    home: function () {
      return api('/home').then(function (d) {
        var sub = d.subscription || {};
        var active = sub.status === 'active';
        var orders = (d.orders || []).map(function (o) {
          return '<div class="row small"><span>' + esc(o.number || o.public_number || o.id)
            + '</span><span class="muted">' + esc(o.status_title || o.status || '') + '</span></div>';
        }).join('');
        show(
          '<h1>' + esc((d.user || {}).name) + '</h1>'
          + '<div class="card">'
          +   '<div class="row"><span class="muted">Подписка</span>'
          +     '<span class="pill ' + (active ? 'ok' : 'off') + '">'
          +       (active ? 'активна' : 'нет') + '</span></div>'
          +   (sub.until ? '<div class="row"><span class="muted">Действует до</span><span>'
                + date(sub.until) + '</span></div>' : '')
          + '</div>'
          + (orders
              ? '<div class="card"><h2>Последние заказы</h2>' + orders + '</div>'
              : '<div class="card muted small">Заказов пока нет.</div>')
        );
      });
    },

    router: function () {
      return api('/router').then(function (d) {
        if (!d.has_client || !d.router) {
          return show('<div class="card muted">Роутера за вами пока не числится.</div>');
        }
        var r = d.router;
        show(
          '<h1>Мой роутер</h1>'
          + '<div class="card">'
          +   '<div class="row"><span class="muted">Связь</span><span class="pill '
          +     (r.online ? 'ok' : 'off') + '">' + (r.online ? 'на связи' : 'молчит') + '</span></div>'
          +   '<div class="row"><span class="muted">MAC</span><span>' + esc(r.mac) + '</span></div>'
          +   (r.model ? '<div class="row"><span class="muted">Модель</span><span>'
                + esc(r.model) + '</span></div>' : '')
          +   (r.until ? '<div class="row"><span class="muted">Подписка до</span><span>'
                + date(r.until) + '</span></div>' : '')
          + '</div>'
          + (d.instruction_url
              ? '<button class="act" data-link="' + esc(d.instruction_url) + '">Инструкция</button>'
              : '')
        );
        // Обработчик вешаем отдельно, а не атрибутом onclick: адрес приезжает
        // из базы, и одна кавычка в нём разломала бы разметку кнопки.
        var help = screen.querySelector('[data-link]');
        if (help) {
          help.addEventListener('click', function () { tg.openLink(help.dataset.link); });
        }
      });
    },

    renew: function () {
      return api('/renew').then(function (d) {
        var sub = d.subscription || {};
        var plans = (d.plans || []).map(function (p) {
          return '<div class="plan"><div><div>' + esc(p.title) + '</div>'
            + '<div class="muted small">' + esc(p.months) + ' мес.</div></div>'
            + '<button class="act" style="width:auto" data-plan="' + esc(p.id) + '">'
            + money(p.price, d.currency) + '</button></div>';
        }).join('');
        show(
          '<h1>Подписка</h1>'
          + (sub.until
              ? '<div class="card"><div class="row"><span class="muted">Действует до</span>'
                + '<span class="big">' + date(sub.until) + '</span></div></div>'
              : '')
          + (plans
              ? '<div class="card">' + plans + '</div>'
              : '<div class="card muted">Сроков для продления сейчас нет.</div>')
        );
        screen.querySelectorAll('[data-plan]').forEach(function (btn) {
          btn.addEventListener('click', function () {
            btn.disabled = true;
            btn.textContent = 'Готовим…';
            api('/renew', { method: 'POST', body: JSON.stringify({ plan_id: Number(btn.dataset.plan) }) })
              .then(function (res) {
                if (!res.ok) { throw new Error(res.error || 'Не получилось'); }
                tg.openLink(res.pay_url);
                btn.textContent = 'Оплатить';
                btn.disabled = false;
              })
              .catch(function (err) {
                btn.textContent = 'Ошибка';
                btn.disabled = false;
                tg.showAlert(err.message || String(err));
              });
          });
        });
      });
    },

    catalog: function () {
      return api('/catalog').then(function (d) {
        var items = (d.products || []).map(function (p) {
          return '<div class="card prod">'
            + (p.photo_url ? '<img src="' + esc(p.photo_url) + '" alt="">' : '')
            + '<div class="row"><b>' + esc(p.title) + '</b><span>'
            + money(p.price, d.currency) + '</span></div>'
            + (p.description ? '<div class="muted small" style="margin-top:6px">'
                + esc(p.description) + '</div>' : '')
            + '</div>';
        }).join('');
        show('<h1>Каталог</h1>' + (items || '<div class="card muted">Пока пусто.</div>'));
      });
    }
  };

  function open(name) {
    tab = name;
    document.querySelectorAll('nav button').forEach(function (b) {
      b.classList.toggle('on', b.dataset.tab === name);
    });
    loading();
    views[name]().catch(failed);
  }

  document.querySelectorAll('nav button').forEach(function (b) {
    b.addEventListener('click', function () { open(b.dataset.tab); });
  });

  open('home');
})();
</script>
</body>
</html>"""
