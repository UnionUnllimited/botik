"""Списки доменов и подсетей, по которым роутер решает, что вести в туннель.

Раньше они собирались скриптом на сервере frps: два десятка адресов были
зашиты в массив, а свой список правился руками через веб-интерфейс GitHub.
Отключить одну категорию значило зайти на сервер и поправить файл.

Теперь источники и свой список — данные. Скачивает, чистит и склеивает
их наша сборка, результат отдаётся роутерам с нашего домена.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base, BigIntPkMixin


class ListKind:
    """Что лежит в источнике: домены или подсети IPv4.

    Не enum: значение уезжает в чужую админку по HTTP и обратно, а лишний
    тип в схеме ради двух вариантов усложняет и миграцию, и разбор ответа.
    """

    DOMAIN = "domain"
    IP = "ip"

    ALL = (DOMAIN, IP)


class DomainSource(BigIntPkMixin, Base):
    """Адрес, откуда берётся кусок списка.

    Выключенный источник остаётся в таблице, а не удаляется: категории
    включают и выключают сезонно, и вспомнить точный адрес `.lst` через месяц
    неоткуда. Удаление — отдельное действие для тех, что заведены по ошибке.
    """

    __tablename__ = "domain_sources"

    url: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    """Понятное имя для страницы: «Discord», «Подсети Telegram»."""

    kind: Mapped[str] = mapped_column(String(16), default=ListKind.DOMAIN, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    etag: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    """Метка версии от отдающей стороны. Уходит обратно в `If-None-Match`,
    и неизменившийся файл отвечает `304` без тела — так частый круг перестаёт
    быть долбёжом чужого GitHub."""

    last_ok_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    last_lines: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """Сколько строк дал источник в прошлую сборку.

    Нужно ровно для одного: увидеть, что источник отвечает 200, но отдаёт
    пустоту. Прежний скрипт писал `WARN` в вывод, который никто не читал,
    и список молча худел.
    """

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_domain_sources_kind_enabled", "kind", "is_enabled"),)


class ManualList(BigIntPkMixin, Base):
    """Свой список: то, что оператор дописывает руками.

    Хранится одной строкой на вид, а не записью на домен. Правят его целиком,
    вставкой из блокнота, и разбор на записи только мешал бы: удалить три
    строки из середины проще в текстовом поле, чем тремя нажатиями.
    """

    __tablename__ = "manual_lists"

    kind: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)

    updated_by: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    """Кто правил. Журнала действий у нас больше нет, а здесь важно: домен
    в этом списке открывает доступ, и «кто его добавил» — единственный вопрос,
    который зададут, когда он окажется лишним."""

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DomainBuild(BigIntPkMixin, Base):
    """Итог сборки: что и когда получилось.

    Одна запись на сборку, старые не чистятся — их немного (раз в час),
    а вопрос «когда список в последний раз менялся в размере» задают именно
    к истории.
    """

    __tablename__ = "domain_builds"

    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    domains: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ips: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_sources: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    error: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Круг прошёл, но ни один источник не изменился — пересобирать было нечего.
    Таких записей большинство, и по ним видно, что опрос идёт."""

    uploaded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Легла ли копия в объектное хранилище. Выкладка мягкая: список уже
    отдаётся с нашего домена, и падать из-за недоступного хранилища нельзя."""


class ManualListRevision(BigIntPkMixin, Base):
    """Прежние версии своего списка: кто, когда и что поменял.

    Список правится текстом целиком, и «убрал лишнее» здесь неотличимо
    от «стёр половину и не заметил». Журнала действий у нас больше нет,
    а домен в этом списке открывает доступ всему парку — вопрос «кто это
    добавил и когда» задают первым же делом.

    Хранится тело целиком, а не разница. Список — пара килобайт, версий
    за год наберётся сотня; считать разницу на лету дешевле, чем собирать
    состояние из цепочки правок и однажды собрать неверно.
    """

    __tablename__ = "manual_list_revisions"

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    author: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    added: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    removed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """Сколько строк прибавилось и убыло против прошлой версии. Считаем при
    сохранении: на странице это первое, на что смотрят, и пересчитывать его
    при каждой отрисовке ради экономии двух чисел незачем."""

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_manual_list_revisions_kind_id", "kind", "id"),)
