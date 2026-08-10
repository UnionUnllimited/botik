/**
 * Страны для админки: ISO-код → флаг + английское имя (🇩🇪 Germany).
 * Используется в Remnawave SSH-установке и может подключаться на других страницах.
 */
(function (global) {
  'use strict';

  var ISO_CODES = (
    'AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ ' +
    'CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET EU FI FJ FK FM FO FR ' +
    'GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP ' +
    'KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT ' +
    'MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW ' +
    'SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG ' +
    'UM US UY UZ VA VC VE VG VI VN VU WF WS XX YE YT ZA ZM ZW'
  ).trim().split(/\s+/);

  var OVERRIDES = {
    EU: 'European Union',
    XX: 'Other',
  };

  var displayNames = null;
  try {
    displayNames = new Intl.DisplayNames(['en'], { type: 'region' });
  } catch (_) {}

  function countryName(code) {
    var c = String(code || '').toUpperCase();
    if (OVERRIDES[c]) return OVERRIDES[c];
    if (displayNames) {
      try {
        var n = displayNames.of(c);
        if (n && n !== c) return n;
      } catch (_) {}
    }
    return c;
  }

  function countryFlag(code) {
    var c = String(code || '').toUpperCase();
    if (c === 'XX') return '🌐';
    if (c.length !== 2) return '';
    var a = c.charCodeAt(0);
    var b = c.charCodeAt(1);
    if (a < 65 || a > 90 || b < 65 || b > 90) return '';
    return String.fromCodePoint(0x1F1E6 + a - 65, 0x1F1E6 + b - 65);
  }

  function countryLabel(code) {
    var c = String(code || '').toUpperCase();
    if (!c) return '';
    return countryFlag(c) + ' ' + countryName(c);
  }

  var LIST = ISO_CODES.map(function (code) {
    return { code: code, name: countryName(code), flag: countryFlag(code) };
  }).sort(function (a, b) {
    return a.name.localeCompare(b.name, 'en');
  });

  var BY_CODE = {};
  LIST.forEach(function (item) {
    BY_CODE[item.code] = item;
  });

  function fillSelect(selectEl, selectedCode) {
    if (!selectEl) return;
    var sel = String(selectedCode || 'XX').toUpperCase();
    selectEl.innerHTML = LIST.map(function (item) {
      var label = item.flag + ' ' + item.name;
      var selected = item.code === sel ? ' selected' : '';
      return '<option value="' + item.code + '"' + selected + '>' + label + '</option>';
    }).join('');
  }

  function fillFlagMenu(menuEl, onPick) {
    if (!menuEl) return;
    menuEl.innerHTML = LIST.map(function (item) {
      return (
        '<button type="button" class="rw-flag-item w-full text-left flex items-center gap-2 px-3 py-1.5 text-[13px] hover:bg-[var(--admin-bg-soft)]" ' +
        'style="color: var(--admin-text-base);" ' +
        'data-code="' + item.code + '" data-flag="' + item.flag + '" data-name="' + item.name.replace(/"/g, '&quot;') + '">' +
        '<span class="text-[15px] leading-none">' + item.flag + '</span> ' + item.name +
        '</button>'
      );
    }).join('');

    menuEl.querySelectorAll('.rw-flag-item').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (typeof onPick === 'function') {
          onPick({
            code: btn.getAttribute('data-code') || '',
            flag: btn.getAttribute('data-flag') || '',
            name: btn.getAttribute('data-name') || '',
          });
        }
        menuEl.classList.add('hidden');
      });
    });
  }

  function toggleFlagDropdown(e, btn) {
    e.preventDefault();
    e.stopPropagation();
    var wrap = btn.closest('.tw-dropdown');
    if (!wrap) return;
    var menu = wrap.querySelector('.tw-dropdown-menu');
    if (!menu) return;
    var wasOpen = !menu.classList.contains('hidden');
    document.querySelectorAll('.tw-dropdown-menu').forEach(function (m) {
      m.classList.add('hidden');
    });
    if (!wasOpen) menu.classList.remove('hidden');
  }

  global.AdminCountries = {
    list: LIST,
    byCode: BY_CODE,
    flag: countryFlag,
    name: countryName,
    label: countryLabel,
    fillSelect: fillSelect,
    fillFlagMenu: fillFlagMenu,
    toggleFlagDropdown: toggleFlagDropdown,
  };
})(typeof window !== 'undefined' ? window : this);
