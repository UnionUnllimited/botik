"""Палитра приложения: читаемость проверяется числом, а не на глаз.

Палитра «Титан» холоднее и глуше прежней, и на тёмном графите это легко
не заметить: цвет выглядит нормально на большом мониторе в тёмной комнате
и пропадает на телефоне при солнце. Поэтому контраст здесь считается.

Главное, что стережёт этот файл, — разделение акцента на два. Чистый
Anodic Blue даёт на графите 3.2 и как текст не читается, но как заливка
под белым он же даёт 5.0. Один цвет на обе роли означал бы либо
нечитаемые подписи, либо блёклую кнопку — и первое из двух вернуть
особенно легко, потому что выглядит оно «почти нормально».
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "api" / "static" / "miniapp" / "app.css").read_text(
    encoding="utf-8"
)

AA_TEXT = 4.5
"""Порог WCAG AA для обычного текста."""

AA_THING = 3.0
"""Порог для того, что не текст: рамки, значки, полосы."""


def _srgb(channel: float) -> float:
    channel /= 255
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(colour: str) -> float:
    raw = colour.lstrip("#")
    red, green, blue = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _srgb(red) + 0.7152 * _srgb(green) + 0.0722 * _srgb(blue)


def contrast(one: str, two: str) -> float:
    first, second = _luminance(one), _luminance(two)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def _root() -> dict[str, str]:
    """Переменные из `:root` — те, что цветом."""
    block = CSS[CSS.index(":root {") : CSS.index("}", CSS.index(":root {"))]
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9A-Fa-f]{6})", block))


PALETTE = _root()

SURFACES = ("--bg", "--surface", "--surface-3")
"""Поверхности, на которых вообще лежит текст. `--surface-2` между ними,
и проверять его отдельно смысла нет: он не крайний ни с одной стороны."""


def test_the_palette_was_read():
    assert len(PALETTE) >= 10, "переменные цвета не разобрались — проверьте :root"


class TestItIsTitan:
    """Значения из фирменной палитры, а не «похожие»."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("--bg", "#0E1116"),  # Void
            ("--surface", "#1C2128"),  # Graphite
            ("--surface-3", "#2E353F"),  # Gunmetal
            ("--metal", "#6B7280"),  # Titanium
            ("--muted", "#A8B0BA"),  # Silver
            ("--text", "#E4E8ED"),  # Brushed
            ("--accent", "#2E6BD6"),  # Anodic Blue
            ("--accent-2", "#3FB6D9"),  # Cyan Sheen
            ("--accent-3", "#7B5CD6"),  # Violet Temper
        ],
    )
    def test_the_value_is_exact(self, name, expected):
        assert PALETTE.get(name) == expected

    def test_the_surfaces_climb_evenly(self):
        """Неравные ступени глаз читает как случайность, а не как глубину."""
        ladder = ["--bg", "--surface", "--surface-2", "--surface-3"]
        steps = [
            contrast(PALETTE[lower], PALETTE[upper])
            for lower, upper in itertools.pairwise(ladder)
        ]
        assert max(steps) - min(steps) < 0.06, f"ступени разъехались: {steps}"


class TestTextIsReadable:
    @pytest.mark.parametrize("surface", SURFACES)
    @pytest.mark.parametrize("ink", ["--text", "--muted"])
    def test_on_every_surface(self, ink, surface):
        got = contrast(PALETTE[ink], PALETTE[surface])
        assert got >= AA_TEXT, f"{ink} на {surface}: {got:.2f}"

    @pytest.mark.parametrize("surface", ["--bg", "--surface"])
    def test_the_third_step_where_words_live(self, surface):
        """`--subtle` — третья ступень текста: заголовки групп, подписи
        в характеристиках, зачёркнутая цена. Всё это лежит на фоне и на
        карточках, и там ему нужен полный порог."""
        got = contrast(PALETTE["--subtle"], PALETTE[surface])
        assert got >= AA_TEXT, f"--subtle на {surface}: {got:.2f}"

    def test_the_third_step_on_the_lightest_surface_is_not_text(self):
        """На `--surface-3` он до порога текста не дотягивает, и поднимать
        его туда нельзя: нужное значение сливается с `--muted`, и три
        ступени текста превращаются в две.

        Поэтому правило: на самой светлой поверхности `--subtle` бывает
        только значком, которому хватает 3.0. Сейчас это одно место —
        заглушка вместо фотографии товара. Появится второе с текстом —
        тест упадёт, и это правильно.
        """
        assert contrast(PALETTE["--subtle"], PALETTE["--surface-3"]) >= AA_THING

        places = [
            line.strip()
            for line in CSS.splitlines()
            if "var(--subtle)" in line and "surface-3" in line
        ]
        assert len(places) == 1, f"мест стало больше — проверьте каждое: {places}"

        head = CSS.index(".prod .shot-empty")
        assert places[0] in CSS[head : CSS.index("}", head)], "это уже не заглушка фотографии"

    @pytest.mark.parametrize("surface", SURFACES)
    def test_the_accent_for_text_is_readable(self, surface):
        """Им красятся ссылки, метки «выгоднее» и подпись выбранной вкладки."""
        got = contrast(PALETTE["--accent-on"], PALETTE[surface])
        assert got >= AA_THING, f"--accent-on на {surface}: {got:.2f}"

    def test_the_accent_for_text_holds_aa_where_words_live(self):
        """На графите и глубже — это фон карточек и списков, там слова."""
        for surface in ("--bg", "--surface"):
            got = contrast(PALETTE["--accent-on"], PALETTE[surface])
            assert got >= AA_TEXT, f"--accent-on на {surface}: {got:.2f}"

    def test_plain_titanium_is_not_text(self):
        """`--metal` оставлен как есть, из палитры, и потому тёмен для слов.
        Существует ради нетекстового: разделителей и значков."""
        assert contrast(PALETTE["--metal"], PALETTE["--surface"]) < AA_TEXT


class TestTheAccentKeepsItsTwoRoles:
    """Тот самый разъезд, ради которого этот файл и написан."""

    def test_the_filled_accent_would_fail_as_text(self):
        """Если это перестанет быть правдой, разделение можно убирать —
        но пока правда, тест ниже обязан держать его на месте."""
        assert contrast(PALETTE["--accent"], PALETTE["--surface"]) < AA_TEXT

    def test_it_is_never_used_as_ink(self):
        """`color: var(--accent)` — самая вероятная правка, которая молча
        сделает подписи нечитаемыми: выглядит она совершенно безобидно."""
        bad = re.findall(r"color:\s*var\(--accent\)", CSS)
        assert bad == [], "акцент-заливка попал в цвет текста — нужен --accent-on"

    def test_it_is_left_only_where_it_is_a_fill(self):
        """Сейчас это одно место: дорожка включённого переключателя."""
        places = re.findall(r"^.*var\(--accent\)(?!-).*$", CSS, re.M)
        assert len(places) == 1, f"мест стало больше — проверьте каждое: {places}"
        assert "background" in places[0]

    def test_white_holds_across_the_whole_button(self):
        """Градиент идёт от синего к фиолетовому: провалиться белый может
        в любой его точке, а не только на краях."""
        stops = re.findall(r"#[0-9A-Fa-f]{6}", PALETTE_GRADIENT)
        assert len(stops) >= 2, "в градиенте не нашлось остановок"
        for stop in stops:
            got = contrast("#FFFFFF", stop)
            assert got >= AA_TEXT, f"белый на {stop}: {got:.2f}"

    def test_the_button_is_written_in_white(self):
        head = CSS.index(".btn {")
        assert "color: var(--on-accent)" in CSS[head : CSS.index("}", head)]
        assert PALETTE.get("--on-accent") == "#FFFFFF"


PALETTE_GRADIENT = re.search(r"--grad:\s*([^;]+);", CSS).group(1)


class TestTheSystemButtonMatches:
    """Системную кнопку Telegram красит не CSS, а скрипт, и разъехаться
    с главной кнопкой экрана ей проще всего: она в другом файле."""

    JS = (
        Path(__file__).resolve().parents[1] / "api" / "static" / "miniapp" / "app.js"
    ).read_text(encoding="utf-8")

    def test_it_takes_the_filled_accent(self):
        assert "'#2E6BD6'" in self.JS

    def test_it_is_written_in_white(self):
        head = self.JS.index("mb.setParams(")
        assert "text_color: '#FFFFFF'" in self.JS[head : head + 200]

    def test_the_window_frame_is_the_deepest_surface(self):
        """Иначе рамка мессенджера светлее приложения, и оно выглядит
        вставленным в чужое окно."""
        assert "setHeaderColor('#0E1116')" in self.JS
        assert "setBackgroundColor('#0E1116')" in self.JS


class TestSemanticColoursStayVisible:
    @pytest.mark.parametrize("ink", ["--ok", "--warn", "--err"])
    def test_on_the_card(self, ink):
        got = contrast(PALETTE[ink], PALETTE["--surface"])
        assert got >= AA_TEXT, f"{ink}: {got:.2f}"

    def test_they_are_not_the_warm_defaults(self):
        """Рядом с холодным металлом тёплый янтарь и розовый выглядят
        грязью. Здесь они сдвинуты в холод — проверяем, что не вернулись."""
        assert PALETTE["--warn"] != "#FBBF24"
        assert PALETTE["--err"] != "#FB7185"


def test_keyboard_focus_is_visible():
    """Пальцем сюда не попадают, но приложение открывают и с клавиатурой."""
    assert ":focus-visible" in CSS
    assert "outline:" in CSS[CSS.index(":focus-visible") : CSS.index(":focus-visible") + 300]


def test_no_colour_is_hardcoded_past_the_palette():
    """Цвет мимо переменных — это цвет, который не поменяется вместе
    с палитрой и однажды останется синим на графите."""
    body = CSS[CSS.index("* { box-sizing") :]
    loose = re.findall(r"(?<!\w)#[0-9A-Fa-f]{6}(?!\w)", body)
    assert loose == [], f"цвета мимо палитры: {sorted(set(loose))}"
