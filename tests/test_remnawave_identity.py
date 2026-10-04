"""Чем панель называет учётку — и чем ей отвечать.

До версии 2.9 панель узнаёт учётку по `uuid`, дальше — по числовому `id`.
Какой ключ она считает основным, видно только из её же ответа, и мы отвечаем
тем же ключом.

А вот тип значения мы теряли. Идентификатор хранится строкой — так удобнее
в журнале и в сравнениях, — и строкой же уходил обратно. Для `uuid` это верно,
для `id` нет: панель проверяет тип и отвечает

    Validation failed: expected number, received string, path ["id"]

Ловится это в худшем месте — на продлении: учётка заведена, деньги приняты,
а срок не двигается. Клиент заплатил и остался без доступа.
"""

from __future__ import annotations

from core.services.remnawave import RemnaUser


class TestWhichKeyThePanelAnswersTo:
    def test_old_panel_is_known_by_uuid(self):
        account = RemnaUser.parse({"uuid": "3f2a-...", "username": "tg1_aa"})
        assert account.uid_key == "uuid"
        assert account.uid == "3f2a-..."

    def test_new_panel_is_known_by_id(self):
        account = RemnaUser.parse({"id": 42, "username": "tg1_aa"})
        assert account.uid_key == "id"

    def test_uuid_wins_while_both_are_given(self):
        """Пока панель отдаёт оба ключа, отвечать надо тем, который она
        считает основным. `uuid` исчезнет — останется `id`."""
        account = RemnaUser.parse({"uuid": "3f2a", "id": 42})
        assert account.uid_key == "uuid"


class TestTheValueKeepsItsType:
    def test_a_numeric_id_goes_back_as_a_number(self):
        """Иначе панель отвечает «expected number, received string»
        и срок не двигается."""
        account = RemnaUser.parse({"id": 42, "username": "tg1_aa"})
        assert account.uid_payload == 42
        assert isinstance(account.uid_payload, int)

    def test_a_uuid_goes_back_as_a_string(self):
        account = RemnaUser.parse({"uuid": "3f2a-9b", "username": "tg1_aa"})
        assert account.uid_payload == "3f2a-9b"
        assert isinstance(account.uid_payload, str)

    def test_a_non_numeric_id_is_left_alone(self):
        """Выдумывать за панель число хуже, чем вернуть то, что она прислала."""
        account = RemnaUser.parse({"id": "abc-1", "username": "tg1_aa"})
        assert account.uid_payload == "abc-1"

    def test_the_stored_value_stays_a_string(self):
        """Строкой он удобнее в журнале и в сравнениях — меняется только то,
        что уходит в панель."""
        account = RemnaUser.parse({"id": 42})
        assert account.uid == "42"


def test_the_request_uses_the_typed_value():
    """Самое вероятное место ошибки: в запрос подставили `uid` вместо
    `uid_payload`, и оно выглядит совершенно нормально."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "core" / "services" / "remnawave.py").read_text(
        encoding="utf-8"
    )
    head = source.index("async def update_expiry")
    body = source[head : source.index("async def probe", head)]
    assert "account.uid_key: account.uid_payload" in body
    assert "account.uid_key: account.uid," not in body
