/* Приложение в Telegram: профиль, роутер, заказы, каталог с покупкой.

   Один файл без сборки и без внешних библиотек — намеренно. Экран открывают
   из мессенджера, часто на плохой связи, и каждый лишний запрос к чужому
   адресу это ещё одна причина увидеть пустоту вместо каталога.

   Данные берутся у наших же ручек `/app/api/*`; `tg_id` в них не передаётся
   никогда — он берётся из подписи входа на стороне сервера. */

(function () {
  'use strict';

  var tg = window.Telegram && window.Telegram.WebApp;
  var screen = document.getElementById('screen');

  if (!tg || !tg.initData) {
    screen.innerHTML =
      '<div class="card"><div class="row"><span class="ic-box">'
      + '<svg class="ic"><use href="#i-alert"/></svg></span>'
      + '<div class="grow"><b>Нужен Telegram</b>'
      + '<div class="muted small">Приложение открывают из бота: подпись входа '
      + 'выдаёт мессенджер, и вне его подтвердить, кто вы, нечем.</div></div></div></div>';
    return;
  }

  tg.ready();
  tg.expand();
  // Рамка вокруг приложения красится под наш фон: иначе шапка мессенджера
  // остаётся светлой и приложение выглядит вставленным в чужое окно.
  try {
    tg.setHeaderColor('#0b1220');
    tg.setBackgroundColor('#0b1220');
  } catch (e) { /* старые клиенты этого не умеют — не беда */ }

  function haptic(kind) {
    try { tg.HapticFeedback.impactOccurred(kind || 'light'); } catch (e) { /* не везде есть */ }
  }

  /* --- Мелочи ------------------------------------------------------------ */

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function icon(name, cls) {
    return '<svg class="' + (cls || 'ic') + '"><use href="#i-' + name + '"/></svg>';
  }

  function date(iso) {
    if (!iso) { return '—'; }
    var d = new Date(iso);
    return isNaN(d) ? '—' : d.toLocaleDateString('ru-RU',
      { day: '2-digit', month: 'long', year: 'numeric' });
  }

  function money(value, currency) {
    var n = Number(value);
    if (isNaN(n)) { return String(value == null ? '' : value); }
    var text = n.toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: 2 });
    return text + ' ' + (!currency || currency === 'RUB' ? '₽' : currency);
  }

  function bytes(value) {
    var n = Number(value);
    if (!n || isNaN(n)) { return '—'; }
    var units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + ' ' + units[i];
  }

  function uptime(seconds) {
    var s = Number(seconds);
    if (!s || isNaN(s)) { return '—'; }
    var days = Math.floor(s / 86400);
    var hours = Math.floor((s % 86400) / 3600);
    return days ? days + ' сут ' + hours + ' ч' : hours + ' ч';
  }

  /* --- Разговор с сервером ----------------------------------------------- */

  function api(path, options) {
    var opts = options || {};
    opts.headers = Object.assign({}, opts.headers, {
      // Подпись уходит заголовком, а не в адресе: адреса попадают в журналы
      // прокси, и строка входа лежала бы там открытым текстом.
      'X-Telegram-Init-Data': tg.initData || '',
      'Content-Type': 'application/json'
    });
    return fetch('/app/api' + path, opts).then(function (r) {
      // Разбираем через текст: на пути стоит прокси, и его страница на 502
      // роняла бы разбор с «Unexpected token '<'» вместо внятной причины.
      return r.text().then(function (raw) {
        var body = null;
        try { body = raw ? JSON.parse(raw) : null; } catch (e) { body = null; }
        if (!r.ok) {
          var reason = body && (body.error || body.detail);
          throw new Error(reason || 'Сервер ответил ' + r.status);
        }
        return body;
      });
    });
  }

  /* --- Экраны и переходы -------------------------------------------------- */

  var TABS = ['home', 'router', 'orders', 'catalog'];
  var stack = [];          // история переходов внутри вкладки
  var current = null;

  function show(html) {
    screen.innerHTML = html;
    window.scrollTo(0, 0);
  }

  function skeleton() {
    show('<div class="skel line" style="width:45%"></div>'
      + '<div class="skel block"></div><div class="skel block"></div>');
  }

  function failed(err) {
    show('<div class="card"><div class="row" style="align-items:flex-start">'
      + '<span class="ic-box" style="background:rgba(255,107,107,.13);color:var(--err)">'
      + icon('alert') + '</span>'
      + '<div class="grow"><b>Не получилось</b>'
      + '<div class="muted small" style="margin-top:4px">' + esc(err && err.message || err) + '</div>'
      + '</div></div>'
      + '<button class="btn ghost" id="again" style="margin-top:12px">'
      + icon('refresh') + 'Ещё раз</button></div>');
    var again = document.getElementById('again');
    if (again) { again.addEventListener('click', function () { go(current, true); }); }
  }

  // Родная кнопка «назад» вместо своей: в Telegram она в шапке, и клиент
  // ищет её именно там, а не внутри страницы.
  function syncBack() {
    try {
      if (stack.length > 1) { tg.BackButton.show(); } else { tg.BackButton.hide(); }
    } catch (e) { /* старые клиенты */ }
  }

  function go(view, replace) {
    if (!view) { return; }
    if (replace && stack.length) { stack[stack.length - 1] = view; }
    else { stack.push(view); }
    render(view);
  }

  function back() {
    if (stack.length > 1) { stack.pop(); render(stack[stack.length - 1]); }
  }

  function openTab(name) {
    stack = [];
    document.querySelectorAll('nav button').forEach(function (b) {
      b.classList.toggle('on', b.dataset.tab === name);
    });
    go({ name: name });
  }

  function render(view) {
    current = view;
    var tabName = TABS.indexOf(view.name) >= 0 ? view.name : null;
    if (tabName) {
      document.querySelectorAll('nav button').forEach(function (b) {
        b.classList.toggle('on', b.dataset.tab === tabName);
      });
    }
    syncBack();
    skeleton();
    var draw = views[view.name];
    if (!draw) { return failed(new Error('Неизвестный экран: ' + view.name)); }
    Promise.resolve()
      .then(function () { return draw(view); })
      .catch(failed);
  }

  /* --- Общие куски разметки ---------------------------------------------- */

  function empty(iconName, title, text) {
    return '<div class="empty"><span class="ic-box">' + icon(iconName, 'ic-lg') + '</span>'
      + '<div><b>' + esc(title) + '</b></div>'
      + (text ? '<div class="small" style="margin-top:5px">' + esc(text) + '</div>' : '')
      + '</div>';
  }

  // Цвет метки по статусу. Значения — из `core/enums.OrderStatus`, слова
  // приходят с сервера (`status_title`): переводить их здесь значило бы
  // завести вторую таблицу, которая разойдётся на первом же новом статусе.
  var ORDER_TONE = {
    'new': 'warn', awaiting_payment: 'warn',
    paid: 'ok', packing: 'ok', shipped: 'ok', delivered: 'ok', activated: 'ok', done: 'ok',
    cancelled: 'off'
  };

  function orderRow(o) {
    var tone = ORDER_TONE[o.status] || 'off';
    return '<button class="card tight" data-order="' + esc(o.id) + '"'
      + ' style="display:block;width:100%;text-align:left;font-family:inherit;color:inherit;cursor:pointer">'
      + '<div class="row"><div class="grow">'
      + '<div class="mono small">' + esc(o.number || ('#' + o.id)) + '</div>'
      + '<div class="muted tiny" style="margin-top:3px">' + date(o.created_at) + '</div>'
      + '</div>'
      + '<div style="text-align:right">'
      + '<div>' + money(o.total, o.currency) + '</div>'
      + '<span class="pill ' + tone + '" style="margin-top:4px">' + esc(o.status_title || o.status) + '</span>'
      + '</div>'
      + '<span class="subtle">' + icon('chev-r') + '</span>'
      + '</div></button>';
  }

  function bindOrderRows() {
    screen.querySelectorAll('[data-order]').forEach(function (el) {
      el.addEventListener('click', function () {
        haptic();
        go({ name: 'order', id: Number(el.dataset.order) });
      });
    });
  }

  /* --- Профиль ------------------------------------------------------------ */

  var views = {};

  views.home = function () {
    return api('/home').then(function (d) {
      var sub = d.subscription || {};
      var active = sub.status === 'active';
      var user = d.user || {};
      var orders = (d.orders || []).slice(0, 3).map(orderRow).join('');

      show(
        '<h1>' + esc(user.name || 'Профиль') + '</h1>'

        + '<div class="card">'
        +   '<div class="row">'
        +     '<span class="ic-box">' + icon('shield') + '</span>'
        +     '<div class="grow"><div class="muted small">Подписка</div>'
        +       '<div style="margin-top:2px">'
        +         (active && sub.until ? 'до <b>' + date(sub.until) + '</b>' : 'не активна')
        +       '</div></div>'
        +     '<span class="pill ' + (active ? 'ok' : 'off') + '"><i class="dot"></i>'
        +       (active ? 'активна' : 'нет') + '</span>'
        +   '</div>'
        +   '<div class="hr"></div>'
        +   '<button class="btn" id="renew">' + icon('card') + 'Продлить подписку</button>'
        + '</div>'

        + (d.router_available
            ? '<button class="card tight" id="to-router" style="display:block;width:100%;'
              + 'text-align:left;font-family:inherit;color:inherit;cursor:pointer">'
              + '<div class="row"><span class="ic-box">' + icon('router') + '</span>'
              + '<div class="grow"><b>Мой роутер</b>'
              + '<div class="muted small">Связь, срок и обновление</div></div>'
              + '<span class="subtle">' + icon('chev-r') + '</span></div></button>'
            : '')

        + (orders
            ? '<h2 style="margin-top:18px">Последние заказы</h2>' + orders
              + '<button class="btn quiet" id="all-orders">Все заказы</button>'
            : '<div class="card flat muted small">Заказов пока нет. Загляните в каталог — '
              + 'роутер приедет с уже настроенным доступом.</div>')
      );

      document.getElementById('renew').addEventListener('click', function () {
        haptic(); go({ name: 'renew' });
      });
      var toRouter = document.getElementById('to-router');
      if (toRouter) {
        toRouter.addEventListener('click', function () { haptic(); openTab('router'); });
      }
      var all = document.getElementById('all-orders');
      if (all) { all.addEventListener('click', function () { haptic(); openTab('orders'); }); }
      bindOrderRows();
    });
  };

  /* --- Подписка ----------------------------------------------------------- */

  views.renew = function () {
    return api('/renew').then(function (d) {
      var sub = d.subscription || {};
      var plans = (d.plans || []).map(function (p) {
        return '<div class="plan"><div class="grow"><div><b>' + esc(p.title) + '</b></div>'
          + '<div class="muted small">' + esc(p.months) + ' мес.</div></div>'
          + '<button class="btn small" data-plan="' + esc(p.id) + '">'
          + money(p.price, d.currency) + '</button></div>';
      }).join('');

      show(
        '<h1>Подписка</h1>'
        + (sub.until
            ? '<div class="card"><div class="row"><div>'
              + '<div class="muted small">Действует до</div>'
              + '<div class="big" style="margin-top:2px">' + date(sub.until) + '</div></div>'
              + '<span class="ic-box">' + icon('shield') + '</span></div></div>'
            : '')
        + (plans
            ? '<div class="card">' + plans + '</div>'
              + '<div class="muted tiny center">Продление считается от текущей даты окончания — '
              + 'оставшиеся дни не сгорают.</div>'
            : empty('info', 'Сроков нет', 'Продление сейчас недоступно.'))
      );

      screen.querySelectorAll('[data-plan]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          haptic('medium');
          var was = btn.textContent;
          btn.disabled = true;
          btn.textContent = 'Готовим…';
          api('/renew', { method: 'POST', body: JSON.stringify({ plan_id: Number(btn.dataset.plan) }) })
            .then(function (res) {
              if (!res.ok) { throw new Error(res.error || 'Не получилось создать счёт'); }
              tg.openLink(res.pay_url);
              btn.disabled = false;
              btn.textContent = was;
            })
            .catch(function (err) {
              btn.disabled = false;
              btn.textContent = was;
              tg.showAlert(err.message || String(err));
            });
        });
      });
    });
  };

  /* --- Роутер ------------------------------------------------------------- */

  views.router = function (view) {
    var path = view && view.id ? '/router?device_id=' + view.id : '/router';
    return api(path).then(function (d) {
      if (!d.has_client || !d.router) {
        return show('<h1>Мой роутер</h1>'
          + empty('router', 'Роутера пока нет',
                  'Как только устройство выйдет на связь, здесь появятся его показания.'));
      }
      var r = d.router;
      var many = (d.routers || []).length > 1;

      var picker = many
        ? '<div class="card tight"><div class="muted small" style="margin-bottom:8px">Устройства</div>'
          + (d.routers || []).map(function (x) {
              return '<button class="btn ' + (x.id === r.id ? '' : 'ghost') + ' small"'
                + ' data-dev="' + esc(x.id) + '" style="margin:0 6px 6px 0">'
                + esc(x.mac) + '</button>';
            }).join('')
          + '</div>'
        : '';

      function stat(iconName, label, value) {
        return '<div class="row"><span class="muted small">'
          + '<span style="display:inline-flex;gap:7px;align-items:center">'
          + icon(iconName) + esc(label) + '</span></span><span>' + value + '</span></div>';
      }

      show(
        '<h1>Мой роутер</h1>'
        + picker
        + '<div class="card">'
        +   '<div class="row"><div class="grow"><div class="mono">' + esc(r.mac) + '</div>'
        +     '<div class="muted small">' + esc(r.model || 'Модель не указана') + '</div></div>'
        +     '<span class="pill ' + (r.online ? 'ok' : 'off') + '"><i class="dot"></i>'
        +       (r.online ? 'на связи' : 'молчит') + '</span></div>'
        +   (r.until ? '<div class="hr"></div>'
              + stat('shield', 'Подписка до', '<b>' + date(r.until) + '</b>') : '')
        + '</div>'

        + '<div class="card">'
        +   '<h2>Показания</h2>'
        +   stat('wifi', 'Устройств в сети', esc(r.clients == null ? '—' : r.clients))
        +   stat('clock', 'Аптайм', esc(uptime(r.uptime_sec)))
        +   stat('gauge', 'Загрузка', r.cpu_pct == null ? '—' : esc(r.cpu_pct) + '%')
        +   stat('swap', 'Трафик', bytes(r.rx_bytes) + ' / ' + bytes(r.tx_bytes))
        + '</div>'

        + '<div class="stack">'
        +   '<button class="btn ghost" id="upd">' + icon('download') + 'Обновить прошивку</button>'
        +   (d.instruction_url
                ? '<button class="btn quiet" id="help">' + icon('info') + 'Инструкция</button>' : '')
        + '</div>'
        + '<div class="muted tiny center" style="margin-top:10px">Обновление идёт в фоне и '
        + 'занимает несколько минут. Роутер перезагрузится сам.</div>'
      );

      screen.querySelectorAll('[data-dev]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          haptic(); go({ name: 'router', id: Number(btn.dataset.dev) }, true);
        });
      });

      var help = screen.querySelector('#help');
      if (help) {
        // Обработчик, а не onclick в разметке: адрес приезжает из базы, и одна
        // кавычка в нём разломала бы кнопку.
        help.addEventListener('click', function () { tg.openLink(d.instruction_url); });
      }

      document.getElementById('upd').addEventListener('click', function () {
        var btn = this;
        haptic('medium');
        btn.disabled = true;
        btn.innerHTML = icon('refresh') + 'Отправляем…';
        api('/router/update', {
          method: 'POST', body: JSON.stringify({ device_id: r.id })
        }).then(function (res) {
          if (!res.ok) { throw new Error(res.error || 'Роутер не ответил'); }
          btn.innerHTML = icon('check') + 'Команда ушла';
          tg.showAlert('Обновление запущено. Роутер сам перезагрузится через несколько минут.');
        }).catch(function (err) {
          btn.disabled = false;
          btn.innerHTML = icon('download') + 'Обновить прошивку';
          tg.showAlert(err.message || String(err));
        });
      });
    });
  };

  /* --- Заказы ------------------------------------------------------------- */

  views.orders = function () {
    return api('/orders').then(function (d) {
      var list = (d.orders || []).map(orderRow).join('');
      show('<h1>Заказы</h1>'
        + (list || empty('receipt', 'Заказов нет', 'Оформленные заказы появятся здесь.')));
      bindOrderRows();
    });
  };

  views.order = function (view) {
    return api('/orders/' + view.id).then(function (d) {
      var o = d.order || {};
      var tone = ORDER_TONE[o.status] || 'off';
      var items = (o.items || []).map(function (it) {
        return '<div class="row"><span class="grow ellip">' + esc(it.title) + '</span>'
          + '<span>' + money(it.total, o.currency) + '</span></div>';
      }).join('');

      function line(label, value, bold) {
        return '<div class="row"><span class="muted small">' + esc(label) + '</span>'
          + '<span' + (bold ? ' class="big"' : '') + '>' + value + '</span></div>';
      }

      show(
        '<h1>Заказ ' + esc(o.number || ('#' + o.id)) + '</h1>'

        + '<div class="card"><div class="row">'
        +   '<span class="muted small">' + date(o.created_at) + '</span>'
        +   '<span class="pill ' + tone + '">' + esc(o.status_title || o.status) + '</span>'
        + '</div></div>'

        + (items ? '<div class="card"><h2>Состав</h2>' + items + '</div>' : '')

        + '<div class="card">'
        +   line('Товары', money(o.subtotal, o.currency))
        +   (Number(o.discount) ? line('Скидка', '−' + money(o.discount, o.currency)) : '')
        +   line('Доставка', o.awaiting_quote
                ? '<span class="muted small">оператор посчитает</span>'
                : money(o.delivery, o.currency))
        +   '<div class="hr"></div>'
        +   line('Итого', money(o.total, o.currency), true)
        + '</div>'

        + (o.delivery_summary
            ? '<div class="card"><div class="row" style="align-items:flex-start">'
              + '<span class="ic-box">' + icon('truck') + '</span>'
              + '<div class="grow"><div>' + esc(o.delivery_summary) + '</div>'
              + (o.tracking_number
                  ? '<div class="muted small" style="margin-top:4px">Трек-номер '
                    + '<span class="mono">' + esc(o.tracking_number) + '</span></div>' : '')
              + '</div></div></div>'
            : '')

        + '<div class="stack">'
        + (o.payable ? '<button class="btn" id="pay">' + icon('card') + 'Оплатить</button>' : '')
        + (o.instruction_url
            ? '<button class="btn ghost" id="setup">' + icon('info') + 'Как подключить</button>' : '')
        + '</div>'
      );

      var pay = document.getElementById('pay');
      if (pay) {
        pay.addEventListener('click', function () {
          haptic('medium');
          pay.disabled = true;
          pay.innerHTML = icon('refresh') + 'Готовим…';
          api('/orders/' + view.id + '/pay', { method: 'POST', body: '{}' })
            .then(function (res) {
              if (!res.ok || !res.pay_url) { throw new Error(res.error || 'Счёт не создался'); }
              tg.openLink(res.pay_url);
              pay.disabled = false;
              pay.innerHTML = icon('card') + 'Оплатить';
            })
            .catch(function (err) {
              pay.disabled = false;
              pay.innerHTML = icon('card') + 'Оплатить';
              tg.showAlert(err.message || String(err));
            });
        });
      }
      var setup = document.getElementById('setup');
      if (setup) {
        setup.addEventListener('click', function () { tg.openLink(o.instruction_url); });
      }
    });
  };

  /* --- Каталог и покупка -------------------------------------------------- */

  views.catalog = function () {
    return api('/catalog').then(function (d) {
      var items = (d.products || []).map(function (p) {
        return '<div class="card prod">'
          + (p.photo_url ? '<img src="' + esc(p.photo_url) + '" alt="">' : '')
          + '<div class="row"><b class="grow">' + esc(p.title) + '</b>'
          + '<span class="price">' + money(p.price, d.currency) + '</span></div>'
          + (p.description
              ? '<div class="muted small" style="margin-top:7px">' + esc(p.description) + '</div>'
              : '')
          + '<button class="btn" data-buy="' + esc(p.id) + '" style="margin-top:12px">'
          + icon('cart') + 'Купить</button>'
          + '</div>';
      }).join('');

      show('<h1>Каталог</h1>'
        + (items || empty('box', 'Пока пусто', 'Товары появятся здесь.')));

      screen.querySelectorAll('[data-buy]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          haptic('medium');
          go({ name: 'buy', productId: Number(btn.dataset.buy) });
        });
      });
    });
  };

  // Ответы формы живут между экранами: клиент уходит на подтверждение и
  // возвращается поправить телефон, и терять всё введённое при этом нельзя.
  var form = { name: '', phone: '', city: '', address: '', promo: '', comment: '',
               speed: '', method: '', toPvz: true };

  views.buy = function (view) {
    return Promise.all([api('/catalog'), api('/delivery')]).then(function (res) {
      var product = (res[0].products || []).filter(function (p) {
        return p.id === view.productId;
      })[0];
      if (!product) { throw new Error('Этого товара больше нет в продаже'); }

      var currency = res[0].currency;
      var speeds = res[1].options || [];
      var carriers = res[1].carriers || [];
      if (!form.speed && speeds.length) { form.speed = speeds[0].speed; }
      if (!form.method && carriers.length) { form.method = carriers[0].method; }

      function field(key, label, placeholder, hint) {
        return '<label class="field"><span>' + esc(label) + '</span>'
          + '<input class="input" data-f="' + key + '" value="' + esc(form[key]) + '"'
          + ' placeholder="' + esc(placeholder) + '" autocomplete="off">'
          + '<div class="hint muted" data-hint="' + key + '">' + esc(hint || '') + '</div></label>';
      }

      function choice(group, value, checked, title, note) {
        return '<label class="choice"><input type="radio" name="' + group + '" value="'
          + esc(value) + '"' + (checked ? ' checked' : '') + '>'
          + '<div class="box"><span class="tick"></span><span class="grow">'
          + '<span>' + esc(title) + '</span>'
          + (note ? '<span class="muted small" style="display:block;margin-top:2px">'
                    + esc(note) + '</span>' : '')
          + '</span></div></label>';
      }

      show(
        '<h1>Оформление</h1>'

        + '<div class="card tight"><div class="row">'
        +   '<span class="ic-box">' + icon('box') + '</span>'
        +   '<div class="grow"><b>' + esc(product.title) + '</b>'
        +     '<div class="muted small">Роутер с настроенным доступом</div></div>'
        +   '<span class="price">' + money(product.price, currency) + '</span>'
        + '</div></div>'

        + '<div class="card">'
        +   '<h2>Получатель</h2>'
        +   field('name', 'Фамилия и имя', 'Иванов Иван')
        +   field('phone', 'Телефон', '+7 900 123-45-67')
        +   field('city', 'Город', 'Москва')
        + '</div>'

        + (speeds.length
            ? '<div class="card"><h2>Скорость доставки</h2>'
              + speeds.map(function (s) {
                  return choice('speed', s.speed, form.speed === s.speed, s.title, s.description);
                }).join('')
              + '</div>'
            : '')

        + '<div class="card">'
        +   '<h2>Куда везти</h2>'
        +   choice('where', 'pvz', form.toPvz, 'В пункт выдачи', 'Заберёте сами, обычно дешевле')
        +   choice('where', 'door', !form.toPvz, 'Курьером на адрес', 'Привезут до двери')
        +   '<div id="carriers" style="margin-top:12px">'
        +     (carriers.length
                  ? '<div class="muted small" style="margin-bottom:8px">Перевозчик</div>'
                    + carriers.map(function (c) {
                        return choice('carrier', c.method, form.method === c.method, c.title, '');
                      }).join('')
                  : '')
        +   '</div>'
        +   '<div style="margin-top:12px">'
        +     field('address', 'Адрес', 'Улица, дом, квартира')
        +   '</div>'
        + '</div>'

        + '<div class="card">'
        +   field('promo', 'Промокод', 'если есть')
        +   '<label class="field"><span>Комментарий</span>'
        +     '<textarea class="input" data-f="comment" placeholder="Что-то важное для курьера">'
        +       esc(form.comment) + '</textarea></label>'
        + '</div>'

        + '<button class="btn" id="next">' + icon('chev-r') + 'Посчитать и продолжить</button>'
        + '<div class="muted tiny center" style="margin-top:10px">Цену доставки назовёт '
        + 'оператор после оформления: она зависит от города и габаритов.</div>'
      );

      function pvzMode() { return form.toPvz; }

      function applyMode() {
        var carriersBox = document.getElementById('carriers');
        carriersBox.style.display = pvzMode() ? '' : 'none';
        var addr = screen.querySelector('[data-f="address"]');
        addr.placeholder = pvzMode() ? 'Адрес пункта выдачи' : 'Улица, дом, квартира';
        addr.parentNode.querySelector('span').textContent = pvzMode()
          ? 'Пункт выдачи' : 'Адрес доставки';
      }
      applyMode();

      screen.querySelectorAll('[data-f]').forEach(function (input) {
        input.addEventListener('input', function () { form[input.dataset.f] = input.value; });
        input.addEventListener('blur', function () { checkField(input); });
      });

      screen.querySelectorAll('input[name="speed"]').forEach(function (r) {
        r.addEventListener('change', function () { form.speed = r.value; haptic(); });
      });
      screen.querySelectorAll('input[name="carrier"]').forEach(function (r) {
        r.addEventListener('change', function () { form.method = r.value; haptic(); });
      });
      screen.querySelectorAll('input[name="where"]').forEach(function (r) {
        r.addEventListener('change', function () {
          form.toPvz = r.value === 'pvz';
          haptic();
          applyMode();
        });
      });

      document.getElementById('next').addEventListener('click', function () {
        var btn = this;
        haptic('medium');
        btn.disabled = true;
        // Проверяем поля там же, где их проверит заказ: правила живут на
        // сервере, и повторять их здесь значит однажды разойтись с ними.
        var checks = ['name', 'phone', 'city', 'address'].map(function (key) {
          return checkField(screen.querySelector('[data-f="' + key + '"]'));
        });
        Promise.all(checks).then(function (results) {
          btn.disabled = false;
          if (results.indexOf(false) >= 0) {
            tg.showAlert('Проверьте поля, отмеченные красным.');
            return;
          }
          go({ name: 'confirm', productId: view.productId });
        });
      });
    });
  };

  // Возвращает промис с true/false. Поле, которое сервер причесал (телефон
  // к единому виду), заменяем причёсанным — заказ уедет ровно с тем, что
  // клиент видит на экране.
  function checkField(input) {
    if (!input) { return Promise.resolve(true); }
    var key = input.dataset.f;
    var hint = screen.querySelector('[data-hint="' + key + '"]');
    var field = key === 'address' ? (form.toPvz ? 'pvz' : 'address') : key;
    var value = input.value.trim();

    if (!value) {
      input.classList.add('bad');
      if (hint) { hint.textContent = 'Заполните поле'; hint.className = 'hint err'; }
      return Promise.resolve(false);
    }

    return api('/validate', {
      method: 'POST', body: JSON.stringify({ field: field, value: value })
    }).then(function (res) {
      if (!res.ok) {
        input.classList.add('bad');
        if (hint) { hint.textContent = res.error || 'Не подходит'; hint.className = 'hint err'; }
        return false;
      }
      input.classList.remove('bad');
      input.value = res.value;
      form[key] = res.value;
      if (hint) { hint.textContent = ''; hint.className = 'hint muted'; }
      return true;
    }).catch(function () {
      // Недоступная проверка не должна запирать оформление: сервер проверит
      // ещё раз при создании заказа и там уже откажет по делу.
      return true;
    });
  }

  views.confirm = function (view) {
    var payload = {
      product_id: view.productId,
      name: form.name, phone: form.phone, city: form.city,
      address: form.address, promo_code: form.promo, comment: form.comment,
      delivery_speed: form.speed, delivery_method: form.method,
      delivery_to_pvz: form.toPvz
    };

    return api('/orders/quote', { method: 'POST', body: JSON.stringify(payload) })
      .then(function (q) {
        if (!q.ok) { throw new Error(q.error || 'Не получилось посчитать'); }

        function line(label, value, bold) {
          return '<div class="row"><span class="muted small">' + esc(label) + '</span>'
            + '<span' + (bold ? ' class="big"' : '') + '>' + value + '</span></div>';
        }

        show(
          '<h1>Проверьте заказ</h1>'

          + '<div class="card">'
          +   (q.product ? '<div class="row"><span class="grow ellip">' + esc(q.product.title)
                  + '</span><span>' + money(q.subtotal, q.currency) + '</span></div>' : '')
          +   (Number(q.discount)
                ? line('Скидка' + (q.promo ? ' · ' + esc(q.promo.code) : ''),
                       '−' + money(q.discount, q.currency))
                : '')
          +   line('Доставка', Number(q.delivery)
                ? money(q.delivery, q.currency)
                : '<span class="muted small">оператор посчитает</span>')
          +   '<div class="hr"></div>'
          +   line('К оплате', money(q.total, q.currency), true)
          + '</div>'

          + '<div class="card">'
          +   '<h2>Куда и кому</h2>'
          +   '<div class="row"><span class="muted small">Получатель</span><span>'
                + esc(form.name) + '</span></div>'
          +   '<div class="row"><span class="muted small">Телефон</span><span class="mono">'
                + esc(form.phone) + '</span></div>'
          +   '<div class="row"><span class="muted small">Город</span><span>'
                + esc(form.city) + '</span></div>'
          +   '<div class="row" style="align-items:flex-start">'
          +     '<span class="muted small">' + (form.toPvz ? 'Пункт выдачи' : 'Адрес') + '</span>'
          +     '<span style="text-align:right;max-width:62%">' + esc(form.address) + '</span></div>'
          + '</div>'

          + '<button class="btn" id="make">' + icon('check') + 'Оформить и оплатить</button>'
          + '<button class="btn quiet" id="edit" style="margin-top:8px">Изменить данные</button>'
          + '<div class="muted tiny center" style="margin-top:12px">Платёжная система добавит '
          + 'свою комиссию сверху — в сумму заказа она не входит.</div>'
        );

        document.getElementById('edit').addEventListener('click', function () { haptic(); back(); });

        document.getElementById('make').addEventListener('click', function () {
          var btn = this;
          haptic('medium');
          btn.disabled = true;
          btn.innerHTML = icon('refresh') + 'Оформляем…';
          api('/orders', { method: 'POST', body: JSON.stringify(payload) })
            .then(function (res) {
              if (!res.ok) { throw new Error(res.error || 'Заказ не оформился'); }
              // Заказ принят, даже если провайдер не дал ссылку: об этом надо
              // сказать прямо, иначе клиент оформит его второй раз.
              if (res.pay_url) { tg.openLink(res.pay_url); }
              else {
                tg.showAlert('Заказ принят. Ссылку на оплату пришлём — платёжная система '
                  + 'сейчас не ответила.');
              }
              stack = [];
              openTab('orders');
            })
            .catch(function (err) {
              btn.disabled = false;
              btn.innerHTML = icon('check') + 'Оформить и оплатить';
              tg.showAlert(err.message || String(err));
            });
        });
      });
  };

  /* --- Запуск ------------------------------------------------------------- */

  document.querySelectorAll('nav button').forEach(function (b) {
    b.addEventListener('click', function () { haptic(); openTab(b.dataset.tab); });
  });

  try { tg.BackButton.onClick(back); } catch (e) { /* старые клиенты */ }

  openTab('home');
})();
