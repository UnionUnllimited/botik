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
    // Вкладки внизу живут в разметке, а обработчики им навешиваются в самом
    // конце этого кода — до которого мы уже не дойдём. Оставленные на месте,
    // они нажимаются и не делают ничего: приложение выглядит зависшим.
    var nav = document.querySelector('nav');
    if (nav) { nav.remove(); }
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

  // Результат — в руку: успех и ошибка отдаются разной вибрацией, как
  // у самого Telegram. Человек узнаёт исход, не дочитывая надпись.
  function notify(kind) {
    try { tg.HapticFeedback.notificationOccurred(kind); } catch (e) { /* не везде есть */ }
  }

  // Пока человек заполняет доставку, случайный свайв вниз спрашивает,
  // закрывать ли: набранное с телефона терять обиднее всего.
  function guardClosing(on) {
    try {
      if (on) { tg.enableClosingConfirmation(); } else { tg.disableClosingConfirmation(); }
    } catch (e) { /* старые клиенты */ }
  }

  // Получатель запоминается в облаке Telegram: оно личное, по боту, и живёт
  // между сеансами. Второй роутер — родителям, на дачу — оформляется без
  // повторного набора имени и телефона. Только поля получателя: адрес
  // у второго роутера почти всегда другой.
  var RECIPIENT_KEYS = ['name', 'phone', 'city'];

  function cloud() {
    var cs = tg.CloudStorage;
    return cs && tg.isVersionAtLeast && tg.isVersionAtLeast('6.9') ? cs : null;
  }

  function loadRecipient() {
    var cs = cloud();
    if (!cs || RECIPIENT_KEYS.some(function (k) { return form[k]; })) { return Promise.resolve(); }
    return new Promise(function (resolve) {
      var done = false;
      function finish() { if (!done) { done = true; resolve(); } }
      // Облако может не ответить; форма не должна ждать его дольше секунды.
      window.setTimeout(finish, 800);
      try {
        cs.getItems(RECIPIENT_KEYS, function (err, values) {
          if (!err && values) {
            RECIPIENT_KEYS.forEach(function (k) {
              if (values[k] && !form[k]) { form[k] = String(values[k]); }
            });
          }
          finish();
        });
      } catch (e) { finish(); }
    });
  }

  function saveRecipient() {
    var cs = cloud();
    if (!cs) { return; }
    RECIPIENT_KEYS.forEach(function (k) {
      try { cs.setItem(k, String(form[k] || '')); } catch (e) { /* не страшно */ }
    });
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

  // Названия тарифов, скоростей доставки и перевозчиков заводит оператор,
  // и в боте они написаны с эмодзи впереди: в переписке это уместно. Здесь
  // набор значков свой и одинаковый на всё приложение, и чужая цветная
  // картинка в начале строки выбивается из него. Срезаем её тут, а не просим
  // оператора держать два написания одного названия: разъехавшись, они
  // назовут одну и ту же доставку по-разному в боте и в приложении.
  //
  // Если после срезки не осталось ничего — название состоит из одних значков,
  // и лучше показать его как есть, чем пустоту.
  function plainTitle(value) {
    var text = String(value == null ? '' : value);
    return text.replace(/^[^\p{L}\p{N}]+/u, '').trim() || text;
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

  // Подпись под названием тарифа — только то, чего в названии нет.
  //
  // Оператор называет тарифы сроком: «30 дней», «60 дней». Приписка «30 дн.»
  // под таким названием повторяла его дважды на каждой строке. Сравниваем
  // числа, а не слова: «1 месяц» и «30 дн.» тоже об одном сроке, но это
  // разные единицы, и вторая строка там как раз объясняет первую.
  function periodNote(title, period) {
    var inPeriod = String(period || '').match(/\d+/g);
    if (!inPeriod) { return period || ''; }
    var inTitle = String(title || '').match(/\d+/g) || [];
    return inPeriod.every(function (n) { return inTitle.indexOf(n) !== -1; })
      ? '' : period;
  }

  function planDays(p) {
    return Number(p.months || 0) * 30 + Number(p.extra_days || 0);
  }

  // Цена за месяц для сравнения сроков. Считаем из общего числа дней, а не
  // из поля тарифа: у дневного там лежит полная цена, и «300 ₽ в месяц»
  // у срока на 30 дней соседствовало бы с «1800 ₽ в месяц» у полугодового.
  // У месячного срока цена за месяц равна цене — но строка нужна и ему:
  // в списке из четырёх карточек одна без подписи читается как карточка
  // с ошибкой, а не как карточка, где подпись не нужна. Не приводим только
  // срокам короче месяца: у недели «в месяц» — выдумка.
  function planPerMonth(p, currency) {
    var days = planDays(p);
    if (days < 28) { return ''; }
    return perMonth(Number(p.price) / (days / 30), currency);
  }

  function planTitle(p) {
    return plainTitle(p.title);
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
    unreachable: 'Роутер не ответил. Попробуйте позже.',
    // Причину отказа пишет сам роутер: он один знает, что у него настроено.
    // Своего текста здесь нет намеренно — «узла больше нет» мы бы выдумали,
    // а он это знает точно.
    refused: ''
  };

  // «Слишком часто» без срока читается как поломка: через минуту то же
  // самое. С числом человек знает, что ждать, а не что чинить.
  function reason(res) {
    if (res.error === 'too_often' && res.retry_after) {
      var minutes = Math.max(1, Math.ceil(Number(res.retry_after) / 60));
      return 'Слишком много переключений подряд: каждое перезапускает сервис и '
        + 'на секунду роняет интернет дома. Подождите ' + minutes + ' '
        + plural(minutes, ['минуту', 'минуты', 'минут']) + '.';
    }
    return res.message || REASONS[res.error] || 'Роутер не ответил';
  }

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

  // Коды отказа с нашей стороны — словами. Список короткий намеренно:
  // всё, чего в нём нет, показывается общей фразой, а подробность и так
  // лежит в журнале сервера.
  var CODES = {
    not_found: 'Не нашли — возможно, заказ отменён или открыт не с того аккаунта.',
    tg_id_required: 'Не удалось узнать, кто вы. Закройте приложение и откройте заново.',
    unknown_field: 'Это поле мы не знаем — обновите приложение.'
  };

  function api(path, options) {
    var opts = options || {};
    opts.headers = Object.assign({}, opts.headers, {
      // Подпись уходит заголовком, а не в адресе: адреса попадают в журналы
      // прокси, и строка входа лежала бы там открытым текстом.
      'X-Telegram-Init-Data': tg.initData || '',
      'Content-Type': 'application/json'
    });
    return fetch('/app/api' + path, opts).catch(function () {
      // Сорванный fetch — это не ответ сервера, а обрыв связи, и разбор ниже
      // до него не доходит. Браузер отдаёт «Failed to fetch» или «Load
      // failed», и клиент читал это дословно: приложение открывают в метро
      // и в лифте чаще, чем за столом.
      throw new Error('Нет связи. Проверьте интернет и попробуйте ещё раз.');
    }).then(function (r) {
      // Разбираем через текст: на пути стоит прокси, и его страница на 502
      // роняла бы разбор с «Unexpected token '<'» вместо внятной причины.
      return r.text().then(function (raw) {
        var body = null;
        try { body = raw ? JSON.parse(raw) : null; } catch (e) { body = null; }
        if (!r.ok) {
          // `error` пишется человеку — «Роутера нет в наличии». `detail` —
          // код для разбора, и показывать его нельзя: клиент читал на экране
          // «Не получилось — not_found» и шёл в поддержку выяснять, что это.
          var reason = (body && body.error) || CODES[body && body.detail];
          // Код состояния оставляем в скобках: клиенту он ничего не говорит,
          // но назвать его поддержке — единственное, чем он может помочь.
          throw new Error(reason || 'Сейчас не получилось (' + r.status + '). Попробуйте ещё раз.');
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

  // Откуда приехал экран: вкладка поднимается снизу, подэкран въезжает
  // справа, возврат — слева. Движение объясняет, куда ведёт «Назад»,
  // раньше, чем человек его нажмёт.
  var motion = 'tab';

  function show(html) {
    screen.className = 'wrap m-' + motion;
    screen.innerHTML = html;
    window.scrollTo(0, 0);
    countNumbers();
  }

  // Числа с data-count набегают от нуля. Только там, где число — смысл
  // экрана (дни подписки), а не везде: считающиеся цены выглядят как торг.
  function countNumbers() {
    var still = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    screen.querySelectorAll('[data-count]').forEach(function (el) {
      var target = Number(el.dataset.count);
      if (still || !isFinite(target) || target <= 0) { return; }
      var startedAt = null;
      var span = 650;
      function step(ts) {
        if (startedAt === null) { startedAt = ts; }
        var k = Math.min(1, (ts - startedAt) / span);
        var eased = 1 - Math.pow(1 - k, 3);
        el.textContent = Math.round(target * eased);
        if (k < 1) { window.requestAnimationFrame(step); }
      }
      el.textContent = '0';
      window.requestAnimationFrame(step);
      // Страховка: если кадры не идут (окно скрыто, экономия батареи),
      // число всё равно доезжает до цели — ноль вместо «24» хуже,
      // чем отсутствие анимации.
      window.setTimeout(function () { el.textContent = String(target); }, span + 120);
    });
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
    else {
      if (stack.length) { stack[stack.length - 1].scroll = window.scrollY; }
      stack.push(view);
    }
    motion = stack.length > 1 ? 'push' : 'tab';
    render(view);
  }

  function back() {
    if (stack.length > 1) { stack.pop(); motion = 'pop'; render(stack[stack.length - 1]); }
  }

  function openTab(name) {
    stack = [];
    motion = 'tab';
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
    // На подэкране нижняя панель прячется, как у пролистанных экранов
    // в iOS: назад ведёт кнопка в шапке, вперёд — системная кнопка внизу,
    // и третий ряд управления был бы шумом.
    document.body.classList.toggle('pushed', stack.length > 1);
    mainButton(null);
    guardClosing(view.name === 'buy' && (view.step || 1) >= 2);
    syncBack();
    skeleton();
    var draw = views[view.name];
    if (!draw) { return failed(new Error('Неизвестный экран: ' + view.name)); }
    Promise.resolve()
      .then(function () { return draw(view); })
      .catch(failed)
      .then(bridgePrimary, bridgePrimary)
      // «Назад» возвращает на то место, где человек стоял, а не наверх:
      // список заказов, открытый на пятом, после карточки снова на пятом.
      .then(function () {
        if (view.scroll) { window.scrollTo(0, view.scroll); view.scroll = 0; }
      })
      .then(hideSplash, hideSplash);
  }

  /* --- Системная кнопка ---------------------------------------------------
     Главное действие подэкрана — кнопкой Telegram под окном приложения,
     а не своей. Гайд Telegram прямой: элементы должны повторять поведение
     уже существующих, а дублирующая навигация — шум. Кнопка красится нашим
     акцентом; окно под неё Telegram уменьшает сам, содержимое не перекрыто.

     Обработчики экранов не переписаны: системная кнопка просто «нажимает»
     страничную, а та прячется. Её состояние — текст, занятость — зеркалится
     наблюдателем: экран продления переписывает подпись при каждом выборе,
     и кнопка внизу должна говорить то же самое. Клиенту без системной
     кнопки остаётся страничная, как была. */

  var mainHandler = null;
  var primaryWatch = null;

  // Кнопка настроек в шапке ведёт в поддержку: она должна быть под рукой
  // с любого экрана, а не только с экрана роутера. Показывается, когда
  // контакт задан; обработчик вешается один раз, контакт — переменной.
  var supportUrl = '';
  var settingsBound = false;

  // Ссылку собирает сервер: оператор пишет контакт как удобно — «@имя»,
  // «t.me/имя» или адрес сайта поддержки, — а страница её только открывает.
  // Телеграмовскую — внутри мессенджера, чужую — во внешнем браузере.
  function openSupport(url) {
    if (!url) { return; }
    if (/^https?:\/\/t\.me\//.test(url)) { tg.openTelegramLink(url); } else { tg.openLink(url); }
  }

  function setupSettings(url) {
    var sb = tg.SettingsButton;
    if (!sb || !tg.isVersionAtLeast || !tg.isVersionAtLeast('7.0')) { return; }
    try {
      if (!url) { sb.hide(); return; }
      supportUrl = url;
      if (!settingsBound) {
        settingsBound = true;
        sb.onClick(function () { haptic(); openSupport(supportUrl); });
      }
      sb.show();
    } catch (e) { /* старые клиенты */ }
  }

  function mainButton(label, onClick) {
    var mb = tg.MainButton;
    if (!mb || !tg.isVersionAtLeast || !tg.isVersionAtLeast('6.1')) { return false; }
    try {
      if (mainHandler) { mb.offClick(mainHandler); mainHandler = null; }
      if (!label) { mb.hide(); return true; }
      mainHandler = onClick;
      mb.setParams({ text: label, color: '#3b93ff', text_color: '#04121f' });
      mb.onClick(onClick);
      mb.show();
      return true;
    } catch (e) { return false; }
  }

  function bridgePrimary() {
    if (primaryWatch) { primaryWatch.disconnect(); primaryWatch = null; }
    if (stack.length < 2) { return; }
    var btn = screen.querySelector('#make, #next, #pay');
    if (!btn) { return; }

    function sync() {
      var mb = tg.MainButton;
      var label = btn.textContent.replace(/\s+/g, ' ').trim();
      try {
        mb.setText(label.slice(0, 64));
        if (btn.disabled) { mb.showProgress(false); } else { mb.hideProgress(); }
      } catch (e) { /* старые клиенты */ }
    }

    var shown = mainButton(btn.textContent.trim(), function () { btn.click(); });
    if (!shown) { return; }
    btn.classList.add('mirrored');
    sync();
    primaryWatch = new MutationObserver(sync);
    primaryWatch.observe(btn, { attributes: true, childList: true, subtree: true, characterData: true });
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

  // Склонение по числу: 1 минуту, 2 минуты, 5 минут. Формы даёт вызывающий.
  function plural(n, forms) {
    var a = Math.abs(n) % 100;
    var b = a % 10;
    if (a > 10 && a < 20) { return forms[2]; }
    if (b > 1 && b < 5) { return forms[1]; }
    if (b === 1) { return forms[0]; }
    return forms[2];
  }

  // Приветствие по часам телефона: «доброе утро» в час ночи выдаёт
  // сервер, живущий по своему времени. Ровно одна строка тепла — дальше
  // экран деловой.
  function greeting() {
    var h = new Date().getHours();
    if (h >= 5 && h < 12) { return 'Доброе утро'; }
    if (h >= 12 && h < 18) { return 'Добрый день'; }
    if (h >= 18 && h < 23) { return 'Добрый вечер'; }
    return 'Доброй ночи';
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
            '<div class="hero">' + '<div class="brand"><img src="/app/logo" alt="">Titan Routers</div>' + '<h1>' + esc(p.hero_title || 'Роутер с доступом') + '</h1>'
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
        '<div class="eyebrow">' + greeting() + '</div>'
        + '<h1>' + esc(user.name || 'Профиль') + '</h1>'

        // Подписка — первым и крупно: это то, за чем сюда заходят повторно,
        // и то, что приносит деньги после первой покупки.
        + (term
            ? '<div class="card"><div class="term ' + term.tone + '">'
              +   '<div class="row" style="align-items:flex-end">'
              +     '<div><div class="muted small">Подписка активна</div>'
              +       '<div class="num" data-count="' + term.days + '" style="margin-top:4px">' + term.days + '</div></div>'
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
              + '<span class="muted small" id="router-line" style="display:block">'
              + 'Связь, показания, обновление</span>'
              + '</span><span class="chev">' + icon('chev-r') + '</span></button></div>'
            : '')

        + '<div style="margin-top:14px">' + buyOffer(d.router_available) + '</div>'

        // Самый естественный для Telegram рост — переслать. Ссылка их
        // реферальная, приглашение засчитывается ботом; текст — с сервера.
        + (d.share && d.share.url
            ? '<div class="list leading">'
              + '<button class="item" id="share">'
              + '<span class="ic-box">' + icon('share') + '</span>'
              + '<span class="grow"><b>Порекомендовать</b>'
              + '<span class="muted small" style="display:block">Отправить другу ссылку '
              + 'на роутер</span></span>'
              + '<span class="chev">' + icon('chev-r') + '</span></button></div>'
            : '')

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
      var share = document.getElementById('share');
      if (share) {
        share.addEventListener('click', function () {
          haptic('medium');
          tg.openTelegramLink('https://t.me/share/url?url=' + encodeURIComponent(d.share.url)
            + '&text=' + encodeURIComponent(d.share.text || ''));
        });
      }
      setupSettings(d.support_url);
      bindCatalog();
      bindOrderRows();

      // Строка «Мой роутер» — живая: связь и число устройств, а не подпись.
      // Отдельным запросом после отрисовки: показания лежат в базе и стоят
      // копейки, а профиль без них — список ссылок, а не приборная панель.
      var line = document.getElementById('router-line');
      if (line) {
        api('/router').then(function (rd) {
          var r = rd && rd.router;
          if (!r) { return; }
          var n = Number(r.clients);
          line.innerHTML = r.online
            ? '<span class="live ok"><i class="dot"></i>На связи</span>'
              + (isFinite(n) && r.clients != null
                  ? ' · ' + n + ' ' + plural(n, ['устройство', 'устройства', 'устройств']) : '')
            : '<span class="live off"><i class="dot"></i>Не на связи</span>';
        }).catch(function () { /* подпись останется прежней */ });
      }
    });
  };

  /* --- Подписка ----------------------------------------------------------- */

  views.renew = function () {
    // Экран продления собран как шаг выбора срока в покупке: не четыре
    // ценника, каждый из которых кнопка, а выбор карточками и одна кнопка
    // внизу. Четыре равноправные кнопки — это четыре решения, и человек
    // не принимает ни одного; выбор с одной кнопкой — одно решение, уже
    // наполовину принятое отмеченной карточкой.
    //
    // Доводы с витрины стоят выше цены по той же причине, что и в каталоге:
    // цифра без них читается как «ещё 900 ₽», с ними — как «ещё три месяца
    // всего дома». Свой текст здесь не пишем: обещания живут на витрине.
    return Promise.all([api('/renew'), once('pitch', '/pitch')]).then(function (res) {
      var d = res[0];
      var sub = d.subscription || {};
      var active = sub.status === 'active';
      var term = active ? termLeft(sub) : null;
      var list = (d.plans || []).slice();
      var best = bestPlan(list);
      var preset = list.filter(function (x) { return x.is_default; })[0];
      var chosen = (preset || best || list[0] || {}).id;

      // Новая дата окончания: от текущей, если подписка жива, иначе
      // от сегодня. Это главный аргумент за длинный срок — не «180 дней»,
      // а «до марта не вспоминать».
      function endsAt(p) {
        var base = active && sub.until ? new Date(sub.until) : new Date();
        if (isNaN(base)) { base = new Date(); }
        base.setDate(base.getDate() + planDays(p));
        return base.toISOString();
      }

      function box(p) {
        var rate = planPerMonth(p, d.currency);
        var note = [periodNote(planTitle(p), planPeriod(p)), rate ? rate + ' в месяц' : '']
          .filter(Boolean).join(' · ');
        // «Выгоднее всего» считается по цене за месяц и при ровной сетке
        // не появляется; «рекомендуем» — срок, который оператор отметил
        // в тарифах, и это его выбор, а не наш.
        var tag = best && best.id === p.id ? 'выгоднее всего'
          : (preset && preset.id === p.id ? 'рекомендуем' : '');
        return '<label class="choice"><input type="radio" name="renew" value="' + esc(p.id) + '"'
          + (chosen === p.id ? ' checked' : '') + '>'
          + '<div class="box"><span class="tick"></span><span class="grow">'
          + '<span class="row"><span><b>' + esc(planTitle(p)) + '</b>'
          + (tag ? ' <span class="best">' + tag + '</span>' : '') + '</span>'
          + '<span style="font-weight:700">' + money(p.price, d.currency) + '</span></span>'
          + (note ? '<span class="muted small" style="display:block;margin-top:3px">'
                    + esc(note) + '</span>' : '')
          + '</span></div></label>';
      }

      var value = (res[1].value || []).slice(0, 3).map(function (v) {
        return '<span class="chip">' + icon('check') + esc(v.title) + '</span>';
      }).join('');

      show(
        '<h1>Подписка</h1>'
        + (term
            ? '<div class="card"><div class="term ' + term.tone + '">'
              +   '<div class="row" style="align-items:flex-end">'
              +     '<div><div class="muted small">Подписка активна</div>'
              +       '<div class="num" data-count="' + term.days + '" style="margin-top:4px">' + term.days + '</div></div>'
              +     '<div style="text-align:right">'
              +       '<div class="muted small">' + daysWord(term.days) + ' осталось</div>'
              +       '<div class="small" style="margin-top:4px">до ' + date(sub.until) + '</div>'
              +     '</div>'
              +   '</div>'
              +   '<div class="track"><div class="fill" style="width:'
              +     (100 - term.spent) + '%"></div></div>'
              + '</div></div>'
            : (sub.until
                ? '<div class="card"><div class="row"><div>'
                  + '<div class="muted small">Закончилась</div>'
                  + '<div class="big" style="margin-top:2px">' + date(sub.until) + '</div></div>'
                  + '<span class="pill bad">не активна</span></div></div>'
                : ''))
        + (value ? '<div class="chips">' + value + '</div>' : '')
        + (list.length
            ? '<div class="sec">Срок продления</div>'
              + '<div class="card">' + list.map(box).join('') + '</div>'
              + '<div class="small center" id="ends" style="margin:-2px 0 12px"></div>'
              + '<button class="btn" id="pay"></button>'
              + '<div class="muted tiny center" style="margin-top:10px">'
              + (active && sub.until
                  ? 'Оплаченные дни прибавятся к ' + date(sub.until)
                    + ' — то, что осталось, не сгорает. '
                  : 'Доступ включится сразу после оплаты. ')
              // Главный страх у любой подписки — автосписание. У нас его
              // нет, и об этом надо сказать там, где человек решает.
              + 'Платёж разовый, без автосписаний.'
              + '</div>'
            : empty('info', 'Сроков нет', 'Продление сейчас недоступно.'))
      );

      if (!list.length) { return; }

      function selected() {
        return list.filter(function (x) { return x.id === chosen; })[0];
      }

      // Кнопка и дата переписываются под выбор: человек читает не «оплатить»,
      // а «продлить на 90 дней за 900 ₽» — и видит, до какого числа.
      function refresh() {
        var p = selected();
        var ends = document.getElementById('ends');
        var pay = document.getElementById('pay');
        // Счёт готовится не мгновенно, и к ответу клиент может быть уже на
        // другом экране. Тогда переписывать нечего: без этой проверки
        // обращение к исчезнувшей строке роняло обработчик.
        if (!p || !ends || !pay) { return; }
        var title = planTitle(p);
        ends.innerHTML = 'Будет действовать до <b>' + date(endsAt(p)) + '</b>';
        pay.innerHTML = icon('card') + 'Продлить на '
          + esc(title.charAt(0).toLowerCase() + title.slice(1)) + ' · '
          + money(p.price, d.currency);
      }
      refresh();

      screen.querySelectorAll('input[name="renew"]').forEach(function (r) {
        r.addEventListener('change', function () {
          chosen = Number(r.value); haptic(); refresh();
        });
      });

      document.getElementById('pay').addEventListener('click', function () {
        var btn = this;
        haptic('medium');
        btn.disabled = true;
        btn.innerHTML = icon('refresh', 'ic spin') + 'Готовим счёт…';
        api('/renew', { method: 'POST', body: JSON.stringify({ plan_id: chosen }) })
          .then(function (r) {
            if (!r.ok) { throw new Error(r.error || 'Не получилось создать счёт'); }
            tg.openLink(r.pay_url);
          })
          .catch(function (err) { tg.showAlert(err.message || String(err)); })
          .then(function () { btn.disabled = false; refresh(); });
      });
    });
  };

  /* --- Роутер ------------------------------------------------------------- */

  // Настройки сервиса доступа: переключатель и выбор сервера.
  //
  // Рисуются отдельно от экрана, потому что и приезжают отдельно: список
  // читается с самого устройства по туннелю, а это до пятнадцати секунд.
  // Экран, который ждал бы их, заставлял бы ждать столько же и того, кто
  // зашёл посмотреть срок подписки, — а заходят чаще за этим.
  function renderAccess(slot, state, deviceId) {
    function row(node) {
      var on = node.id === state.current;
      return '<button class="item" data-node="' + esc(node.id) + '">'
        // Флаг — единственная цветная картинка в приложении, и это оправдано:
        // страну по флагу узнают быстрее, чем прочитывают слово, а рисовать
        // тридцать флагов обводкой в одну толщину невозможно. Узла без флага
        // это не касается: значка вместо него не ставим, строка просто
        // начинается с названия.
        // Плитка одной ширины у каждой строки: у страны в ней флаг, у «Авто» —
        // значок. Без плитки названия стран и «Авто» начинались бы с разных
        // отступов, и один список читался бы как два.
        + '<span class="ic-box">'
        +   (node.flag ? '<span class="flag">' + esc(node.flag) + '</span>'
                       : icon(node.auto ? 'swap' : 'router'))
        + '</span>'
        + '<span class="grow"><b>' + esc(node.name) + '</b>'
        + (node.auto
            ? '<span class="muted small" style="display:block;margin-top:2px">'
              + 'Сеть подберёт сервер сама</span>'
            : '')
        + '</span>'
        + (on ? '<span class="pop" style="color:var(--accent)">' + icon('check') + '</span>'
              : '<span class="chev">' + icon('chev-r') + '</span>')
        + '</button>';
    }

    slot.innerHTML =
      '<div class="sec" style="margin-top:24px">Сервис доступа</div>'
      + '<div class="list"><div class="item">'
      +   '<span class="grow"><b>Доступ к зарубежным ресурсам</b>'
      +     '<span class="muted small" style="display:block;margin-top:2px">'
      +     'Российские сайты идут напрямую в любом случае</span></span>'
      +   '<label class="sw"><input type="checkbox" id="svc"'
      +     (state.enabled ? ' checked' : '') + '><i></i></label>'
      + '</div></div>'
      // Список серверов при выключенном сервисе прячем: выбирать сервер
      // для выключенного доступа человеку нечего, а строй неактивных кнопок
      // читается как поломка.
      + (state.enabled && (state.nodes || []).length > 1
          ? '<div class="sec">Сервер</div>'
            + '<div class="list leading">' + (state.nodes || []).map(row).join('') + '</div>'
            + '<div class="muted tiny" style="margin:-4px 2px 0">Если какой-то сервис '
            + 'открывается медленно, попробуйте другой сервер. Переключение занимает '
            + 'несколько секунд, в которые интернет дома замирает.</div>'
          : '');

    function apply(request, busyText) {
      slot.querySelectorAll('button,input').forEach(function (el) { el.disabled = true; });
      var note = document.createElement('div');
      note.className = 'muted tiny center busy';
      note.style.marginTop = '10px';
      note.innerHTML = icon('refresh', 'ic spin') + '<span></span>';
      note.lastChild.textContent = busyText;
      slot.appendChild(note);

      return request.then(function (res) {
        if (!res.ok) { throw new Error(reason(res)); }
        renderAccess(slot, res, deviceId);
        notify('success');
      }).catch(function (err) {
        // Перерисовываем прежним состоянием: оставить переключатель
        // в новом положении после отказа — соврать о том, что применилось.
        renderAccess(slot, state, deviceId);
        notify('error');
        tg.showAlert(err.message || String(err));
      });
    }

    slot.querySelectorAll('[data-node]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (btn.dataset.node === state.current) { return; }
        haptic('medium');
        apply(api('/router/node', {
          method: 'POST',
          body: JSON.stringify({ device_id: deviceId, node_id: btn.dataset.node })
        }), 'Переключаем сервер…');
      });
    });

    var svc = slot.querySelector('#svc');
    if (svc) {
      svc.addEventListener('change', function () {
        haptic('medium');
        apply(api('/router/service', {
          method: 'POST',
          body: JSON.stringify({ device_id: deviceId, enabled: svc.checked })
        }), svc.checked ? 'Включаем…' : 'Выключаем…');
      });
    }
  }

  views.router = function (view) {
    var path = view && view.id ? '/router?device_id=' + view.id : '/router';
    return api(path).then(function (d) {
      if (!d.has_client || !d.router) {
        // Тупик без выхода — упущенная продажа: у человека нет роутера,
        // и ровно здесь ему уместно предложить выбрать.
        show('<h1>Мой роутер</h1>'
          + empty('router', 'Роутера пока нет',
                  'Как только устройство выйдет на связь, здесь появятся его показания.')
          + '<button class="btn ghost" id="to-catalog">' + icon('box') + 'Выбрать роутер</button>');
        document.getElementById('to-catalog').addEventListener('click', function () {
          haptic('medium'); openTab('catalog');
        });
        return;
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
        +   '<div class="row"><span class="ic-box">' + icon('router') + '</span><div class="grow">'
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

        // Пустое место под настройки сервиса: они читаются с самого роутера
        // и приезжают позже остального экрана.
        // Не пустое место, а заготовка: список едет с самого роутера, до
        // пятнадцати секунд, и пустота под кнопками читается как «настроек
        // нет», а заготовка — как «сейчас будут». Не дождались — уберётся вся.
        + '<div id="access">'
        +   '<div class="sec" style="margin-top:24px">Сервис доступа</div>'
        +   '<div class="card"><div class="sk" style="width:62%"></div>'
        +     '<div class="sk" style="width:38%;margin-top:10px"></div></div>'
        +   '<div class="muted tiny center" style="margin-top:8px">Спрашиваем роутер…</div>'
        + '</div>'

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

      // Настройки сервиса — отдельным запросом, уже после отрисовки экрана.
      // Отказ здесь не ошибка: на прошивке без скрипта управления список
      // не читается вовсе, и блок просто не появляется. Показать вместо него
      // «не удалось загрузить» значило бы сообщать клиенту о нашей недоделке
      // на экране, где у него всё работает.
      var slot = document.getElementById('access');
      api('/router/nodes?device_id=' + r.id).then(function (n) {
        if (n && n.ok) { renderAccess(slot, n, r.id); } else { slot.remove(); }
      }).catch(function () { slot.remove(); });

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
          openSupport(d.support_url);
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
              reboot.innerHTML = icon('refresh', 'ic spin') + 'Отправляем…';
              api('/router/reboot', {
                method: 'POST', body: JSON.stringify({ device_id: r.id })
              }).then(function (res) {
                if (!res.ok) { throw new Error(REASONS[res.error] || 'Роутер не ответил'); }
                reboot.innerHTML = icon('check') + 'Перезагружается';
                notify('success');
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
        btn.innerHTML = icon('refresh', 'ic spin') + 'Отправляем…';
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
            : empty('receipt', 'Заказов нет', 'Оформленные заказы появятся здесь.')
              + '<button class="btn ghost" id="to-catalog">' + icon('box') + 'Выбрать роутер</button>'));
      var toCatalog = document.getElementById('to-catalog');
      if (toCatalog) {
        toCatalog.addEventListener('click', function () { haptic('medium'); openTab('catalog'); });
      }
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
                  ? '<button id="track" class="linkline"><span class="muted small">Трек-номер</span>'
                    + '<span class="mono small">' + esc(o.tracking_number) + '</span>'
                    + '<span class="subtle">' + icon('copy') + '</span></button>' : '')
              + '</div></div></div>'
            : '')

        + '<div class="stack">'
        + (o.payable ? '<button class="btn" id="pay">' + icon('card') + 'Оплатить</button>' : '')
        + (o.instruction_url
            ? '<button class="btn ghost" id="setup">' + icon('info') + 'Как подключить</button>' : '')
        + '</div>'
      );

      // Трек-номер копируется нажатием, как MAC: его вводят на сайте
      // перевозчика, а перепечатывать четырнадцать знаков с экрана — ошибки.
      var track = document.getElementById('track');
      if (track) {
        track.addEventListener('click', function () {
          haptic();
          copyText(o.tracking_number).then(function (done) {
            tg.showAlert(done ? 'Трек-номер скопирован: ' + o.tracking_number
                              : 'Скопировать не вышло. Трек-номер: ' + o.tracking_number);
          });
        });
      }

      var pay = document.getElementById('pay');
      if (pay) {
        pay.addEventListener('click', function () {
          haptic('medium');
          pay.disabled = true;
          pay.innerHTML = icon('refresh', 'ic spin') + 'Готовим…';
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
    // Сроки отправки спрашиваем заодно: до сих пор их можно было увидеть
    // только начав оформление, а «когда приедет» — вопрос, который человек
    // задаёт себе до кнопки «Купить», а не после неё. Тот же справочник, что
    // и в оформлении, и тот же кэш — второй раз он уже не запросится.
    return Promise.all([api('/pitch'), once('delivery', '/delivery')]).then(function (res) {
      var d = res[0];

      // Два роутера рядом — это развилка, на которой человек чаще всего
      // уходит думать: характеристики он сравнивать не станет, а ошибиться
      // на несколько тысяч не хочет. Поэтому «кому какой» отвечаем словами
      // оператора — подзаголовком товара, тем же, что на витрине, — и только
      // потом показываем карточки с цифрами. Пишется он в админке; пустой
      // подзаголовок просто убирает этот блок, выдумывать за оператора,
      // кому подойдёт его товар, мы не станем.
      var named = (d.products || []).filter(function (p) { return p.subtitle; });
      var chooser = named.length > 1
        ? '<div class="sec">Какой выбрать</div><div class="list leading">'
          + named.map(function (p) {
              return '<button class="item" data-jump="p' + esc(p.id) + '">'
                + '<span class="ic-box">' + icon('router') + '</span>'
                + '<span class="grow"><b>' + esc(p.title) + '</b>'
                + '<span class="muted small" style="display:block;margin-top:2px">'
                + esc(p.subtitle) + '</span></span>'
                + '<span class="chev">' + icon('chev-r') + '</span></button>';
            }).join('')
          + '</div>'
        : '';

      var items = (d.products || []).map(function (p) {
        var specs = (p.specs || []).map(function (pair) {
          return '<div><dt>' + esc(pair[0]) + '</dt><dd>' + esc(pair[1]) + '</dd></div>';
        }).join('');

        return '<div class="card prod" id="p' + esc(p.id) + '">'
          // Без фото карточка начиналась бы с заголовка впритык к краю и
          // выглядела бы обрезанной рядом с соседней, у которой фото есть.
          + (p.photo_url
              ? '<div class="shot"><img src="' + esc(p.photo_url) + '" alt=""></div>'
              : '<div class="shot shot-empty">' + icon('router', 'ic-lg') + '</div>')
          + '<div class="row"><b class="grow" style="font-size:17px">' + esc(p.title) + '</b>'
          +   (p.in_stock
                ? '<span class="pill ok"><i class="dot"></i>в наличии</span>'
                : (p.preorder ? '<span class="pill warn">под заказ</span>'
                              : '<span class="pill off">нет в наличии</span>'))
          + '</div>'
          // Когда блок «Какой выбрать» собрался, подзаголовок уже сказан
          // строкой выше — повторять его в карточке значит два раза подряд
          // написать одно и то же на одном экране.
          + (p.subtitle && !chooser
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
          + 'входит в стоимость. Роутер приезжает настроенным — '
          + '<button class="link" data-jump="ship">сроки отправки</button>.</div>'
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
        var period = periodNote(p.title, p.period);
        return '<div class="plan"><div class="grow"><div><b>' + esc(p.title) + '</b></div>'
          + (period ? '<div class="muted small">' + esc(period) + '</div>' : '')
          + '</div>'
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

      var ship = (res[1].options || []).map(function (s) {
        return '<div class="feat"><span class="ic-box">' + icon('truck') + '</span>'
          + '<div class="grow"><b>' + esc(plainTitle(s.title)) + '</b>'
          + '<div class="muted small" style="margin-top:3px">' + esc(s.description) + '</div>'
          + '</div></div>';
      }).join('');

      show(
        (d.hero_title
          ? '<div class="hero">' + '<div class="brand"><img src="/app/logo" alt="">Titan Routers</div>' + '<h1>' + esc(d.hero_title) + '</h1>'
            + (d.hero_subtitle ? '<p>' + esc(d.hero_subtitle) + '</p>' : '') + '</div>'
          : '<h1>Каталог</h1>')
        + (value ? '<div class="list leading">' + value + '</div>' : '')
        + chooser
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
        + (ship
            ? '<div class="sec" id="ship">Когда приедет</div><div class="card">' + ship + '</div>'
              + '<div class="muted tiny center">Стоимость доставки зависит от города '
              + 'и габаритов: её посчитает оператор после оформления.</div>'
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

      // Переходы внутри страницы, а не на отдельный экран: человек читает
      // каталог и не должен терять место, куда вернуться. Промах по цели
      // молча ничего не делает — товар мог кончиться между отрисовкой
      // и нажатием.
      screen.querySelectorAll('[data-jump]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var target = document.getElementById(btn.dataset.jump);
          if (!target) { return; }
          haptic();
          target.scrollIntoView({ behavior: 'smooth', block: 'start' });
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
            + '<span class="muted small">'
            + esc(periodNote(planTitle(x), planPeriod(x))) + '</span>'
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
    return Promise.all([productById(view.productId), once('delivery', '/delivery'), loadRecipient()])
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
                    return choice('speed', s.speed, form.speed === s.speed,
                                  plainTitle(s.title), s.description);
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
                          return choice('carrier', c.method, form.method === c.method,
                                        plainTitle(c.title), '');
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
              notify('error');
              return;
            }
            saveRecipient();
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
          + '<div class="muted tiny center" style="margin-top:10px">Платёж разовый, без '
          + 'автосписаний. Картой, через СБП или криптовалютой.</div>'
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
          btn.innerHTML = icon('refresh', 'ic spin') + 'Оформляем…';
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
    notify('success');
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
