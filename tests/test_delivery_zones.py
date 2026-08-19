"""Зоны доставки: разбор города и цена по поясу.

Доставка стоила одинаково по всей стране — и Тольятти, и Владивосток. На ближнем
заказе это лишние деньги с клиента, на дальнем — минус из кармана.

Ошибка в разборе города дороже, чем кажется: не узнали — заказ не оформится
и клиент уйдёт, узнали не ту зону — повезли через полстраны по цене соседнего
города.
"""

from __future__ import annotations

import pytest

from core.services.delivery import normalize_city


class TestNormalizeCity:
    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("Самара", "самара"),
            ("САМАРА", "самара"),
            ("  самара  ", "самара"),
            ("г. Самара", "самара"),
            ("г.Самара", "самара"),
            ("г . Самара", "самара"),
            ("город Самара", "самара"),
            ("посёлок Красный Яр", "красный яр"),
            ("пос. Кинель", "кинель"),
        ],
    )
    def test_address_noise_is_dropped(self, written, expected):
        assert normalize_city(written) == expected

    def test_yo_and_ye_are_the_same_city(self):
        """Половина клавиатур не набирает «ё», а Королёв один."""
        assert normalize_city("Королёв") == normalize_city("Королев")

    @pytest.mark.parametrize(
        ("one", "two"),
        [
            ("Нижний Новгород", "нижний-новгород"),
            ("Ростов-на-Дону", "ростов на дону"),
            ("Санкт-Петербург", "санкт петербург"),
        ],
    )
    def test_separators_do_not_matter(self, one, two):
        assert normalize_city(one) == normalize_city(two)

    def test_city_starting_like_a_prefix_survives(self):
        """«Сочи» начинается на «с», но это не «с. Очи»."""
        assert normalize_city("Сочи") == "сочи"
        assert normalize_city("Городец") == "городец"

    def test_empty_stays_empty(self):
        assert normalize_city("") == ""
        assert normalize_city("   ") == ""


class TestSeededZones:
    """Зоны заводятся миграцией: без них после выката встало бы оформление."""

    def _migration(self) -> str:
        import pathlib

        return (
            pathlib.Path(__file__).resolve().parents[1]
            / "migrations"
            / "versions"
            / "0012_delivery_zones.py"
        ).read_text(encoding="utf-8")

    def test_home_zone_is_samara(self):
        """Склад в Самаре: ближняя зона считается от неё, а не от Москвы."""
        source = self._migration()
        home = source[source.index('"home"') : source.index('"volga"')]
        assert "Самара" in home
        assert "Тольятти" in home
        assert "Москва" not in home

    def test_moscow_is_not_the_nearest(self):
        source = self._migration()
        central = source[source.index('"central"') : source.index('"northwest_urals"')]
        assert "Москва" in central

    def test_every_offered_carrier_has_a_price(self):
        """Перевозчик без цены в зоне откатился бы к общей настройке молча."""
        source = self._migration()
        for method in ("cdek", "yandex", "post"):
            assert source.count(f'"{method}"') >= 6

    def test_far_east_costs_more_than_home(self):
        source = self._migration()
        home = source[source.index('"home"') : source.index('"volga"')]
        far = source[source.index('"far_east"') :]
        assert '("200", "350")' in home
        assert '("950", "1200")' in far
