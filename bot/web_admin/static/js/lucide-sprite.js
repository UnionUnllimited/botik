/**
 * Локальные Lucide: один SVG-спрайт + <use>. Без тяжёлого lucide.js.
 *
 * Подключается в <head> БЕЗ defer/async (короткий синхронный скрипт),
 * чтобы успеть установить MutationObserver ДО парсинга <body>.
 * Каждый <i data-lucide="..."> заменяется на <svg><use href=".../sprite.svg#name"/></svg>
 * в момент его появления в DOM — без FOUC и «мерцания» при перезагрузке.
 *
 * URL спрайта задаётся через window.__LUCIDE_SPRITE__ (см. base.html).
 * Совместимо с lucide.createIcons() / lucide.createIcons({ root }).
 */
(function (global) {
  'use strict';

  function spriteUrl() {
    return global.__LUCIDE_SPRITE__ || '';
  }

  function replaceIcon(el) {
    if (!el || el.nodeType !== 1) return;
    if (el.tagName !== 'I') return;
    if (!el.hasAttribute('data-lucide')) return;
    if (el.__lucideReplaced) return;
    var name = el.getAttribute('data-lucide');
    if (!name) return;
    var spr = spriteUrl();
    if (!spr) {
      if (!global.__lucideSpriteWarned) {
        try { console.warn('[lucide-sprite] Задайте window.__LUCIDE_SPRITE__'); } catch (_) {}
        global.__lucideSpriteWarned = true;
      }
      return;
    }
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    var filledIcons = { remnawave: '0 0 24 24' };
    if (filledIcons[name]) {
      svg.setAttribute('viewBox', filledIcons[name]);
      svg.setAttribute('fill', 'currentColor');
    } else {
      svg.setAttribute('viewBox', '0 0 24 24');
      svg.setAttribute('fill', 'none');
      svg.setAttribute('stroke', 'currentColor');
      svg.setAttribute('stroke-width', '2');
      svg.setAttribute('stroke-linecap', 'round');
      svg.setAttribute('stroke-linejoin', 'round');
    }
    var cls = el.getAttribute('class');
    svg.setAttribute('class', (cls ? cls + ' ' : '') + 'tw-lucide');
    var st = el.getAttribute('style');
    if (st) svg.setAttribute('style', st);
    var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    var href = spr + '#' + name;
    use.setAttribute('href', href);
    try { use.setAttributeNS('http://www.w3.org/1999/xlink', 'href', href); } catch (_) {}
    svg.appendChild(use);
    if (el.parentNode) {
      el.parentNode.replaceChild(svg, el);
    } else {
      // Элемент ещё не вставлен в DOM (редкий случай через innerHTML).
      // Помечаем, чтобы при следующем проходе не дублировать.
      el.__lucideReplaced = true;
    }
  }

  function replaceTree(root) {
    if (!root) return;
    // Сам root может быть <i data-lucide> (когда вставляют innerHTML с одной иконкой)
    if (root.nodeType === 1 && root.tagName === 'I' && root.hasAttribute('data-lucide')) {
      replaceIcon(root);
      return;
    }
    if (!root.querySelectorAll) return;
    var nodes = root.querySelectorAll('i[data-lucide]');
    for (var i = 0; i < nodes.length; i++) replaceIcon(nodes[i]);
  }

  function createIcons(opts) {
    var root = opts && opts.root ? opts.root : document;
    replaceTree(root);
  }

  // ─── MutationObserver: заменять иконки в момент их появления в DOM ───
  // Это работает уже во время парсинга <body> (если скрипт в <head> без defer)
  // и для динамически вставляемого innerHTML после загрузки страницы.
  function startObserver() {
    var target = document.documentElement || document.body || document;
    if (!target || !global.MutationObserver) return;
    var mo = new MutationObserver(function (mutations) {
      for (var i = 0; i < mutations.length; i++) {
        var m = mutations[i];
        if (m.type !== 'childList') continue;
        var added = m.addedNodes;
        for (var j = 0; j < added.length; j++) {
          var node = added[j];
          if (!node || node.nodeType !== 1) continue;
          replaceTree(node);
        }
      }
    });
    mo.observe(target, { childList: true, subtree: true });
  }

  // Запускаем наблюдатель сразу — он будет ловить новые <i data-lucide>
  // в процессе парсинга <body> и заменять их без мерцания.
  startObserver();

  // На всякий случай — финальный проход после готовности DOM,
  // чтобы покрыть иконки, которые могли быть добавлены до старта observer.
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { createIcons(); });
    } else {
      createIcons();
    }
  }

  global.lucide = { createIcons: createIcons };
})(typeof window !== 'undefined' ? window : this);
