"""Зоны доставки: разбор города и цена по поясу.

Доставка стоила одинаково по всей стране — и Тольятти, и Владивосток. На ближнем
заказе это лишние деньги с клиента, на дальнем — минус из кармана.

Ошибка в разборе города дороже, чем кажется: не узнали — заказ не оформится
и клиент уйдёт, узнали не ту зону — повезли через полстраны по цене соседнего
города.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from core.models import DeliveryZone
from core.services.delivery import add_city_to_zone, normalize_city

BOT = Path(__file__).resolve().parents[1] / "bot"
CATALOG = (BOT / "src" / "router_catalog.py").read_text(encoding="utf-8")


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


class TestAddCityToZone:
    """Кнопка «в зону» из списка неопознанных городов.

    Дописывает строку в список — сессия здесь не нужна, поэтому и не заводится.
    """

    def _add(self, zone: DeliveryZone, city: str) -> bool:
        return asyncio.run(add_city_to_zone(None, zone, city))

    def test_city_is_appended(self):
        zone = DeliveryZone(cities="Самара\nТольятти")
        assert self._add(zone, "Сызрань") is True
        assert zone.cities.splitlines() == ["Самара", "Тольятти", "Сызрань"]

    def test_first_city_of_an_empty_zone(self):
        zone = DeliveryZone(cities="")
        assert self._add(zone, "Самара") is True
        assert zone.cities == "Самара"

    @pytest.mark.parametrize("written", ["самара", "г. Самара", "САМАРА"])
    def test_the_same_city_is_not_added_twice(self, written):
        """Иначе один город копился бы в списке в разных написаниях."""
        zone = DeliveryZone(cities="Самара")
        assert self._add(zone, written) is False
        assert zone.cities == "Самара"

    def test_blank_is_not_a_city(self):
        zone = DeliveryZone(cities="Самара")
        assert self._add(zone, "  ") is False
        assert zone.cities == "Самара"


class TestBotStopsOnUnknownCity:
    """Незнакомый город останавливает оформление, а не идёт дальше без доставки.

    Раньше пустой список способов означал «оформляем без доставки» — и на этой
    же ветке оказался бы отказ по городу: клиент получил бы заказ с доставкой
    за ноль рублей на другой конец страны.
    """

    def test_refusal_is_checked_before_the_empty_options_branch(self):
        refusal = CATALOG.index('data.get("unknown_city")')
        fallback = CATALOG.index('options = data.get("options", [])')
        assert refusal < fallback, (
            "проверка незнакомого города должна идти до ветки «оформляем без "
            "доставки», иначе отказ превратится в бесплатную доставку"
        )

    def test_the_order_is_dropped(self):
        head = CATALOG[CATALOG.index("async def ask_carrier") :]
        body = head[: head.index("async def ask_promo")]
        assert "await state.clear()" in body, "оформление должно прерываться"

    def test_city_travels_to_the_price_request(self):
        """Без города вернутся общие цены — и зона никогда не сработает."""
        assert re.search(r"delivery_options\(\s*saved\.get\(\"city\"", CATALOG)

    def test_the_refusal_text_is_seeded(self):
        texts = (BOT / "src" / "shop_texts.py").read_text(encoding="utf-8")
        assert '"text_order_unknown_city"' in texts, (
            "текст отказа не заведён — клиент увидит имя ключа"
        )
