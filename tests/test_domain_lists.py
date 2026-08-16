"""Чистка списков переехала со скрипта и должна вести себя так же.

Разойтись с прежними правилами значит поменять то, что попадёт в туннель,
у всего парка сразу: строка, которую скрипт выбрасывал, а мы оставим, —
это домен, поехавший в обход, и наоборот.
"""

from __future__ import annotations

import pytest

from core.services.domain_lists import clean_domains, clean_ips, merge


class TestCleanDomains:
    def test_plain_lines_survive(self):
        assert clean_domains("example.com\nsub.example.org") == ["example.com", "sub.example.org"]

    def test_scheme_and_path_are_cut(self):
        assert clean_domains("https://example.com/some/path?a=1") == ["example.com"]

    def test_comments_and_blanks_dropped(self):
        raw = "# заголовок\nexample.com  # почему\n\n   \n"
        assert clean_domains(raw) == ["example.com"]

    def test_case_is_normalized(self):
        assert clean_domains("EXAMPLE.COM") == ["example.com"]

    def test_carriage_returns_do_not_leak(self):
        """Файлы у поставщика приходят с CRLF — скрипт снимал их первым делом."""
        assert clean_domains("example.com\r\nexample.org\r\n") == ["example.com", "example.org"]

    @pytest.mark.parametrize(
        "line",
        ["не домен", "example", "1.2.3.4", "два слова.com", "*.example.com", "-", "example.c"],
    )
    def test_garbage_dropped(self, line):
        assert clean_domains(line) == []


class TestCleanIps:
    def test_addresses_and_subnets(self):
        assert clean_ips("1.2.3.4\n10.0.0.0/8") == ["1.2.3.4", "10.0.0.0/8"]

    def test_comment_stripped(self):
        assert clean_ips("1.2.3.4 # telegram") == ["1.2.3.4"]

    @pytest.mark.parametrize("line", ["example.com", "::1", "1.2.3", "1.2.3.4 5.6.7.8", ""])
    def test_garbage_dropped(self, line):
        assert clean_ips(line) == []


class TestMerge:
    def test_duplicates_collapse_and_sorted(self):
        merged = merge([["b.com", "a.com"], ["a.com"]], "", "domain")
        assert merged == ["a.com", "b.com"]

    def test_manual_list_is_cleaned_too(self):
        """Оператор вставляет что придётся — причёсываем тем же способом."""
        merged = merge([["a.com"]], "https://Z.COM/path\n# коммент\n\nмусор тут", "domain")
        assert merged == ["a.com", "z.com"]

    def test_manual_ip_list_uses_ip_rules(self):
        merged = merge([["1.2.3.4"]], "10.0.0.0/8\nexample.com", "ip")
        assert merged == ["1.2.3.4", "10.0.0.0/8"]

    def test_empty_everything_is_not_an_error(self):
        assert merge([], "", "domain") == []
