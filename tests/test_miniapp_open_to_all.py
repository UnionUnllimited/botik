"""Приложение можно открыть всем — но только нарочно.

Пустой список допущенных значит «никому»: пока идёт обкатка, приложение не
должно открыться у случайного человека по найденной ссылке. Продавать с
таким списком, однако, нельзя вовсе — покупатель в него не попадёт и вместо
каталога увидит «приложение пока открыто не всем».

Раньше выхода из этой вилки не было: чтобы открыть всем, пришлось бы
вписывать в список каждого покупателя. Теперь есть отдельный выключатель, и
взводится он руками — пустая строка по-прежнему не означает «всем».
"""

from __future__ import annotations

from pydantic import SecretStr

from core.config import MiniappSettings

TOKEN = SecretStr("123:token")


def _settings(**kwargs) -> MiniappSettings:
    return MiniappSettings(bot_token=TOKEN, **kwargs)


class TestWhileTheListIsTheOnlyWayIn:
    def test_an_empty_list_still_means_nobody(self):
        shut = _settings()
        assert shut.is_allowed(42) is False
        # И сама возможность считается ненастроенной: ручки отвечают 404,
        # а не «403, но приложение вот оно».
        assert shut.is_configured is False

    def test_the_list_lets_in_only_those_on_it(self):
        listed = _settings(allowed_tg_ids=[7])
        assert listed.is_allowed(7) is True
        assert listed.is_allowed(42) is False
        assert listed.is_configured is True


class TestWhenTheSwitchIsThrown:
    def test_everyone_gets_in(self):
        opened = _settings(open_to_all=True)
        assert opened.is_allowed(42) is True
        assert opened.is_configured is True

    def test_the_list_stops_mattering(self):
        """Иначе после открытия пришлось бы ещё и чистить список."""
        opened = _settings(open_to_all=True, allowed_tg_ids=[7])
        assert opened.is_allowed(42) is True

    def test_without_a_token_it_is_still_off(self):
        """Подпись входа проверяется ключом из токена. Нет токена — нет и
        проверки, и открывать такое всем нельзя тем более."""
        assert MiniappSettings(open_to_all=True).is_configured is False


def test_the_switch_is_off_until_asked():
    """Умолчание не должно однажды тихо открыть приложение при обновлении."""
    assert MiniappSettings().open_to_all is False
