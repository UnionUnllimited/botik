"""Базовый класс моделей, общие миксины и хелперы колонок."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import BigInteger, DateTime, Integer, MetaData, Numeric, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

MONEY = Numeric(12, 2)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        Decimal: MONEY,
        dt.datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[str]: JSONB,
        list[int]: JSONB,
    }

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


def enum_column(enum_cls: type[StrEnum], length: int = 32, **kwargs: Any) -> Any:
    """VARCHAR-колонка со значениями StrEnum (не нативный ENUM Postgres)."""
    return mapped_column(
        SAEnum(
            enum_cls,
            native_enum=False,
            length=length,
            values_callable=lambda e: [member.value for member in e],
            validate_strings=True,
        ),
        **kwargs,
    )


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class IntPkMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class BigIntPkMixin:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
