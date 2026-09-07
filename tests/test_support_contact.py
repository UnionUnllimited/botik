"""Контакт поддержки из одной строки — ссылка и имя для показа.

Оператор пишет контакт как удобно, а три потребителя — приложение, сайт,
тексты — раньше собирали из него каждый своё: приложение клеило `https://t.me/`
к полной ссылке и получало адрес в адресе. Теперь ссылку и имя даёт один код.
"""

from __future__ import annotations

import pytest

from core.services import landing


@pytest.mark.parametrize(
    ("raw", "url", "handle"),
    [
        ("@TitanVPSHelp_bot", "https://t.me/TitanVPSHelp_bot", "@TitanVPSHelp_bot"),
        ("TitanVPSHelp_bot", "https://t.me/TitanVPSHelp_bot", "@TitanVPSHelp_bot"),
        ("t.me/TitanVPSHelp_bot", "https://t.me/TitanVPSHelp_bot", "@TitanVPSHelp_bot"),
        ("https://t.me/TitanVPSHelp_bot", "https://t.me/TitanVPSHelp_bot", "@TitanVPSHelp_bot"),
        ("  https://t.me/TitanVPSHelp_bot/  ", "https://t.me/TitanVPSHelp_bot/", "@TitanVPSHelp_bot"),
        # Сайт поддержки вне Telegram — ссылка как есть, текстом тоже.
        ("https://help.example.com/chat", "https://help.example.com/chat", "https://help.example.com/chat"),
        ("", "", ""),
        ("   ", "", ""),
    ],
)
def test_one_value_gives_a_link_and_a_name(raw, url, handle):
    assert landing.support_url(raw) == url
    assert landing.support_handle(raw) == handle
