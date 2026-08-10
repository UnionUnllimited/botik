/**
 * Справка INFO в админке.
 * Тексты: static/info/<key>.js → window.AdminInfoRegistry[key]
 * Картинки: https://docs.3xstore.ru/info/<filename> (ADMIN_INFO_IMAGE_BASE)
 */
(function (global) {
  'use strict';

  /** Базовый URL иллюстраций на docs.3xstore.ru */
  var ADMIN_INFO_IMAGE_BASE = 'https://docs.3xstore.ru/info/';

  var registry = global.AdminInfoRegistry || {};
  var popoverEl = null;
  var activeBtn = null;
  var hideTimer = null;

  function resolveImageUrl(name) {
    if (!name) return '';
    if (/^https?:\/\//i.test(name)) return name;
    return ADMIN_INFO_IMAGE_BASE.replace(/\/?$/, '/') + String(name).replace(/^\//, '');
  }

  function ensurePopover() {
    if (popoverEl) return popoverEl;
    popoverEl = document.createElement('div');
    popoverEl.className = 'admin-info-popover';
    popoverEl.setAttribute('role', 'tooltip');
    popoverEl.hidden = true;
    document.body.appendChild(popoverEl);
    return popoverEl;
  }

  function buildPopoverContent(entry) {
    var frag = document.createDocumentFragment();
    if (entry.title) {
      var title = document.createElement('div');
      title.className = 'admin-info-popover-title';
      title.textContent = entry.title;
      frag.appendChild(title);
    }
    if (entry.body) {
      var body = document.createElement('div');
      body.className = 'admin-info-popover-body';
      if (typeof entry.body === 'string') body.innerHTML = entry.body;
      else body.appendChild(entry.body);
      frag.appendChild(body);
    }
    (entry.images || []).forEach(function (name) {
      var wrap = document.createElement('div');
      wrap.className = 'admin-info-popover-img-wrap';
      var img = document.createElement('img');
      img.className = 'admin-info-popover-img';
      img.src = resolveImageUrl(name);
      img.alt = entry.imageAlt || entry.title || '';
      img.loading = 'lazy';
      img.decoding = 'async';
      wrap.appendChild(img);
      frag.appendChild(wrap);
    });
    return frag;
  }

  function positionPopover(btn) {
    var pop = ensurePopover();
    var rect = btn.getBoundingClientRect();
    var gap = 8;
    var pad = 12;
    pop.style.left = '';
    pop.style.right = '';
    pop.style.top = '';

    var popW = pop.offsetWidth || Math.min(420, window.innerWidth - 24);
    var left = rect.left + rect.width / 2 - popW / 2;
    left = Math.max(pad, Math.min(left, window.innerWidth - popW - pad));
    pop.style.left = left + 'px';

    var below = rect.bottom + gap;
    var popH = pop.offsetHeight;
    if (below + popH > window.innerHeight - pad && rect.top - gap - popH > pad) {
      pop.style.top = Math.max(pad, rect.top - gap - popH) + 'px';
    } else {
      pop.style.top = below + 'px';
    }
  }

  function showPopover(btn, key) {
    var entry = registry[key];
    if (!entry) return;
    clearTimeout(hideTimer);
    var pop = ensurePopover();
    pop.innerHTML = '';
    pop.appendChild(buildPopoverContent(entry));
    pop.hidden = false;
    pop.classList.add('is-visible');
    pop.setAttribute('data-for', key);
    activeBtn = btn;
    btn.classList.add('is-open');
    btn.setAttribute('aria-expanded', 'true');
    /* Выше модалок админки (часто z-index: 99999) */
    var zBase = 100100;
    var node = btn.parentElement;
    while (node && node !== document.body) {
      var z = parseInt(window.getComputedStyle(node).zIndex, 10);
      if (!isNaN(z) && z >= zBase) zBase = z + 2;
      node = node.parentElement;
    }
    pop.style.zIndex = String(zBase);
    requestAnimationFrame(function () { positionPopover(btn); });
  }

  function hidePopover() {
    if (!popoverEl) return;
    popoverEl.classList.remove('is-visible');
    popoverEl.hidden = true;
    if (activeBtn) {
      activeBtn.classList.remove('is-open');
      activeBtn.setAttribute('aria-expanded', 'false');
      activeBtn = null;
    }
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hidePopover, 120);
  }

  function cancelHide() {
    clearTimeout(hideTimer);
  }

  function bindTrigger(btn) {
    if (btn.dataset.adminInfoBound === '1') return;
    btn.dataset.adminInfoBound = '1';
    var key = btn.getAttribute('data-admin-info');
    if (!key || !registry[key]) {
      btn.style.display = 'none';
      return;
    }

    btn.setAttribute('aria-expanded', 'false');

    btn.addEventListener('mouseenter', function () {
      cancelHide();
      showPopover(btn, key);
    });
    btn.addEventListener('mouseleave', scheduleHide);
    btn.addEventListener('focus', function () {
      cancelHide();
      showPopover(btn, key);
    });
    btn.addEventListener('blur', scheduleHide);

    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (activeBtn === btn && popoverEl && popoverEl.classList.contains('is-visible')) {
        hidePopover();
      } else {
        showPopover(btn, key);
      }
    });
  }

  function init(root) {
    registry = global.AdminInfoRegistry || {};
    var scope = root || document;
    scope.querySelectorAll('[data-admin-info]').forEach(bindTrigger);

    if (!global.__adminInfoGlobalBound) {
      global.__adminInfoGlobalBound = true;
      ensurePopover();
      popoverEl.addEventListener('mouseenter', cancelHide);
      popoverEl.addEventListener('mouseleave', scheduleHide);
      document.addEventListener('click', function (e) {
        if (!activeBtn) return;
        if (e.target.closest('.admin-info-btn') || e.target.closest('.admin-info-popover')) return;
        hidePopover();
      });
      window.addEventListener('resize', function () {
        if (activeBtn && popoverEl && popoverEl.classList.contains('is-visible')) {
          positionPopover(activeBtn);
        }
      });
      window.addEventListener('scroll', function () {
        if (activeBtn && popoverEl && popoverEl.classList.contains('is-visible')) {
          positionPopover(activeBtn);
        }
      }, true);
    }
  }

  global.AdminInfo = {
    IMAGE_BASE: ADMIN_INFO_IMAGE_BASE,
    init: init,
    register: function (key, entry) {
      registry[key] = entry;
      global.AdminInfoRegistry = registry;
    },
  };
})(window);
