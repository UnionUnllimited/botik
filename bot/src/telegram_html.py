"""Проверка HTML-разметки для Telegram (parse_mode=HTML)."""
from __future__ import annotations

import re

TELEGRAM_HTML_TAGS = frozenset({
    'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
    'code', 'pre', 'a', 'blockquote', 'span', 'tg-spoiler', 'br',
})

VOID_TAGS = frozenset({'br'})

_TAG_RE = re.compile(
    r'<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9-]*)\s*([^<>]*?)\s*(/?)?\s*>',
    re.IGNORECASE,
)

_TEMPLATE_VAR_RE = re.compile(r'\{[^{}]+\}')

_FORMAT_PLACEHOLDER_RE = re.compile(r'\{[a-zA-Z_][\w]*(?::[^}]*)?\}')

_HREF_RE = re.compile(r'href\s*=\s*(["\']).*?\1', re.IGNORECASE)


def is_html_text_setting_key(key: str) -> bool:
    """Ключи настроек, где value отправляется в Telegram как HTML."""
    if key.startswith('text_'):
        return True
    return key.startswith('bot_protection_') and key.endswith('_text')


def validate_telegram_html(text: str) -> tuple[bool, list[str]]:
    """
    Проверяет баланс тегов и базовый синтаксис Telegram HTML.
    Возвращает (ok, список сообщений об ошибках).
    """
    errors: list[str] = []
    if not text or not text.strip():
        return True, errors

    errors.extend(_validate_format_braces(text))

    masked = _TEMPLATE_VAR_RE.sub('\uFFFC', text)
    stack: list[str] = []
    last_pos = 0

    for match in _TAG_RE.finditer(masked):
        between = masked[last_pos:match.start()]
        if '<' in between:
            errors.append('Некорректный символ «<» вне HTML-тега')
        last_pos = match.end()

        is_close = bool(match.group(1))
        tag = match.group(2).lower()
        attrs = match.group(3) or ''
        self_close = bool(match.group(4)) or tag in VOID_TAGS

        if is_close:
            if not stack:
                errors.append(f'Лишний закрывающий тег </{tag}>')
            elif stack[-1] != tag:
                errors.append(
                    f'Несовпадение тегов: открыт <{stack[-1]}>, закрыт </{tag}>',
                )
            else:
                stack.pop()
            continue

        if self_close:
            if tag not in TELEGRAM_HTML_TAGS:
                errors.append(f'Неподдерживаемый тег <{tag}> (Telegram HTML)')
            if tag == 'a':
                errors.append('Тег <a> должен быть парным: <a href="...">...</a>')
            continue

        if tag not in TELEGRAM_HTML_TAGS:
            errors.append(f'Неподдерживаемый тег <{tag}> (Telegram HTML)')
        if tag == 'a' and not _HREF_RE.search(attrs):
            errors.append('Тег <a> без корректного href="..."')
        stack.append(tag)

    if '<' in masked[last_pos:]:
        errors.append('Некорректный символ «<» вне HTML-тега')

    if stack:
        errors.append(f'Не закрыт тег <{stack[-1]}>')

    return _dedupe(errors) == [], _dedupe(errors)


def _validate_format_braces(text: str) -> list[str]:
    """Проверка фигурных скобок для шаблонов {variable}."""
    stripped = _FORMAT_PLACEHOLDER_RE.sub('', text)
    stripped = stripped.replace('{{', '').replace('}}', '')
    if '{' in stripped or '}' in stripped:
        return [
            'Некорректные фигурные скобки: одиночные { или } вне переменных. '
            'Для символа используйте {{ или }}',
        ]
    return []


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
