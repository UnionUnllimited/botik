"""Country patterns for auto-grouping hosts by remark/tag."""

from __future__ import annotations

import re

_RENEWAL_DATE_IN_NAME_RE = re.compile(r'\[\d{2}\.\d{2}\.\d{4}\]')
_FLAG_RE = re.compile(r'(?:[\U0001F1E6-\U0001F1FF]{2}\s*)+')
_TRAILING_SUFFIX_RE = re.compile(
    r'[\s\-_#]+(?:\d+|main|backup|vip|primary|secondary)\s*$',
    re.I,
)

COUNTRY_PATTERNS: dict[str, dict] = {
    # Western Europe
    'Finland': {'emoji': '🇫🇮', 'label': 'Финляндия', 'patterns': ['Finland', 'Финлянд', 'FI', 'Helsinki']},
    'Germany': {'emoji': '🇩🇪', 'label': 'Германия', 'patterns': ['Germany', 'German', 'Германи', 'DE', 'Berlin', 'Frankfurt']},
    'Netherlands': {'emoji': '🇳🇱', 'label': 'Нидерланды', 'patterns': ['Netherlands', 'Нидерланд', 'NL', 'Amsterdam', 'Holland']},
    'France': {'emoji': '🇫🇷', 'label': 'Франция', 'patterns': ['France', 'Франци', 'FR', 'Paris']},
    'UK': {'emoji': '🇬🇧', 'label': 'Великобритания', 'patterns': ['United Kingdom', 'UK', 'Британи', 'GB', 'London', 'England']},
    'Switzerland': {'emoji': '🇨🇭', 'label': 'Швейцария', 'patterns': ['Switzerland', 'Swiss', 'Швейцар', 'CH', 'Zurich', 'Женев']},
    'Austria': {'emoji': '🇦🇹', 'label': 'Австрия', 'patterns': ['Austria', 'Австри', 'AT', 'Vienna']},
    'Belgium': {'emoji': '🇧🇪', 'label': 'Бельгия', 'patterns': ['Belgium', 'Бельги', 'BE', 'Brussels']},
    'Luxembourg': {'emoji': '🇱🇺', 'label': 'Люксембург', 'patterns': ['Luxembourg', 'Люксембург', 'LU']},
    'Ireland': {'emoji': '🇮🇪', 'label': 'Ирландия', 'patterns': ['Ireland', 'Ирланди', 'IE', 'Dublin']},
    'Portugal': {'emoji': '🇵🇹', 'label': 'Португалия', 'patterns': ['Portugal', 'Португал', 'PT', 'Lisbon']},
    'Spain': {'emoji': '🇪🇸', 'label': 'Испания', 'patterns': ['Spain', 'Испани', 'ES', 'Madrid']},
    'Italy': {'emoji': '🇮🇹', 'label': 'Италия', 'patterns': ['Italy', 'Итали', 'IT', 'Rome', 'Milan']},
    'Greece': {'emoji': '🇬🇷', 'label': 'Греция', 'patterns': ['Greece', 'Греци', 'GR', 'Athens']},
    # Northern Europe
    'Sweden': {'emoji': '🇸🇪', 'label': 'Швеция', 'patterns': ['Sweden', 'Швеци', 'SE', 'Stockholm']},
    'Norway': {'emoji': '🇳🇴', 'label': 'Норвегия', 'patterns': ['Norway', 'Норвег', 'NO', 'Oslo']},
    'Denmark': {'emoji': '🇩🇰', 'label': 'Дания', 'patterns': ['Denmark', 'Дани', 'DK', 'Copenhagen']},
    'Iceland': {'emoji': '🇮🇸', 'label': 'Исландия', 'patterns': ['Iceland', 'Исланд', 'IS', 'Reykjavik']},
    # Eastern Europe
    'Poland': {'emoji': '🇵🇱', 'label': 'Польша', 'patterns': ['Poland', 'Польш', 'PL', 'Warsaw']},
    'Czech': {'emoji': '🇨🇿', 'label': 'Чехия', 'patterns': ['Czech', 'Чехи', 'CZ', 'Prague']},
    'Slovakia': {'emoji': '🇸🇰', 'label': 'Словакия', 'patterns': ['Slovakia', 'Словак', 'SK', 'Bratislava']},
    'Hungary': {'emoji': '🇭🇺', 'label': 'Венгрия', 'patterns': ['Hungary', 'Венгр', 'HU', 'Budapest']},
    'Romania': {'emoji': '🇷🇴', 'label': 'Румыния', 'patterns': ['Romania', 'Румыни', 'RO']},
    'Bulgaria': {'emoji': '🇧🇬', 'label': 'Болгария', 'patterns': ['Bulgaria', 'Болгари', 'BG']},
    'Latvia': {'emoji': '🇱🇻', 'label': 'Латвия', 'patterns': ['Latvia', 'Латви', 'LV', 'Riga']},
    'Lithuania': {'emoji': '🇱🇹', 'label': 'Литва', 'patterns': ['Lithuania', 'Литв', 'LT', 'Vilnius']},
    'Estonia': {'emoji': '🇪🇪', 'label': 'Эстония', 'patterns': ['Estonia', 'Эстони', 'EE', 'Tallinn']},
    'Ukraine': {'emoji': '🇺🇦', 'label': 'Украина', 'patterns': ['Ukraine', 'Украин', 'UA', 'Kyiv', 'Kiev']},
    'Moldova': {'emoji': '🇲🇩', 'label': 'Молдова', 'patterns': ['Moldova', 'Молдов', 'MD']},
    'Belarus': {'emoji': '🇧🇾', 'label': 'Беларусь', 'patterns': ['Belarus', 'Беларус', 'BY', 'Minsk']},
    'Russia': {'emoji': '🇷🇺', 'label': 'Россия', 'patterns': ['Russia', 'Russian', 'Росси', 'RU', 'Moscow', 'Москв']},
    # Balkans
    'Slovenia': {'emoji': '🇸🇮', 'label': 'Словения', 'patterns': ['Slovenia', 'Сloven', 'SI', 'Ljubljana']},
    'Croatia': {'emoji': '🇭🇷', 'label': 'Хорватия', 'patterns': ['Croatia', 'Хорват', 'HR', 'Zagreb']},
    'Serbia': {'emoji': '🇷🇸', 'label': 'Сербия', 'patterns': ['Serbia', 'Серб', 'RS', 'Belgrade']},
    'Bosnia': {'emoji': '🇧🇦', 'label': 'Босния', 'patterns': ['Bosnia', 'Босни', 'BA', 'Sarajevo']},
    'NorthMacedonia': {'emoji': '🇲🇰', 'label': 'Северная Македония', 'patterns': ['North Macedonia', 'Macedonia', 'Македон', 'MK']},
    'Albania': {'emoji': '🇦🇱', 'label': 'Албания', 'patterns': ['Albania', 'Албани', 'AL', 'Tirana']},
    'Montenegro': {'emoji': '🇲🇪', 'label': 'Черногория', 'patterns': ['Montenegro', 'Черногор', 'ME']},
    'Cyprus': {'emoji': '🇨🇾', 'label': 'Кипр', 'patterns': ['Cyprus', 'Кипр', 'CY', 'Nicosia']},
    'Malta': {'emoji': '🇲🇹', 'label': 'Мальта', 'patterns': ['Malta', 'Мальт', 'MT']},
    # CIS / Central Asia
    'Kazakhstan': {'emoji': '🇰🇿', 'label': 'Казахстан', 'patterns': ['Kazakhstan', 'Казахстан', 'KZ', 'Almaty']},
    'Kyrgyzstan': {'emoji': '🇰🇬', 'label': 'Киргизия', 'patterns': ['Kyrgyzstan', 'Киргиз', 'KG', 'Bishkek']},
    'Uzbekistan': {'emoji': '🇺🇿', 'label': 'Узбекистан', 'patterns': ['Uzbekistan', 'Узбек', 'UZ', 'Tashkent']},
    'Tajikistan': {'emoji': '🇹🇯', 'label': 'Таджикистан', 'patterns': ['Tajikistan', 'Таджик', 'TJ']},
    'Armenia': {'emoji': '🇦🇲', 'label': 'Армения', 'patterns': ['Armenia', 'Армен', 'AM', 'Yerevan']},
    'Azerbaijan': {'emoji': '🇦🇿', 'label': 'Азербайджан', 'patterns': ['Azerbaijan', 'Азербайдж', 'AZ', 'Baku']},
    'Georgia': {'emoji': '🇬🇪', 'label': 'Грузия', 'patterns': ['Georgia', 'Груз', 'GE', 'Tbilisi']},
    'Mongolia': {'emoji': '🇲🇳', 'label': 'Монголия', 'patterns': ['Mongolia', 'Монгол', 'MN']},
    # Americas
    'USA': {'emoji': '🇺🇸', 'label': 'США', 'patterns': ['USA', 'United States', 'Америк', 'US', 'New York', 'Los Angeles', 'Dallas']},
    'Canada': {'emoji': '🇨🇦', 'label': 'Канада', 'patterns': ['Canada', 'Канад', 'CA', 'Toronto']},
    'Brazil': {'emoji': '🇧🇷', 'label': 'Бразилия', 'patterns': ['Brazil', 'Бразили', 'BR', 'Sao Paulo']},
    'Argentina': {'emoji': '🇦🇷', 'label': 'Аргентина', 'patterns': ['Argentina', 'Аргентин', 'AR', 'Buenos Aires']},
    'Mexico': {'emoji': '🇲🇽', 'label': 'Мексика', 'patterns': ['Mexico', 'Мексик', 'MX']},
    'Chile': {'emoji': '🇨🇱', 'label': 'Чили', 'patterns': ['Chile', 'Чили', 'CL', 'Santiago']},
    'Colombia': {'emoji': '🇨🇴', 'label': 'Колумбия', 'patterns': ['Colombia', 'Колумби', 'CO', 'Bogota']},
    'Peru': {'emoji': '🇵🇪', 'label': 'Перу', 'patterns': ['Peru', 'Перу', 'PE', 'Lima']},
    # Middle East
    'Turkey': {'emoji': '🇹🇷', 'label': 'Турция', 'patterns': ['Turkey', 'Турци', 'TR', 'Istanbul']},
    'Israel': {'emoji': '🇮🇱', 'label': 'Израиль', 'patterns': ['Israel', 'Израил', 'IL', 'Tel Aviv']},
    'UAE': {'emoji': '🇦🇪', 'label': 'ОАЭ', 'patterns': ['UAE', 'Emirates', 'ОАЭ', 'AE', 'Dubai', 'Abu Dhabi']},
    'SaudiArabia': {'emoji': '🇸🇦', 'label': 'Саудовская Аравия', 'patterns': ['Saudi Arabia', 'Saudi', 'Сауд', 'SA', 'Riyadh']},
    'Qatar': {'emoji': '🇶🇦', 'label': 'Катар', 'patterns': ['Qatar', 'Катар', 'QA', 'Doha']},
    'Egypt': {'emoji': '🇪🇬', 'label': 'Египет', 'patterns': ['Egypt', 'Египет', 'EG', 'Cairo']},
    # Asia-Pacific
    'Japan': {'emoji': '🇯🇵', 'label': 'Япония', 'patterns': ['Japan', 'Япони', 'JP', 'Tokyo']},
    'SouthKorea': {'emoji': '🇰🇷', 'label': 'Южная Корея', 'patterns': ['South Korea', 'Korea', 'Коре', 'KR', 'Seoul']},
    'China': {'emoji': '🇨🇳', 'label': 'Китай', 'patterns': ['China', 'Китай', 'CN', 'Beijing', 'Shanghai']},
    'HongKong': {'emoji': '🇭🇰', 'label': 'Гонконг', 'patterns': ['Hong Kong', 'HongKong', 'Гонконг', 'HK']},
    'Taiwan': {'emoji': '🇹🇼', 'label': 'Тайвань', 'patterns': ['Taiwan', 'Тайван', 'TW', 'Taipei']},
    'Singapore': {'emoji': '🇸🇬', 'label': 'Сингапур', 'patterns': ['Singapore', 'Сингапур', 'SG']},
    'India': {'emoji': '🇮🇳', 'label': 'Индия', 'patterns': ['India', 'Инди', 'IN', 'Mumbai', 'Delhi']},
    'Pakistan': {'emoji': '🇵🇰', 'label': 'Пакистан', 'patterns': ['Pakistan', 'Пакист', 'PK', 'Karachi']},
    'Bangladesh': {'emoji': '🇧🇩', 'label': 'Бангладеш', 'patterns': ['Bangladesh', 'Банглад', 'BD', 'Dhaka']},
    'Vietnam': {'emoji': '🇻🇳', 'label': 'Вьетнам', 'patterns': ['Vietnam', 'Вьетнам', 'VN', 'Hanoi']},
    'Thailand': {'emoji': '🇹🇭', 'label': 'Таиланд', 'patterns': ['Thailand', 'Таиланд', 'TH', 'Bangkok']},
    'Indonesia': {'emoji': '🇮🇩', 'label': 'Индонезия', 'patterns': ['Indonesia', 'Индонез', 'ID', 'Jakarta']},
    'Malaysia': {'emoji': '🇲🇾', 'label': 'Малайзия', 'patterns': ['Malaysia', 'Малайз', 'MY', 'Kuala Lumpur']},
    'Philippines': {'emoji': '🇵🇭', 'label': 'Филиппины', 'patterns': ['Philippines', 'Филиппин', 'PH', 'Manila']},
    'SriLanka': {'emoji': '🇱🇰', 'label': 'Шри-Ланка', 'patterns': ['Sri Lanka', 'SriLanka', 'Lanka', 'LK', 'Colombo']},
    'Australia': {'emoji': '🇦🇺', 'label': 'Австралия', 'patterns': ['Australia', 'Австрали', 'AU', 'Sydney']},
    'NewZealand': {'emoji': '🇳🇿', 'label': 'Новая Зеландия', 'patterns': ['New Zealand', 'NewZealand', 'Зеланд', 'NZ', 'Auckland']},
    # Africa
    'SouthAfrica': {'emoji': '🇿🇦', 'label': 'ЮАР', 'patterns': ['South Africa', 'ЮАР', 'ZA', 'Johannesburg']},
    'Nigeria': {'emoji': '🇳🇬', 'label': 'Нигерия', 'patterns': ['Nigeria', 'Нигери', 'NG', 'Lagos']},
    'Kenya': {'emoji': '🇰🇪', 'label': 'Кения', 'patterns': ['Kenya', 'Кени', 'KE', 'Nairobi']},
    'Morocco': {'emoji': '🇲🇦', 'label': 'Марокко', 'patterns': ['Morocco', 'Марокк', 'MA', 'Casablanca']},
    # Special
    'LTE': {'emoji': '🇪🇺', 'label': 'LTE', 'patterns': ['LTE', 'ЕВРОПА']},
}

_INFER_SKIP_LABELS = frozenset({
    'lte', 'auto', 'proxy', 'wifi', 'europe', 'европа', 'ес', 'global', 'backup', 'main', 'vip',
})


def _build_flag_to_country() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for key, info in COUNTRY_PATTERNS.items():
        if key == 'LTE':
            continue
        emoji = str(info.get('emoji') or '').strip()
        if not emoji:
            continue
        mapping[emoji] = key
        for part in emoji.split():
            if part:
                mapping[part] = key
    return mapping


FLAG_TO_COUNTRY = _build_flag_to_country()


LTE_TAG_PATTERNS = ['LTE']
NO_AUTO_TAG_PATTERNS = ['no auto', 'noauto', 'no-auto', 'без auto', 'безauto', 'без-auto']
_NO_AUTO_STRIP_RE = re.compile(
    r'[\s\[\]_(]*(?:no[\s_-]?auto|без[\s_-]?auto)[\s\[\]_.-]*',
    re.I,
)


def is_lte_tag(text: str, patterns: list[str] | None = None) -> bool:
    pats = patterns if patterns else LTE_TAG_PATTERNS
    if not pats:
        return False
    text_lower = str(text or '').lower()
    return any(p.lower() in text_lower for p in pats if str(p).strip())


def is_no_auto_tag(text: str) -> bool:
    text_lower = str(text or '').lower()
    return any(p in text_lower for p in NO_AUTO_TAG_PATTERNS)


def strip_no_auto_markers(text: str) -> str:
    cleaned = _NO_AUTO_STRIP_RE.sub(' ', str(text or ''))
    return re.sub(r'\s+', ' ', cleaned).strip(' -_|')


def _leading_flag_emoji(text: str) -> str | None:
    m = re.match(r'^\s*((?:[\U0001F1E6-\U0001F1FF]{2}\s*)+)', text)
    return m.group(1).strip() if m else None


def _label_from_remark(remark: str, *, strip_lte: bool = True) -> str | None:
    """Extract human-readable group title from host remark/tag."""
    text = str(remark or '').strip()
    if not text:
        return None
    text = _RENEWAL_DATE_IN_NAME_RE.sub('', text).strip()
    text = _FLAG_RE.sub('', text).strip()
    text = re.sub(r'\s*\([^)]*\)', '', text).strip()
    text = re.sub(r'\s*\[[^\]]*\]', '', text).strip()
    text = strip_no_auto_markers(text)
    if strip_lte:
        text = re.sub(r'\s+LTE\s*$', '', text, flags=re.I).strip()
        text = re.sub(r'\s+LTE\b.*$', '', text, flags=re.I).strip()
    text = _TRAILING_SUFFIX_RE.sub('', text).strip()
    if len(text) < 2 or re.match(r'^[\d.]+$', text):
        return None
    if len(text) <= 2 and text.isalpha() and text.upper() == text:
        return None
    return text


def _group_display_name(country_key: str, remark: str, info: dict) -> str:
    strip_lte = country_key != 'LTE'
    from_remark = _label_from_remark(remark, strip_lte=strip_lte)
    if from_remark:
        return from_remark
    return str(info.get('label') or country_key)


def _best_pattern_match(text: str, patterns_map: dict) -> str | None:
    text_lower = text.lower()
    best_match = None
    best_len = 0
    for key, info in patterns_map.items():
        patterns = info if isinstance(info, list) else info.get('patterns', [])
        for pattern in patterns:
            pat_lower = pattern.lower()
            if len(pat_lower) <= 2:
                matched = bool(re.search(rf'(?:^|[^a-z]){re.escape(pat_lower)}(?:$|[^a-z])', text_lower))
            else:
                matched = pat_lower in text_lower
            if matched and len(pat_lower) > best_len:
                best_match = key
                best_len = len(pat_lower)
    return best_match


def _primary_flag_emoji(text: str) -> str | None:
    leading = _leading_flag_emoji(text)
    if not leading:
        return None
    parts = leading.split()
    return parts[0] if parts else leading


def _detect_country_from_flag(remark: str) -> dict | None:
    """Match country by leading flag emoji (🇺🇸 🇸🇪 …) in host remark."""
    flag = _primary_flag_emoji(str(remark))
    if not flag:
        return None
    country_key = FLAG_TO_COUNTRY.get(flag)
    if not country_key:
        return None
    info = COUNTRY_PATTERNS[country_key]
    patterns = list(info['patterns'])
    for marker in (flag, str(info.get('emoji') or '').split()[0]):
        if marker and marker not in patterns:
            patterns.insert(0, marker)
    return {
        'country': country_key,
        'emoji': info['emoji'],
        'patterns': patterns,
    }


def _infer_country_from_remark(remark: str) -> dict | None:
    """Fallback: flag emoji + readable name when not in COUNTRY_PATTERNS."""
    flag = _leading_flag_emoji(str(remark))
    label = _label_from_remark(str(remark))
    if not flag or not label:
        return None
    label_lower = label.lower()
    if label_lower in _INFER_SKIP_LABELS:
        return None
    if any(label_lower.startswith(skip) for skip in _INFER_SKIP_LABELS):
        return None
    patterns = [label]
    if len(label) >= 5:
        patterns.append(label[: max(4, len(label) - 1)])
    emoji = flag.split()[0]
    return {'country': label, 'emoji': emoji, 'patterns': patterns}


def detect_country_from_remark(remark: str) -> dict | None:
    from_flag = _detect_country_from_flag(remark)
    if from_flag:
        return from_flag
    key = _best_pattern_match(remark, COUNTRY_PATTERNS)
    if key:
        info = COUNTRY_PATTERNS[key]
        return {'country': key, 'emoji': info['emoji'], 'patterns': info['patterns']}
    return _infer_country_from_remark(remark)


def is_russia_remark(text: str) -> bool:
    """True if host remark/tag is Russia (🇷🇺 flag, Russia/Россия/RU/Moscow…)."""
    raw = str(text or '').strip()
    if not raw:
        return False
    if _primary_flag_emoji(raw) == '🇷🇺':
        return True
    detected = detect_country_from_remark(raw)
    return detected is not None and detected.get('country') == 'Russia'


def build_groups_from_hosts(hosts: list) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    seen: set[str] = set()
    for host in hosts:
        if host.get('isDisabled') or host.get('is_disabled'):
            continue
        remark = host.get('remark') or host.get('tag') or host.get('address') or ''
        if is_no_auto_tag(str(remark)):
            continue
        detected = detect_country_from_remark(str(remark))
        if not detected or detected['country'] == 'LTE':
            continue
        if detected['country'] not in seen:
            seen.add(detected['country'])
            info = COUNTRY_PATTERNS.get(detected['country'], {'label': detected['country']})
            display = _group_display_name(detected['country'], str(remark), info)
            groups[f"{detected['emoji']} {display}"] = list(detected['patterns'])
    return groups
