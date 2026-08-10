"""Справочник «код модели → маркетинговое имя».

Источники:
    1) device_names.apple_devices.APPLE_DEVICES — захардкоженный список
                                               iPhone/iPad/iPod/Watch.
                                               Без обращения к интернету,
                                               загружается всегда.
    2) Google Play supported_devices.csv     — Android, качается с сети, ~40k.
                                               Кэш: device_names/devices.json
                                               (только Google, без Apple).

Локально:
    3) device_names/devices_overrides.json   — ручные правки. Применяются
                                               ПОВЕРХ всех источников и не
                                               затираются автообновлением.
                                               Используй для редких моделей,
                                               которых нет у Google
                                               (например, китайский Huawei
                                               LRT-W09 → "HUAWEI MatePad 12X").

Файл `devices.json` обновляется в фоне раз в `DEVICE_NAMES_TTL_DAYS` дней,
а также если он подозрительно маленький (< 1000 записей — признак того,
что прошлая попытка Google провалилась).

Файл `device_names/devices.json` создаётся автоматически в фоне при старте
сервиса (см. `refresh_device_names_if_stale`). Подмена работает «на лету»
при отдаче `/devices/{uuid}`; БД остаётся с заводскими кодами.

Публичный API модуля:
    DEVICE_NAMES_DB                  — словарь в памяти (только чтение).
    load_device_names()              — синхронно, при старте.
    refresh_device_names_if_stale()  — фоновая задача с httpx.AsyncClient.
    prettify_device_model(raw)       — основная функция подмены.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from datetime import datetime, timezone

import httpx

from .apple_devices import APPLE_DEVICES

__all__ = [
    "DEVICE_NAMES_DB",
    "DEVICE_NAMES_FILE",
    "DEVICE_NAMES_OVERRIDES_FILE",
    "DEVICE_NAMES_TTL_DAYS",
    "load_device_names",
    "prettify_device_model",
    "refresh_device_names_if_stale",
]


# ── Пути ─────────────────────────────────────────────────────────────────────
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
DEVICE_NAMES_FILE = os.path.join(_PKG_DIR, "devices.json")
DEVICE_NAMES_OVERRIDES_FILE = os.path.join(_PKG_DIR, "devices_overrides.json")

# ── Константы ────────────────────────────────────────────────────────────────
DEVICE_NAMES_DB: dict[str, str] = {}
DEVICE_NAMES_TTL_DAYS = 7  # автообновление — раз в неделю

ANDROID_DEVICES_URL = "https://storage.googleapis.com/play_public/supported_devices.csv"

# Эти токены НЕ ищем в справочнике — они часто встречаются как архитектура
# CPU или общие слова. Из-за Apple-gist (`x86_64 : iPhone Simulator`) могут
# давать ложные срабатывания.
_BLACKLIST_TOKENS: set[str] = {
    "x86", "x86_64", "x64", "amd64", "i386", "i486", "i586", "i686", "ia64",
    "arm", "arm32", "arm64", "aarch64", "aarch32",
    "armv5", "armv6", "armv7", "armv7l", "armv7a", "armv7-a",
    "armv8", "armv8a", "armv8-a",
    "32", "64", "32-bit", "64-bit", "32bit", "64bit",
    "pc", "desktop", "laptop", "computer", "machine", "device", "unknown",
}

# Распознаёт «Windows 11 amd64», «Linux x86_64», «macOS 14.2» и т.п.
_OS_ARCH_TAIL_RE = re.compile(
    r"\s*\b("
    r"x86_64|x86|x64|amd64|i[3-6]86|ia64|"
    r"arm64|aarch64|aarch32|"
    r"armv5\w*|armv6\w*|armv7\w*|armv8\w*|arm32|arm|"
    r"32-?bit|64-?bit"
    r")\b\s*$",
    re.IGNORECASE,
)
_OS_HEAD_RE = re.compile(
    r"^\s*(?P<os>"
    r"windows\s*nt|windows|win(?:32|64|nt)?|"
    r"mac\s*os\s*x|mac\s*os|macos|osx|darwin|"
    r"gnu/linux|linux|ubuntu|debian|fedora|arch|manjaro|mint|"
    r"chrome\s*os|chromeos|"
    r"freebsd|openbsd|netbsd"
    r")\s*"
    r"(?P<ver>\d[\d\.]*[a-z]*)?"  # необязательная версия
    r"\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_OS_FAMILY_PRETTY: dict[str, tuple[str, str]] = {
    # canonical (lower, без пробелов): (display_name, suffix)
    "windows":   ("Windows", " PC"),
    "win":       ("Windows", " PC"),
    "win32":     ("Windows", " PC"),
    "win64":     ("Windows", " PC"),
    "winnt":     ("Windows", " PC"),
    "windowsnt": ("Windows", " PC"),
    "linux":     ("Linux", " PC"),
    "gnulinux":  ("Linux", " PC"),
    "ubuntu":    ("Ubuntu", " PC"),
    "debian":    ("Debian", " PC"),
    "fedora":    ("Fedora", " PC"),
    "arch":      ("Arch Linux", " PC"),
    "manjaro":   ("Manjaro", " PC"),
    "mint":      ("Linux Mint", " PC"),
    "macos":     ("Mac", ""),
    "macosx":    ("Mac", ""),
    "osx":       ("Mac", ""),
    "darwin":    ("Mac", ""),
    "chromeos":  ("ChromeOS", ""),
    "freebsd":   ("FreeBSD", " PC"),
    "openbsd":   ("OpenBSD", " PC"),
    "netbsd":    ("NetBSD", " PC"),
}

# Generic-нормализация когда вся строка — одно слово ОС.
_GENERIC_MODEL_ALIASES: dict[str, str] = {
    "windows":   "Windows PC",
    "win":       "Windows PC",
    "win32":     "Windows PC",
    "win64":     "Windows PC",
    "winnt":     "Windows PC",
    "linux":     "Linux PC",
    "gnu/linux": "Linux PC",
    "ubuntu":    "Ubuntu PC",
    "debian":    "Debian PC",
    "fedora":    "Fedora PC",
    "arch":      "Arch Linux PC",
    "macos":     "Mac",
    "mac os":    "Mac",
    "mac os x":  "Mac",
    "osx":       "Mac",
    "darwin":    "Mac",
    "android":   "Android",
    "ios":       "iPhone/iPad",
    "ipados":    "iPad",
    "tvos":      "Apple TV",
    "watchos":   "Apple Watch",
    "chromeos":  "ChromeOS",
    "chrome os": "ChromeOS",
    "freebsd":   "FreeBSD PC",
    "openbsd":   "OpenBSD PC",
    "pc":        "Windows PC",
}


# ── Чтение / нормализация ────────────────────────────────────────────────────
def _load_overrides() -> dict[str, str]:
    """Читает devices_overrides.json. Любые ошибки → пустой dict."""
    if not os.path.exists(DEVICE_NAMES_OVERRIDES_FILE):
        return {}
    try:
        with open(DEVICE_NAMES_OVERRIDES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logging.warning(
                "[device_names] %s ожидался dict, получено %s — пропускаем",
                DEVICE_NAMES_OVERRIDES_FILE, type(data).__name__,
            )
            return {}
        return {
            str(k): str(v)
            for k, v in data.items()
            if k and v and not str(k).startswith("_")
        }
    except Exception as e:
        logging.warning(
            "[device_names] Не удалось прочитать %s: %s",
            DEVICE_NAMES_OVERRIDES_FILE, e,
        )
        return {}


def load_device_names() -> None:
    """Подгружает встроенный Apple + devices.json + overrides в память. Без сети."""
    global DEVICE_NAMES_DB
    base: dict[str, str] = {}

    # 1. Захардкоженный Apple-список — всегда доступен, даже при первом старте.
    for k, v in APPLE_DEVICES.items():
        if not _is_bad_pretty(v):
            base[k] = v

    # 2. Кэш Android от Google, если уже скачан.
    if os.path.exists(DEVICE_NAMES_FILE):
        try:
            with open(DEVICE_NAMES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(
                    "ожидался dict, получено: %s" % type(data).__name__
                )
            for k, v in data.items():
                if k not in base:  # Apple всегда побеждает Google.
                    base[k] = v
        except Exception as e:
            logging.error(
                "[device_names] Не удалось прочитать %s: %s",
                DEVICE_NAMES_FILE, e,
            )
    else:
        logging.info(
            "[device_names] %s не найден — пробуем скачать справочник в фоне.",
            DEVICE_NAMES_FILE,
        )

    # 3. Локальные правки — самый высокий приоритет.
    overrides = _load_overrides()
    if overrides:
        base.update(overrides)
        logging.info(
            "[device_names] Применены override-ы: %d записей из %s",
            len(overrides), DEVICE_NAMES_OVERRIDES_FILE,
        )

    DEVICE_NAMES_DB = base
    logging.info(
        "[device_names] В памяти: %d моделей (Apple встроенный: %d)",
        len(DEVICE_NAMES_DB), len(APPLE_DEVICES),
    )


def _is_bad_pretty(name: str) -> bool:
    """Защита от очевидно ложных срабатываний (Apple-симуляторы и пр.)."""
    if not name:
        return True
    low = name.lower()
    return "simulator" in low or "emulator" in low


# Apple-устройства: ОС-маркеры и формат model-кодов.
_APPLE_OS_RE = re.compile(
    r"\b(ios|ipados|macos|mac\s*os|os\s*x|watchos|tvos|visionos|darwin)\b",
    re.IGNORECASE,
)
_APPLE_CODE_RE = re.compile(
    r"^(?:iPhone|iPad|iPod|Watch|AppleTV|TV|AudioAccessory|RealityDevice|HomePod|"
    r"MacBookPro|MacBookAir|MacBook|MacPro|MacMini|iMac|Mac)\d+,\d+$"
)
_APPLE_NAME_PREFIXES = (
    "iphone", "ipad", "ipod", "apple watch", "apple tv", "homepod",
    "macbook", "imac", "mac mini", "mac pro", "mac studio", "mac ", "vision pro",
)


def _looks_like_apple_os(os_value) -> bool:
    if not os_value or not isinstance(os_value, str):
        return False
    return bool(_APPLE_OS_RE.search(os_value))


def _looks_like_apple_code(raw: str) -> bool:
    return bool(_APPLE_CODE_RE.match(raw))


def _looks_like_apple_marketing_name(raw: str) -> bool:
    low = raw.strip().lower()
    return low.startswith(_APPLE_NAME_PREFIXES)


def _lookup_model_code(token: str) -> str | None:
    """Точное совпадение кода с учётом регистра. Архитектуры пропускаются.

    Apple-формат (содержит запятую: «iPhone14,5») — только точное совпадение,
    UPPER не пробуем, чтобы случайный одноимённый Android-ключ не выиграл.
    """
    if not token:
        return None
    if token.lower() in _BLACKLIST_TOKENS:
        return None
    if token in DEVICE_NAMES_DB:
        val = DEVICE_NAMES_DB[token]
        return None if _is_bad_pretty(val) else val
    if "," in token:
        return None  # Apple-формат, без UPPER-фолбэка
    upper = token.upper()
    if upper != token and upper in DEVICE_NAMES_DB:
        val = DEVICE_NAMES_DB[upper]
        return None if _is_bad_pretty(val) else val
    return None


def _normalize_os_string(raw: str) -> str | None:
    """«Windows 11 amd64» → «Windows 11 PC», «Linux x86_64» → «Linux PC».

    Возвращает None, если строка не похожа на OS-описание.
    """
    if not raw:
        return None
    s = raw.strip()
    while True:  # «Linux 5.15 x86_64»
        new = _OS_ARCH_TAIL_RE.sub("", s).strip()
        if new == s:
            break
        s = new
    if not s:
        return None
    m = _OS_HEAD_RE.match(s)
    if not m:
        return None
    os_token = re.sub(r"\s+", "", m.group("os")).lower()  # «Mac OS X» → «macosx»
    pretty = _OS_FAMILY_PRETTY.get(os_token)
    if not pretty:
        return None
    name, suffix = pretty
    ver = (m.group("ver") or "").strip()
    rest = (m.group("rest") or "").strip()
    if rest:
        if ver:
            return f"{name} {ver} {rest}{suffix}".strip()
        return f"{name} {rest}{suffix}".strip()
    if ver:
        return f"{name} {ver}{suffix}".strip()
    return f"{name}{suffix}".strip()


def prettify_device_model(raw_model, os: str | None = None) -> str:
    """Возвращает «красивое» название модели, если оно есть в справочнике.

    Параметры:
        raw_model: значение поля model клиента.
        os:       значение поля os клиента (если известно). При Apple-ОС
                  (iOS/iPadOS/macOS/watchOS/tvOS/Darwin) подмена выполняется
                  ТОЛЬКО для канонических Apple-кодов (iPhoneNN,N) — это
                  защищает от случайных «AXXA PRO», «My iPhone», «Galaxy»
                  и т.п. в model, которые шлют сторонние клиенты.

    Поддерживаемые варианты входа:
        2407FPN8EG               — точное совпадение
        2407fpn8eg               — нижний регистр (нормализуем UPPER)
        xiaomi 2407fpn8eg        — «бренд + код», ищем код в токенах
        iPhone14,5               — точное совпадение Apple-формата
        iPhone 13 / Galaxy S23   — уже маркетинговое имя, остаётся как есть
        Windows / Linux / Mac    — generic, «Windows PC», «Mac»…
        Windows 11 amd64         — отрезаем архитектуру → «Windows 11 PC»
        Linux x86_64             → «Linux PC»

    Порядок проверок:
    1. Пустое/нестроковое — вернуть как есть.
    2. Если ОС Apple — допускается ТОЛЬКО точное совпадение Apple-кода;
       любые другие значения возвращаются как есть.
    3. Точное совпадение всей строки в DEVICE_NAMES_DB.
    4. Распознавание ОС через _normalize_os_string.
    5. Токенный поиск в DEVICE_NAMES_DB.
    6. Fallback на _GENERIC_MODEL_ALIASES.
    7. Иначе — оригинал.
    """
    if not raw_model or not isinstance(raw_model, str):
        return raw_model
    raw = raw_model.strip()
    if not raw:
        return raw_model

    # Apple-ОС: жёсткий режим — Android-логика отключена целиком.
    if _looks_like_apple_os(os):
        if _looks_like_apple_code(raw):
            val = DEVICE_NAMES_DB.get(raw) if DEVICE_NAMES_DB else None
            if val and not _is_bad_pretty(val):
                return val
        return raw_model

    if DEVICE_NAMES_DB:
        found = _lookup_model_code(raw)
        if found:
            return found

    os_pretty = _normalize_os_string(raw)
    if os_pretty:
        return os_pretty

    if DEVICE_NAMES_DB:
        parts = raw.split()
        if len(parts) > 1:
            for tok in parts:
                found = _lookup_model_code(tok)
                if found:
                    return found

    generic = _GENERIC_MODEL_ALIASES.get(raw.lower())
    if generic:
        return generic

    return raw_model


# ── Источники онлайн ─────────────────────────────────────────────────────────
def _devices_file_age_days() -> float | None:
    """Возраст devices.json в днях. None — если файла нет."""
    try:
        mtime = os.path.getmtime(DEVICE_NAMES_FILE)
    except OSError:
        return None
    return max(0.0, (datetime.now(timezone.utc).timestamp() - mtime) / 86400.0)


def _devices_file_record_count() -> int | None:
    """Сколько ключей в devices.json. None — если файла нет/битый."""
    if not os.path.exists(DEVICE_NAMES_FILE):
        return None
    try:
        with open(DEVICE_NAMES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return len(data)
    except Exception:
        pass
    return None


def _parse_android_csv(text: str) -> dict[str, str]:
    """Общий парсер 4-колоночного CSV (Brand, Marketing, Device, Model)."""
    out: dict[str, str] = {}
    reader = csv.reader(io.StringIO(text))
    try:
        next(reader)  # header
    except StopIteration:
        return out
    for row in reader:
        if len(row) < 4:
            continue
        brand = (row[0] or "").strip()
        marketing = (row[1] or "").strip()
        model_code = (row[3] or "").strip()
        if not model_code or not marketing:
            continue
        if brand and brand.lower() not in marketing.lower():
            full = f"{brand} {marketing}"
        else:
            full = marketing
        prev = out.get(model_code)
        if not prev or len(full) > len(prev):
            out[model_code] = full
    return out


async def _fetch_android_models(client: httpx.AsyncClient) -> dict[str, str]:
    """Парсит Google Play supported_devices.csv (UTF-16)."""
    resp = await client.get(ANDROID_DEVICES_URL, timeout=30.0)
    resp.raise_for_status()
    text = resp.content.decode("utf-16", errors="ignore")
    return _parse_android_csv(text)


async def refresh_device_names_if_stale(http_client: httpx.AsyncClient) -> None:
    """Если файл отсутствует или старше DEVICE_NAMES_TTL_DAYS — скачивает Google.

    Файл `devices.json` хранит ТОЛЬКО Google CSV (Android). Apple-список
    встроен в код и подключается из памяти, override-ы — из отдельного JSON.
    Если Google не отвечает, файл НЕ перезаписывается, чтобы при следующем
    старте можно было ещё раз попытаться (TTL не сбрасывается).

    Запускается из lifespan. Любые ошибки логируются, но не пробрасываются —
    сервис должен подняться даже без интернета.
    """
    try:
        age = _devices_file_age_days()
        cached_size = _devices_file_record_count()

        # Подозрительно маленький кэш (например, прошлая попытка Google
        # положила в файл только Apple ~250 записей). Лечим перекачкой.
        cache_too_small = cached_size is not None and cached_size < 1000

        if age is not None and age < DEVICE_NAMES_TTL_DAYS and not cache_too_small:
            logging.info(
                "[device_names] Справочник свежий (возраст %.1f дн., %d записей), "
                "обновление не требуется.",
                age, cached_size or 0,
            )
            return

        if age is None:
            logging.info("[device_names] Скачиваем свежий справочник устройств…")
        elif cache_too_small:
            logging.info(
                "[device_names] Кэш подозрительно мал (%d записей) — перекачиваем.",
                cached_size or 0,
            )
        else:
            logging.info(
                "[device_names] Справочник устарел (возраст %.1f дн.), обновляем…",
                age,
            )

        # Google — единственный сетевой источник. Если упал — файл не трогаем.
        try:
            google_res = await _fetch_android_models(http_client)
        except Exception as e:
            logging.warning("[device_names] Android (Google) не загрузился: %s", e)
            return

        if not google_res:
            logging.warning(
                "[device_names] Google вернул пустой CSV — файл не трогаем."
            )
            return

        logging.info(
            "[device_names] Android (Google): %d моделей получено", len(google_res)
        )

        # Записываем атомарно — только Google.
        tmp = DEVICE_NAMES_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(google_res, f, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, DEVICE_NAMES_FILE)
        except Exception as e:
            logging.error(
                "[device_names] Не удалось записать %s: %s", DEVICE_NAMES_FILE, e
            )
            try:
                os.remove(tmp)
            except OSError:
                pass
            return

        # Пересобираем in-memory карту: Apple → Google → overrides.
        merged: dict[str, str] = {}
        for k, v in APPLE_DEVICES.items():
            if not _is_bad_pretty(v):
                merged[k] = v
        for k, v in google_res.items():
            if k not in merged:
                merged[k] = v
        overrides = _load_overrides()
        if overrides:
            merged.update(overrides)

        DEVICE_NAMES_DB.clear()
        DEVICE_NAMES_DB.update(merged)
        logging.info(
            "[device_names] Справочник обновлён: %d моделей (Apple: %d, Google: %d, overrides: %d)",
            len(merged), len(APPLE_DEVICES), len(google_res), len(overrides),
        )
    except Exception as e:
        logging.error("[device_names] refresh_device_names_if_stale: %s", e)
