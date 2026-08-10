/**
 * Twemoji Country Flags polyfill.
 *
 * Делаем две вещи:
 *   1) Инжектим @font-face — чтобы font-family-стек "Twemoji Country Flags"
 *      работал когда на странице появляются флаги.
 *   2) Принудительно грузим woff2 через FontFace API и добавляем в
 *      document.fonts. Это нужно потому, что @font-face с unicode-range
 *      ленив: если на ТЕКУЩЕЙ странице нет ни одного флага — браузер не
 *      запросит woff2 и шрифт не зарегистрируется в FontFaceSet. Тогда при
 *      переходе на страницу со списком серверов / клиентами браузер увидит
 *      флаги, но Twemoji ещё не подгружен — будет фолбэк на системный
 *      шрифт (на Windows = две буквы CH/RU).
 *
 *   FontFace.load() триггерит сетевой запрос мгновенно, шрифт оказывается
 *   в document.fonts ДО того, как пользователь перешёл на страницу с
 *   флагами. После этого он мгновенно отрендерится.
 *
 * Никакой canvas-детекции: на разных Windows-сборках Chrome она врёт.
 */
export function polyfillCountryFlagEmojis(
  family = "Twemoji Country Flags",
  url = "https://cdn.jsdelivr.net/npm/country-flag-emoji-polyfill@0.1/dist/TwemojiCountryFlags.woff2"
) {
  if (typeof window === "undefined" || !document || !document.head) return false;

  const UNICODE_RANGE =
    "U+1F1E6-1F1FF, U+1F3F4, U+E0062-E0063, U+E0065, U+E0067, " +
    "U+E006C, U+E006E, U+E0073-E0074, U+E0077, U+E007F";

  // 1) @font-face через CSS — обеспечивает корректное использование шрифта
  //    в font-family-стеке (см. base-shell.css).
  const styleId = "__country-flag-polyfill-style";
  if (!document.getElementById(styleId)) {
    const style = document.createElement("style");
    style.id = styleId;
    style.textContent = `@font-face {
  font-family: "${family}";
  unicode-range: ${UNICODE_RANGE};
  src: url('${url}') format('woff2');
  font-display: swap;
}`;
    document.head.appendChild(style);
  }

  // 2) Принудительно скачиваем шрифт через FontFace API.
  //    Без этого, если на странице нет ни одного флага, woff2 не запросится
  //    из-за unicode-range — и при переходе на страницу с флагами они
  //    отрендерятся системным шрифтом (буквами).
  try {
    if (typeof FontFace !== "undefined" && document.fonts) {
      const ff = new FontFace(family, `url('${url}') format('woff2')`, {
        unicodeRange: UNICODE_RANGE,
        display: "swap",
      });
      ff.load()
        .then((loaded) => {
          try { document.fonts.add(loaded); } catch (_) {}
        })
        .catch(() => {
          // Тихо игнорируем — на странице всё ещё работает CSS @font-face fallback.
        });
    }
  } catch (_) {}

  return true;
}
