"""Правила группировки ссылок подписки для балансировщика хостов в админке.

Переехал из вырезанного `xuiweb/dedup/`: сам сервис не нужен роутерам,
а эти правила читает менеджер Remnawave и страница балансировщика.
"""

from __future__ import annotations

import re

_PLATFORM_TAG_RE = re.compile(r'\[(pc|mobile|router|tv)\]', re.IGNORECASE)
_TRAILING_INDEX_RE = re.compile(r'\s+\d{1,3}\s*$', re.UNICODE)
_PLATFORM_SUFFIX_RE = re.compile(
    r'(\s*\[(?:pc|mobile|router|tv)\])+\s*$',
    re.IGNORECASE,
)
_REMARK_MAX_LEN = 40


def normalize_display_name(name: str, *, keep_platform_tags: bool = True) -> str:
    text = str(name or '')
    if not keep_platform_tags:
        text = _PLATFORM_TAG_RE.sub('', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text.casefold()


def platform_tags_key(name: str) -> str:
    tags = sorted({m.group(1).lower() for m in _PLATFORM_TAG_RE.finditer(name or '')})
    return ','.join(tags)


def base_name_for_grouping(name: str) -> str:
    text = str(name or '').strip()
    platform_suffix = ''
    m = _PLATFORM_SUFFIX_RE.search(text)
    if m:
        platform_suffix = m.group(0)
        text = text[:m.start()].strip()
    text = _TRAILING_INDEX_RE.sub('', text).strip()
    return f'{text}{platform_suffix}'.strip()


def pool_key_from_remark(remark: str) -> str:
    canonical = base_name_for_grouping(remark)
    normalized = normalize_display_name(canonical, keep_platform_tags=True)
    if not normalized:
        return f'remark:{str(remark or "").strip()}'
    return f'name:{normalized}|tags:{platform_tags_key(remark)}'


def extract_trailing_index(remark: str) -> int | None:
    text = str(remark or '').strip()
    m = _PLATFORM_SUFFIX_RE.search(text)
    if m:
        text = text[:m.start()].strip()
    m_idx = re.search(r'(\d{1,3})\s*$', text)
    if not m_idx:
        return None
    try:
        return int(m_idx.group(1))
    except ValueError:
        return None


def format_member_remark(base_canonical: str, index: int) -> str:
    base = str(base_canonical or '').strip()
    platform_suffix = ''
    m = _PLATFORM_SUFFIX_RE.search(base)
    if m:
        platform_suffix = m.group(0)
        base = base[:m.start()].strip()
    out = f'{base} {index}{platform_suffix}'.strip()
    if len(out) > _REMARK_MAX_LEN:
        raise ValueError(f'Имя хоста длиннее {_REMARK_MAX_LEN} символов: {out!r}')
    return out


def next_pool_index(remarks: list[str]) -> int:
    max_idx = 0
    for remark in remarks:
        idx = extract_trailing_index(remark)
        if idx is not None and idx > max_idx:
            max_idx = idx
    return max_idx + 1


def is_pool_balanced(members: list[dict]) -> bool:
    if len(members) > 1:
        return True
    if len(members) == 1:
        return extract_trailing_index(members[0].get('remark') or '') is not None
    return False
