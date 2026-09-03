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
    var splash = document.getElementById('splash');
    if (splash) { splash.classList.add('gone'); }
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

  // Цена за месяц — ориентир для сравнения сроков, а не счёт к оплате.
  // «349,83 ₽» заставляет вчитываться там, где нужно охватить взглядом.
  function perMonth(value, currency) {
    return money(Math.round(Number(value) || 0), currency);
  }

  function bytes(value) {
    var n = Number(value);
    if (!n || isNaN(n)) { return '—'; }
    var units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + ' ' + units[i];
  }

  // «10 минут назад» вместо даты: у показаний важен не момент, а давность.
  function ago(iso) {
    if (!iso) { return ''; }
    var then = new Date(iso);
    if (isNaN(then)) { return ''; }
    var mins = Math.floor((Date.now() - then.getTime()) / 60000);
    if (mins < 1) { return 'только что'; }
    if (mins < 60) { return mins + ' мин назад'; }
    var hours = Math.floor(mins / 60);
    if (hours < 24) { return hours + ' ч назад'; }
    return Math.floor(hours / 24) + ' сут назад';
  }

  function uptime(seconds) {
    var s = Number(seconds);
    if (!s || isNaN(s)) { return '—'; }
    var days = Math.floor(s / 86400);
    var hours = Math.floor((s % 86400) / 3600);
    return days ? days + ' сут ' + hours + ' ч' : hours + ' ч';
  }

  // Срок словами. Тарифы бывают и в месяцах, и в днях: у дневного `months`
  // равен нулю, и «0 мес.» под каждым сроком было именно этим.
  function planPeriod(p) {
    var months = Number(p.months || 0);
    var days = Number(p.extra_days || 0);
    if (months && days) { return months + ' мес. + ' + days + ' дн.'; }
    if (months) { return months + ' мес.'; }
    if (days) { return days + ' дн.'; }
    return '';
  }

  function planDays(p) {
    return Number(p.months || 0) * 30 + Number(p.extra_days || 0);
  }

  // Цена за месяц для сравнения сроков. Считаем из общего числа дней, а не
  // из поля тарифа: у дневного там лежит полная цена, и «300 ₽ в месяц»
  // у срока на 30 дней соседствовало бы с «1800 ₽ в месяц» у полугодового.
  // Меньше двух месяцев не приводим: делить тридцать дней на месяцы незачем.
  function planPerMonth(p, currency) {
    var days = planDays(p);
    if (days < 60) { return ''; }
    return perMonth(Number(p.price) / (days / 30), currency);
  }

  // Названия тарифов заводит оператор, и в них попадают эмодзи. В наборе,
  // где всё остальное — обводка одной толщины, они выглядят случайными.
  function planTitle(p) {
    return String(p.title || '').replace(/^[^\p{L}\p{N}]+/u, '').trim() || String(p.title || '');
  }

  // Самый выгодный срок — тот, у кого день дешевле всех, и только если он
  // такой один. При ровной сетке (300 / 600 / 900 / 1800 за 30 / 60 / 90 / 180)
  // выгоды нет ни у кого, и пометка была бы враньём.
  function bestPlan(plans) {
    var rates = plans.map(function (p) {
      var days = planDays(p);
      return days ? Number(p.price) / days : Infinity;
    });
    if (rates.length < 2) { return null; }
    var min = Math.min.apply(null, rates);
    var max = Math.max.apply(null, rates);
    if (!(min < max)) { return null; }
    var first = rates.indexOf(min);
    return rates.lastIndexOf(min) === first ? plans[first] : null;
  }

  // Отказы приходят кодом, чтобы их можно было разобрать; клиенту нужен текст.
  var REASONS = {
    offline: 'Роутер сейчас не на связи — команде некуда прийти.',
    too_often: 'Слишком часто. Подождите и попробуйте ещё раз.',
    unreachable: 'Роутер не ответил. Попробуйте позже.'
  };

  // Копирование: в браузере Telegram обычный буфер обмена доступен не везде,
  // и на отказ надо ответить честно, а не молчанием — MAC называют поддержке.
  function copyText(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(text).then(
          function () { return true; }, function () { return false; });
      }
    } catch (e) { /* разберёмся запасным путём */ }
    try {
      var area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      var done = document.execCommand('copy');
      document.body.removeChild(area);
      return Promise.resolve(done);
    } catch (e) {
      return Promise.resolve(false);
    }
  }

  // Локальный адрес роутера. Открываем во внешнем браузере: встроенный
  // браузер Telegram в домашнюю сеть не ходит, и клиент видел бы пустую
  // страницу, не понимая, в чём дело. Адрес заодно кладём в буфер — если
  // и внешний браузер откажется, его можно вставить руками.
  function openLocal(url) {
    if (!url) { return; }
    copyText(url);
    try {
      tg.openLink(url, { try_instant_view: false });
    } catch (e) {
      tg.showAlert('Откройте в браузере: ' + url);
    }
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

  // Заставку убираем, когда первый экран отрисован, но не раньше, чем через
  // 600 мс от запуска: мелькнувшая на мгновение, она читается как сбой.
  //
  // Убираем и на ошибке тоже: экран с причиной клиенту нужнее заставки,
  // а висящая поверх него навсегда — худшее из возможного.
  var startedAt = Date.now();
  var splashGone = false;

  function hideSplash() {
    if (splashGone) { return; }
    splashGone = true;
    var wait = Math.max(0, 600 - (Date.now() - startedAt));
    setTimeout(function () {
      var splash = document.getElementById('splash');
      if (splash) { splash.classList.add('gone'); }
    }, wait);
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
      .catch(failed)
      .then(hideSplash, hideSplash);
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

  // Заказы — не набор карточек, а одна группа строк с волосяными
  // разделителями: карточка на каждую строку читается как список без начала
  // и конца, а группа — как один блок, где видно, сколько в нём всего.
  function orderRow(o) {
    var tone = ORDER_TONE[o.status] || 'off';
    return '<button class="item" data-order="' + esc(o.id) + '">'
      + '<span class="grow">'
      +   '<span class="mono" style="display:block;font-size:14px">'
      +     esc(o.number || ('#' + o.id)) + '</span>'
      +   '<span style="display:block;margin-top:6px"><span class="pill ' + tone + '">'
      +     esc(o.status_title || o.status) + '</span></span>'
      + '</span>'
      + '<span style="text-align:right">'
      +   '<span style="display:block;font-weight:650">' + money(o.total, o.currency) + '</span>'
      +   '<span class="subtle tiny" style="display:block;margin-top:5px">'
      +     date(o.created_at) + '</span>'
      + '</span>'
      + '<span class="chev">' + icon('chev-r') + '</span>'
      + '</button>';
  }

  function orderList(items) {
    return '<div class="list">' + items.map(orderRow).join('') + '</div>';
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

  // Сколько дней осталось и какая часть срока прошла. Дата окончания одна
  // ничего не сообщает: «до 1 октября» читается как «ещё долго» и первого
  // сентября, и тридцатого.
  function termLeft(sub) {
    if (!sub || !sub.until) { return null; }
    var until = new Date(sub.until);
    if (isNaN(until)) { return null; }
    var left = Math.ceil((until.getTime() - Date.now()) / 86400000);
    var since = sub.since ? new Date(sub.since) : null;
    var total = since && !isNaN(since)
      ? Math.round((until.getTime() - since.getTime()) / 86400000)
      : 0;
    return {
      days: left,
      // Доля прошедшего. Без начала срока не выдумываем: полоса тогда
      // отмеряет месяц — столько же, сколько самый короткий тариф.
      spent: total > 0
        ? Math.min(100, Math.max(0, Math.round((1 - left / total) * 100)))
        : Math.min(100, Math.max(0, Math.round((1 - left / 30) * 100))),
      tone: left <= 3 ? 'bad' : (left <= 10 ? 'warn' : 'ok')
    };
  }

  function daysWord(n) {
    var abs = Math.abs(n) % 100;
    var tail = abs % 10;
    if (abs > 10 && abs < 20) { return 'дней'; }
    if (tail === 1) { return 'день'; }
    if (tail >= 2 && tail <= 4) { return 'дня'; }
    return 'дней';
  }

  // Предложение купить есть на профиле всегда — меняется только повод.
  // Первый роутер продаётся тому, у кого его нет; второй — тому, у кого он
  // уже работает: родителям, на дачу, в съёмную квартиру.
  function buyOffer(hasRouter) {
    return '<div class="offer">'
      + '<h3>' + (hasRouter ? 'Ещё один роутер' : 'Роутер с доступом') + '</h3>'
      + '<p>' + (hasRouter
          ? 'Родителям, на дачу или в съёмную квартиру. Приедет настроенным '
            + 'так же — включить в розетку и всё.'
          : 'Включили в розетку — зарубежные сервисы открываются на всех '
            + 'устройствах дома. Настраивать нечего.')
      + '</p>'
      + '<button class="btn" id="to-catalog">' + icon('box')
      + (hasRouter ? 'Выбрать модель' : 'Смотреть роутеры') + '</button>'
      + '</div>';
  }

  views.home = function () {
    return api('/home').then(function (d) {
      var sub = d.subscription || {};
      var active = sub.status === 'active';
      var user = d.user || {};
      var recent = (d.orders || []).slice(0, 3);
      var term = active ? termLeft(sub) : null;

      function bindCatalog() {
        var toCatalog = document.getElementById('to-catalog');
        if (toCatalog) {
          toCatalog.addEventListener('click', function () { haptic('medium'); openTab('catalog'); });
        }
      }

      // Ни подписки, ни роутера, ни заказов — человек пришёл впервые. Ему
      // нечего продлевать, и «подписка не активна» с кнопкой продления
      // выглядит поломкой. Показываем, что тут вообще продаётся.
      if (!active && !d.router_available && !recent.length) {
        return api('/pitch').then(function (p) {
          show(
            '<div class="hero"><h1>' + esc(p.hero_title || 'Роутер с доступом') + '</h1>'
            + (p.hero_subtitle ? '<p>' + esc(p.hero_subtitle) + '</p>' : '') + '</div>'
            + '<div class="list leading">'
            + (p.features || []).slice(0, 3).map(function (f) {
                return '<div class="item" style="align-items:flex-start">'
                  + '<span class="ic-box">' + icon('check') + '</span>'
                  + '<span class="grow"><b>' + esc(f.title) + '</b>'
                  + '<span class="muted small" style="display:block;margin-top:3px">'
                  + esc(f.text) + '</span></span></div>';
              }).join('')
            + '</div>'
            + '<button class="btn" id="to-catalog" style="margin-top:14px">'
            + icon('box') + 'Посмотреть роутеры</button>'
          );
          bindCatalog();
        });
      }

      show(
        '<h1>' + esc(user.name || 'Профиль') + '</h1>'

        // Подписка — первым и крупно: это то, за чем сюда заходят повторно,
        // и то, что приносит деньги после первой покупки.
        + (term
            ? '<div class="card"><div class="term ' + term.tone + '">'
              +   '<div class="row" style="align-items:flex-end">'
              +     '<div><div class="muted small">Подписка активна</div>'
              +       '<div class="num" style="margin-top:4px">' + term.days + '</div></div>'
              +     '<div style="text-align:right">'
              +       '<div class="muted small">' + daysWord(term.days) + ' осталось</div>'
              +       '<div class="small" style="margin-top:4px">до ' + date(sub.until) + '</div>'
              +     '</div>'
              +   '</div>'
              +   '<div class="track"><div class="fill" style="width:'
              +     (100 - term.spent) + '%"></div></div>'
              + '</div></div>'
            : '<div class="card"><div class="row">'
              +   '<span class="ic-box">' + icon('shield') + '</span>'
              +   '<div class="grow"><div class="muted small">Подписка</div>'
              +     '<div style="margin-top:2px">не активна</div></div>'
              +   '<span class="pill off">нет</span>'
              + '</div></div>')

        + '<button class="btn' + (term && term.tone !== 'ok' ? '' : ' ghost') + '" id="renew">'
        + icon('card') + (term && term.tone !== 'ok' ? 'Продлить сейчас' : 'Продлить подписку')
        + '</button>'

        + (d.router_available
            ? '<div class="list leading" style="margin-top:12px">'
              + '<button class="item" id="to-router">'
              + '<span class="ic-box">' + icon('router') + '</span>'
              + '<span class="grow"><b>Мой роутер</b>'
              + '<span class="muted small" style="display:block">Связь, показания, обновление</span>'
              + '</span><span class="chev">' + icon('chev-r') + '</span></button></div>'
            : '')

        + '<div style="margin-top:14px">' + buyOffer(d.router_available) + '</div>'

        + (recent.length
            ? '<div class="sec">Последние заказы</div>' + orderList(recent)
              + '<button class="btn quiet" id="all-orders" style="padding:6px">'
              + 'Все заказы' + icon('chev-r') + '</button>'
            : '')
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
      bindCatalog();
      bindOrderRows();
    });
  };

  /* --- Подписка ----------------------------------------------------------- */

  views.renew = function () {
    return api('/renew').then(function (d) {
      var sub = d.subscription || {};

      // Из трёх одинаковых на вид сроков человек выбирает дольше всех и чаще
      // не выбирает вовсе. Пометка снимает этот выбор: она не назначена
      // руками, а посчитана по цене за месяц — иначе разъедется с ценами
      // при первой же правке тарифа.
      var best = bestPlan(d.plans || []);

      var plans = (d.plans || []).map(function (p) {
        var rate = planPerMonth(p, d.currency);
        return '<div class="plan"><div class="grow">'
          + '<div><b>' + esc(planTitle(p)) + '</b>'
          + (best && best.id === p.id ? ' <span class="best">выгоднее всего</span>' : '')
          + '</div>'
          + '<div class="muted small">' + esc(planPeriod(p))
          + (rate ? ' · ' + rate + ' в месяц' : '')
          + '</div></div>'
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

      // Показания плитками, а не строками: четыре пары «название — значение»
      // в столбик читаются как накладная, а взгляду нужно охватить их разом.
      function tile(iconName, label, value) {
        return '<div class="card tight" style="margin:0">'
          + '<div class="row"><span class="subtle small">' + esc(label) + '</span>'
          + '<span class="subtle">' + icon(iconName) + '</span></div>'
          + '<div style="font-size:19px;font-weight:700;letter-spacing:-.4px;margin-top:6px">'
          + value + '</div></div>';
      }

      show(
        '<h1>Мой роутер</h1>'
        + picker
        + '<div class="card">'
        +   '<div class="row"><div class="grow">'
        // Модель сверху, MAC под ней: человек ищет глазами «какой это из моих»,
        // а не шестнадцать знаков. Незнакомую модель не подписываем вовсе —
        // строка «модель не указана» сообщает клиенту о нашей недоработке.
        +     (r.model ? '<div><b>' + esc(r.model) + '</b></div>' : '')
        +     '<button id="mac" style="display:flex;align-items:center;gap:7px;background:none;'
        +       'border:0;padding:0;font:inherit;color:var(--muted);cursor:pointer">'
        +       '<span class="mono small">' + esc(r.mac) + '</span>'
        +       '<span class="subtle">' + icon('copy') + '</span></button></div>'
        +     '<span class="pill ' + (r.online ? 'ok' : 'off') + '"><i class="dot"></i>'
        +       (r.online ? 'на связи' : 'молчит') + '</span></div>'
        +   (r.until
              ? '<div class="hr"></div>'
                + '<div class="row"><span class="muted small">Подписка до</span>'
                + '<span class="big" style="font-size:19px">' + date(r.until) + '</span></div>'
              : '')
        + '</div>'

        + '<div class="sec" style="display:flex;justify-content:space-between">'
        +   '<span>Показания</span>'
        +   (r.polled_at ? '<span class="subtle">' + esc(ago(r.polled_at)) + '</span>' : '')
        + '</div>'
        + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">'
        +   tile('wifi', 'Устройств', esc(r.clients == null ? '—' : r.clients))
        +   tile('clock', 'Аптайм', esc(uptime(r.uptime_sec)))
        +   tile('gauge', 'Загрузка', r.cpu_pct == null ? '—' : esc(r.cpu_pct) + '%')
        +   tile('swap', 'Трафик', bytes(r.rx_bytes) + '<span class="subtle" '
              + 'style="font-size:13px;font-weight:400"> / ' + bytes(r.tx_bytes) + '</span>')
        + '</div>'

        // Порядок по частоте: перезагрузка — первое, что советует поддержка,
        // и до сих пор ради неё писали в бот и ждали оператора.
        + '<div class="stack">'
        +   '<button class="btn ghost" id="reboot">' + icon('power') + 'Перезагрузить роутер</button>'
        +   '<button class="btn quiet" id="upd">' + icon('download') + 'Обновить прошивку</button>'
        +   (d.support
                ? '<button class="btn quiet" id="support">' + icon('chat')
                  + 'Написать в поддержку</button>' : '')
        + '</div>'
        + '<div class="muted tiny center" style="margin-top:12px">После перезагрузки роутер '
        + 'молчит около минуты. Обновление идёт в фоне и занимает несколько минут.</div>'

        // Панель и инструкция живут на самом роутере, по локальному адресу.
        // Снаружи его не существует вовсе, поэтому кнопки отделены от прочих
        // и подписаны: иначе клиент нажимает их из метро и решает, что сломано.
        + '<div class="sec" style="margin-top:24px">Из домашней сети</div>'
        + '<div class="list">'
        +   (d.panel_url
                ? '<button class="item" id="panel"><span class="grow"><b>Панель роутера</b>'
                  + '<span class="muted small" style="display:block">Wi-Fi, пароль, устройства</span>'
                  + '</span><span class="chev">' + icon('chev-r') + '</span></button>'
                : '')
        +   (d.instruction_url
                ? '<button class="item" id="help"><span class="grow"><b>Инструкция</b>'
                  + '<span class="muted small" style="display:block">Что делать, если что-то '
                  + 'не работает</span></span><span class="chev">' + icon('chev-r')
                  + '</span></button>'
                : '')
        + '</div>'
        + '<div class="muted tiny" style="margin:-4px 2px 0">Открываются, только когда телефон '
        + 'подключён к Wi-Fi этого роутера: адрес '
        + '<span class="mono">' + esc(String(d.panel_url || '').replace(/^https?:\/\//, '')
            .replace(/\/$/, '')) + '</span> существует лишь в вашей домашней сети. '
        + 'Из мобильного интернета они не откроются.</div>'
      );

      screen.querySelectorAll('[data-dev]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          haptic(); go({ name: 'router', id: Number(btn.dataset.dev) }, true);
        });
      });

      var macBtn = screen.querySelector('#mac');
      if (macBtn) {
        macBtn.addEventListener('click', function () {
          haptic();
          copyText(r.mac).then(function (done) {
            tg.showAlert(done ? 'MAC скопирован: ' + r.mac
                              : 'Скопировать не вышло. MAC: ' + r.mac);
          });
        });
      }

      var support = screen.querySelector('#support');
      if (support) {
        support.addEventListener('click', function () {
          haptic();
          // Ссылкой на бот, а не на человека: клиент разговаривает с ботом,
          // а оператор не светит свой аккаунт каждому покупателю.
          var contact = String(d.support || '').trim().replace(/^@/, '');
          tg.openTelegramLink('https://t.me/' + encodeURIComponent(contact));
        });
      }

      var reboot = screen.querySelector('#reboot');
      if (reboot) {
        reboot.addEventListener('click', function () {
          haptic('medium');
          tg.showConfirm('Перезагрузить роутер? Интернет пропадёт примерно на минуту.',
            function (yes) {
              if (!yes) { return; }
              reboot.disabled = true;
              reboot.innerHTML = icon('refresh') + 'Отправляем…';
              api('/router/reboot', {
                method: 'POST', body: JSON.stringify({ device_id: r.id })
              }).then(function (res) {
                if (!res.ok) { throw new Error(REASONS[res.error] || 'Роутер не ответил'); }
                reboot.innerHTML = icon('check') + 'Перезагружается';
                tg.showAlert('Команда ушла. Роутер вернётся на связь примерно через минуту.');
              }).catch(function (err) {
                reboot.disabled = false;
                reboot.innerHTML = icon('power') + 'Перезагрузить роутер';
                tg.showAlert(err.message || String(err));
              });
            });
        });
      }

      var help = screen.querySelector('#help');
      if (help) {
        // Обработчик, а не onclick в разметке: адрес приезжает из базы, и одна
        // кавычка в нём разломала бы кнопку.
        help.addEventListener('click', function () { haptic(); openLocal(d.instruction_url); });
      }

      var panel = screen.querySelector('#panel');
      if (panel) {
        panel.addEventListener('click', function () { haptic(); openLocal(d.panel_url); });
      }

      document.getElementById('upd').addEventListener('click', function () {
        var btn = this;
        haptic('medium');
        btn.disabled = true;
        btn.innerHTML = icon('refresh') + 'Отправляем…';
        api('/router/update', {
          method: 'POST', body: JSON.stringify({ device_id: r.id })
        }).then(function (res) {
          if (!res.ok) { throw new Error(REASONS[res.error] || 'Роутер не ответил'); }
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
      var items = d.orders || [];
      show('<h1>Заказы</h1>'
        + (items.length
            ? orderList(items)
            : empty('receipt', 'Заказов нет', 'Оформленные заказы появятся здесь.')));
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
    // Заголовок, выгоды, шаги и вопросы приходят с витрины — те же, что на
    // сайте. Свой текст здесь завёл бы второй набор обещаний: поправив цену
    // или условие на сайте, оператор оставил бы в приложении прежние.
    return api('/pitch').then(function (d) {
      var items = (d.products || []).map(function (p) {
        var specs = (p.specs || []).map(function (pair) {
          return '<div><dt>' + esc(pair[0]) + '</dt><dd>' + esc(pair[1]) + '</dd></div>';
        }).join('');

        return '<div class="card prod">'
          + (p.photo_url
              ? '<div class="shot"><img src="' + esc(p.photo_url) + '" alt=""></div>' : '')
          + '<div class="row"><b class="grow" style="font-size:17px">' + esc(p.title) + '</b>'
          +   (p.in_stock
                ? '<span class="pill ok"><i class="dot"></i>в наличии</span>'
                : (p.preorder ? '<span class="pill warn">под заказ</span>'
                              : '<span class="pill off">нет в наличии</span>'))
          + '</div>'
          + (p.subtitle
              ? '<div class="muted small" style="margin-top:6px">' + esc(p.subtitle) + '</div>'
              : '')
          + (p.description
              ? '<div class="small" style="margin-top:9px;color:var(--muted)">'
                + esc(p.description) + '</div>'
              : '')
          + (specs ? '<div class="hr"></div><dl class="specs">' + specs + '</dl>' : '')
          + '<div class="hr"></div>'
          // Старая цена и выгода стоят над ценой, а не рядом: первое число,
          // которое видит человек, задаёт точку отсчёта для второго.
          + (p.old_price
              ? '<div class="row" style="margin-bottom:6px">'
                + '<span class="old">' + esc(p.old_price) + '</span>'
                + (p.saving ? '<span class="save">выгода ' + esc(p.saving) + '</span>' : '')
                + '</div>'
              : '')
          + '<div class="row">'
          +   '<span class="price">' + esc(p.price) + '</span>'
          +   '<button class="btn small" data-buy="' + esc(p.id) + '">'
          +     icon('cart') + 'Купить</button>'
          + '</div>'
          + '<div class="muted tiny" style="margin-top:9px">Подписка на выбранный срок '
          + 'входит в стоимость. Роутер приезжает настроенным.</div>'
          + '</div>';
      }).join('');

      var steps = (d.steps || []).map(function (s, i) {
        return '<div class="step"><span class="num">' + (i + 1) + '</span>'
          + '<div class="grow"><b>' + esc(s.title) + '</b>'
          + '<div class="muted small" style="margin-top:3px">' + esc(s.text) + '</div></div></div>';
      }).join('');

      var features = (d.features || []).map(function (f) {
        return '<div class="feat"><span class="ic-box">' + icon('check') + '</span>'
          + '<div class="grow"><b>' + esc(f.title) + '</b>'
          + '<div class="muted small" style="margin-top:3px">' + esc(f.text) + '</div></div></div>';
      }).join('');

      var plans = (d.plans || []).map(function (p) {
        return '<div class="plan"><div class="grow"><div><b>' + esc(p.title) + '</b></div>'
          + '<div class="muted small">' + esc(p.period || '') + '</div></div>'
          + '<div style="text-align:right"><div>' + esc(p.price) + '</div>'
          + (p.per_month
              ? '<div class="subtle tiny">' + esc(p.per_month) + ' в месяц</div>' : '')
          + '</div></div>';
      }).join('');

      var faq = (d.faq || []).map(function (q) {
        return '<details class="faq"><summary><span class="grow">' + esc(q.question) + '</span>'
          + icon('chev-r') + '</summary><p>' + esc(q.answer) + '</p></details>';
      }).join('');

      // Доводы стоят между заголовком и ценой намеренно: цифра без них
      // читается как «дорого за роутер», а после них — как «дешевле, чем
      // я плачу сейчас».
      var value = (d.value || []).map(function (v) {
        return '<div class="item" style="align-items:flex-start">'
          + '<span class="ic-box">' + icon('check') + '</span>'
          + '<span class="grow"><b>' + esc(v.title) + '</b>'
          + '<span class="muted small" style="display:block;margin-top:3px">'
          + esc(v.text) + '</span></span></div>';
      }).join('');

      show(
        (d.hero_title
          ? '<div class="hero"><h1>' + esc(d.hero_title) + '</h1>'
            + (d.hero_subtitle ? '<p>' + esc(d.hero_subtitle) + '</p>' : '') + '</div>'
          : '<h1>Каталог</h1>')
        + (value ? '<div class="list leading">' + value + '</div>' : '')
        + (items || empty('box', 'Пока пусто', 'Товары появятся здесь.'))
        // Стоимость подписки идёт сразу за ценой роутера: «а сколько платить
        // дальше» — первый вопрос, который возникает у человека после цены,
        // и оставлять его без ответа до конца страницы значит держать
        // сомнение всё время, пока он читает остальное.
        + (plans
            ? '<div class="sec">Сколько стоит потом</div><div class="card">' + plans + '</div>'
              + '<div class="muted tiny center">Роутер остаётся вам навсегда. '
              + 'Продлевается только подписка.</div>'
            : '')
        + (steps ? '<div class="sec">Как это работает</div><div class="card">' + steps + '</div>' : '')
        + (features ? '<div class="sec">Почему это удобно</div><div class="card">' + features + '</div>' : '')
        + (faq ? '<div class="sec">Вопросы</div><div class="card">' + faq + '</div>' : '')
        + (d.support_contact
            ? '<div class="muted tiny center" style="margin-top:16px">Остались вопросы — '
              + esc(d.support_contact) + '</div>'
            : '')
      );

      screen.querySelectorAll('[data-buy]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          haptic('medium');
          go({ name: 'buy', productId: Number(btn.dataset.buy) });
        });
      });
    });
  };

  // Ответы формы живут между шагами и экранами: клиент уходит проверять итог
  // и возвращается поправить телефон, и терять введённое при этом нельзя.
  var form = { planId: 0, name: '', phone: '', city: '', address: '', promo: '',
               comment: '', speed: '', method: '', toPvz: true };

  // Справочники спрашиваем один раз на сеанс: товары, сроки и доставка между
  // шагами не меняются, а лишний запрос на каждом шаге — это пустой экран
  // на плохой связи ровно там, где человек уже готов платить.
  var cache = {};

  function once(key, path) {
    if (cache[key]) { return Promise.resolve(cache[key]); }
    return api(path).then(function (d) { cache[key] = d; return d; });
  }

  var STEP_TITLES = ['Срок подписки', 'Куда и кому', 'Проверка заказа'];

  function stepsBar(n) {
    return '<div class="steps-bar">'
      + '<div class="label"><b>' + esc(STEP_TITLES[n - 1]) + '</b>'
      + '<span>Шаг ' + n + ' из 3</span></div>'
      + '<div class="track"><div class="fill" style="width:'
      + Math.round(n / STEP_TITLES.length * 100) + '%"></div></div></div>';
  }

  function productById(id) {
    return once('catalog', '/catalog').then(function (d) {
      var found = (d.products || []).filter(function (p) { return p.id === id; })[0];
      if (!found) { throw new Error('Этого товара больше нет в продаже'); }
      return { product: found, currency: d.currency };
    });
  }

  function productLine(p, currency) {
    return '<div class="card tight"><div class="row">'
      + '<span class="ic-box">' + icon('box') + '</span>'
      + '<div class="grow ellip"><b>' + esc(p.title) + '</b>'
      + '<div class="muted small">Настроен до отправки</div></div>'
      + '<span class="price" style="font-size:21px">' + money(p.price, currency) + '</span>'
      + '</div></div>';
  }

  views.buy = function (view) {
    var step = view.step || 1;
    if (step === 2) { return buyRecipient(view); }
    if (step === 3) { return buyConfirm(view); }
    return buyPlan(view);
  };

  // Шаг первый — не анкета, а выбор. Спросив сначала имя и телефон, мы бы
  // потребовали личные данные раньше, чем человек хоть на что-то согласился;
  // выбранный срок — это уже маленькое решение в пользу покупки, и следующий
  // шаг после него делается охотнее.
  function buyPlan(view) {
    return Promise.all([productById(view.productId), once('plans', '/plans')])
      .then(function (res) {
        var p = res[0].product;
        var currency = res[0].currency;
        var list = (res[1].plans || []).slice();

        if (!list.length) {
          // Без сроков заказ уйдёт с одним роутером — это не покупка сервиса.
          throw new Error('Сроки подписки сейчас недоступны. Напишите в поддержку.');
        }

        // «Выгоднее всего» считается по цене за месяц, а не назначается
        // руками: назначенная разъедется с ценами при первой правке тарифа.
        var best = bestPlan(list);

        if (!form.planId) {
          var preset = list.filter(function (x) { return x.is_default; })[0];
          form.planId = (preset || best || list[0]).id;
        }

        function planBox(x) {
          var rate = planPerMonth(x, currency);
          return '<label class="choice"><input type="radio" name="plan" value="' + esc(x.id) + '"'
            + (form.planId === x.id ? ' checked' : '') + '>'
            + '<div class="box"><span class="tick"></span><span class="grow">'
            + '<span class="row"><span><b>' + esc(planTitle(x)) + '</b>'
            + (best && best.id === x.id ? ' <span class="best">выгоднее всего</span>' : '')
            + '</span><span>' + money(x.price, currency) + '</span></span>'
            + '<span class="row" style="margin-top:3px">'
            + '<span class="muted small">' + esc(planPeriod(x)) + '</span>'
            + (rate ? '<span class="subtle small">' + rate + ' в месяц</span>' : '')
            + '</span></span></div></label>';
        }

        show(
          stepsBar(1)
          + productLine(p, currency)
          + '<div class="card">' + list.map(planBox).join('') + '</div>'
          + '<div class="muted tiny center" style="margin:-2px 0 14px">Срок входит в стоимость '
          + 'заказа. Дальше подписку можно продлевать любым сроком.</div>'
          + '<button class="btn" id="next">' + icon('chev-r') + 'Дальше</button>'
        );

        screen.querySelectorAll('input[name="plan"]').forEach(function (r) {
          r.addEventListener('change', function () { form.planId = Number(r.value); haptic(); });
        });
        document.getElementById('next').addEventListener('click', function () {
          haptic('medium');
          go({ name: 'buy', productId: view.productId, step: 2 });
        });
      });
  }

  function buyRecipient(view) {
    return Promise.all([productById(view.productId), once('delivery', '/delivery')])
      .then(function (res) {
        var speeds = res[1].options || [];
        var carriers = res[1].carriers || [];
        if (!form.speed && speeds.length) { form.speed = speeds[0].speed; }
        if (!form.method && carriers.length) { form.method = carriers[0].method; }

        function field(key, label, placeholder, type) {
          return '<label class="field"><span>' + esc(label) + '</span>'
            + '<input class="input" data-f="' + key + '" value="' + esc(form[key]) + '"'
            + ' placeholder="' + esc(placeholder) + '" autocomplete="off"'
            + (type ? ' inputmode="' + type + '"' : '') + '>'
            + '<div class="hint muted" data-hint="' + key + '"></div></label>';
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
          stepsBar(2)
          + '<div class="card">'
          +   '<h2>Получатель</h2>'
          +   field('name', 'Фамилия и имя', 'Иванов Иван')
          +   field('phone', 'Телефон', '+7 900 123-45-67', 'tel')
          +   field('city', 'Город', 'Москва')
          + '</div>'
          + (speeds.length
              ? '<div class="card"><h2>Скорость доставки</h2>'
                + speeds.map(function (s) {
                    return choice('speed', s.speed, form.speed === s.speed, s.title, s.description);
                  }).join('') + '</div>'
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
          +   '<div style="margin-top:12px">' + field('address', 'Адрес', '') + '</div>'
          + '</div>'
          + '<button class="btn" id="next">' + icon('chev-r') + 'К проверке</button>'
          + '<div class="muted tiny center" style="margin-top:10px">Цену доставки назовёт '
          + 'оператор после оформления: она зависит от города и габаритов.</div>'
        );

        function applyMode() {
          document.getElementById('carriers').style.display = form.toPvz ? '' : 'none';
          var addr = screen.querySelector('[data-f="address"]');
          addr.placeholder = form.toPvz ? 'Адрес пункта выдачи' : 'Улица, дом, квартира';
          addr.parentNode.querySelector('span').textContent = form.toPvz
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
            form.toPvz = r.value === 'pvz'; haptic(); applyMode();
          });
        });

        document.getElementById('next').addEventListener('click', function () {
          var btn = this;
          haptic('medium');
          btn.disabled = true;
          // Проверяем теми же правилами, что и заказ: повтори мы их здесь,
          // они разошлись бы, и перевозчик не дозвонился бы по телефону,
          // который мы приняли.
          var checks = ['name', 'phone', 'city', 'address'].map(function (key) {
            return checkField(screen.querySelector('[data-f="' + key + '"]'));
          });
          Promise.all(checks).then(function (results) {
            btn.disabled = false;
            if (results.indexOf(false) >= 0) {
              var bad = screen.querySelector('.input.bad');
              if (bad) { bad.scrollIntoView({ block: 'center' }); bad.focus(); }
              return;
            }
            go({ name: 'buy', productId: view.productId, step: 3 });
          });
        });
      });
  }

  // Возвращает промис с true/false. Причёсанное сервером значение (телефон
  // к единому виду) подставляем обратно — заказ уедет ровно с тем, что клиент
  // видит на экране.
  function checkField(input) {
    if (!input) { return Promise.resolve(true); }
    var key = input.dataset.f;
    var hint = screen.querySelector('[data-hint="' + key + '"]');
    var field = key === 'address' ? (form.toPvz ? 'pvz' : 'address') : key;
    var value = input.value.trim();

    function complain(text) {
      input.classList.add('bad');
      if (hint) { hint.textContent = text; hint.className = 'hint err'; }
      return false;
    }

    if (!value) { return Promise.resolve(complain('Заполните поле')); }

    return api('/validate', {
      method: 'POST', body: JSON.stringify({ field: field, value: value })
    }).then(function (res) {
      if (!res.ok) { return complain(res.error || 'Не подходит'); }
      input.classList.remove('bad');
      input.value = res.value;
      form[key] = res.value;
      if (hint) { hint.textContent = ''; hint.className = 'hint muted'; }
      return true;
    }).catch(function () {
      // Недоступная проверка не должна запирать оформление: сервер проверит
      // ещё раз при создании заказа и там откажет по делу.
      return true;
    });
  }

  function orderPayload(productId) {
    return {
      product_id: productId, plan_id: form.planId,
      name: form.name, phone: form.phone, city: form.city, address: form.address,
      promo_code: form.promo, comment: form.comment,
      delivery_speed: form.speed, delivery_method: form.method,
      delivery_to_pvz: form.toPvz
    };
  }

  function buyConfirm(view) {
    var payload = orderPayload(view.productId);

    return api('/orders/quote', { method: 'POST', body: JSON.stringify(payload) })
      .then(function (q) {
        if (!q.ok) { throw new Error(q.error || 'Не получилось посчитать'); }

        function line(label, value, bold) {
          return '<div class="row"><span class="muted small">' + esc(label) + '</span>'
            + '<span' + (bold ? ' class="big"' : '') + '>' + value + '</span></div>';
        }

        show(
          stepsBar(3)
          + '<div class="card">'
          +   (q.product ? line(q.product.title, money(q.subtotal, q.currency)) : '')
          +   (q.plan ? line('Подписка · ' + q.plan.title, money(q.plan.price, q.currency)) : '')
          +   (Number(q.discount)
                ? line('Скидка' + (q.promo ? ' · ' + esc(q.promo.code) : ''),
                       '<span style="color:var(--ok)">−' + money(q.discount, q.currency) + '</span>')
                : '')
          +   line('Доставка', Number(q.delivery)
                ? money(q.delivery, q.currency)
                : '<span class="muted small">оператор посчитает</span>')
          +   '<div class="hr"></div>'
          +   line('К оплате', money(q.total, q.currency), true)
          + '</div>'

          // Промокод спрашиваем здесь, а не в начале: поле в первых шагах
          // сообщает «у кого-то есть скидка, а у вас нет» и отправляет
          // человека искать её вместо покупки.
          + '<div class="card tight">'
          +   '<label class="field" style="margin:0"><span>Промокод, если есть</span>'
          +     '<div class="split">'
          +       '<input class="input" data-f="promo" value="' + esc(form.promo) + '"'
          +         ' placeholder="Например, TITAN" autocomplete="off">'
          +       '<button class="btn ghost" id="promo" style="flex:0 0 auto;width:auto">'
          +         'Применить</button>'
          +     '</div></label>'
          + '</div>'

          + '<div class="card">'
          +   '<h2>Куда и кому</h2>'
          +   line('Получатель', esc(form.name))
          +   line('Телефон', '<span class="mono">' + esc(form.phone) + '</span>')
          +   line('Город', esc(form.city))
          +   '<div class="row" style="align-items:flex-start">'
          +     '<span class="muted small">' + (form.toPvz ? 'Пункт выдачи' : 'Адрес') + '</span>'
          +     '<span style="text-align:right;max-width:62%">' + esc(form.address) + '</span></div>'
          + '</div>'

          + '<button class="btn" id="make">' + icon('check') + 'Оформить и оплатить</button>'
          + '<div class="muted tiny center" style="margin-top:12px">Платёжная система добавит '
          + 'свою комиссию сверху — в сумму заказа она не входит.</div>'
        );

        var promoInput = screen.querySelector('[data-f="promo"]');
        promoInput.addEventListener('input', function () { form.promo = promoInput.value.trim(); });
        document.getElementById('promo').addEventListener('click', function () {
          haptic();
          go({ name: 'buy', productId: view.productId, step: 3 }, true);
        });

        document.getElementById('make').addEventListener('click', function () {
          var btn = this;
          haptic('medium');
          btn.disabled = true;
          btn.innerHTML = icon('refresh') + 'Оформляем…';
          api('/orders', { method: 'POST', body: JSON.stringify(payload) })
            .then(function (res) {
              if (!res.ok) { throw new Error(res.error || 'Заказ не оформился'); }
              form.promo = '';
              stack = [];
              go({ name: 'done', order: res.order || {}, payUrl: res.pay_url || '' });
              if (res.pay_url) { tg.openLink(res.pay_url); }
            })
            .catch(function (err) {
              btn.disabled = false;
              btn.innerHTML = icon('check') + 'Оформить и оплатить';
              tg.showAlert(err.message || String(err));
            });
        });
      });
  }

  // Последнее, что человек видит после оплаты, запоминается сильнее середины
  // пути. Раньше здесь был бросок в список заказов без единого слова — теперь
  // видно, что заказ принят, под каким номером и что произойдёт дальше.
  views.done = function (view) {
    var o = view.order || {};
    show(
      '<div class="done-mark">' + icon('check', 'ic-lg') + '</div>'
      + '<h1 class="center" style="margin-bottom:6px">Заказ принят</h1>'
      + '<div class="muted small center" style="margin-bottom:16px">Номер '
      + '<span class="mono">' + esc(o.number || ('#' + o.id)) + '</span></div>'

      + (view.payUrl
          ? '<button class="btn" id="pay">' + icon('card') + 'Оплатить</button>'
            + '<div class="muted tiny center" style="margin:10px 0 16px">Ссылка на оплату уже '
            + 'открылась. Если она закрылась — нажмите кнопку выше.</div>'
          : '<div class="card"><div class="row" style="align-items:flex-start">'
            + '<span class="ic-box">' + icon('info') + '</span>'
            + '<div class="grow">Платёжная система не ответила. Заказ принят — ссылку на '
            + 'оплату можно взять в карточке заказа через минуту.</div></div></div>')

      + '<div class="card"><h2>Что дальше</h2>'
      +   '<div class="step"><span class="num">1</span><div class="grow">'
      +     '<b>Оплата</b><div class="muted small">Как только деньги придут, статус заказа '
      +     'сменится сам.</div></div></div>'
      +   '<div class="step"><span class="num">2</span><div class="grow">'
      +     '<b>Доставка</b><div class="muted small">Оператор посчитает её и свяжется с вами. '
      +     'Трек-номер появится в карточке заказа.</div></div></div>'
      +   '<div class="step"><span class="num">3</span><div class="grow">'
      +     '<b>Включение</b><div class="muted small">Роутер приедет настроенным: воткнуть '
      +     'кабель провайдера и включить в розетку. Подписка включится сама.</div></div></div>'
      + '</div>'

      + '<button class="btn ghost" id="to-orders">' + icon('receipt') + 'Мои заказы</button>'
    );

    var pay = document.getElementById('pay');
    if (pay) { pay.addEventListener('click', function () { tg.openLink(view.payUrl); }); }
    document.getElementById('to-orders').addEventListener('click', function () {
      haptic(); openTab('orders');
    });
    return Promise.resolve();
  };

  /* --- Запуск ------------------------------------------------------------- */

  document.querySelectorAll('nav button').forEach(function (b) {
    b.addEventListener('click', function () { haptic(); openTab(b.dataset.tab); });
  });

  try { tg.BackButton.onClick(back); } catch (e) { /* старые клиенты */ }

  openTab('home');
})();
