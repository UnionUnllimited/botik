"""Выпуски прошивки: что раздаётся роутерам и какой доле парка.

Роутеры на прошивке Titan обновляются сами: раз в сутки берут по постоянному
адресу один JSON, сравнивают номер версии со своим и, если он выше, качают
образ своей модели и проверяют его sha256. Ни ручек, ни авторизации, ни отчёта
обратно — вся логика на устройстве, от панели нужны файл и манифест.

Отсюда и схема: выпуск — это номер версии, доля раскатки и набор образов
по моделям. Больше в манифест ничего не уходит.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.models.base import Base, BigIntPkMixin


class FirmwareRelease(BigIntPkMixin, Base):
    """Один выпуск прошивки.

    Пока `published_at` пуст — это черновик: образы к нему складывают,
    а в манифест он не попадает. Иначе роутеры увидели бы номер выше своего
    раньше, чем оператор догрузил все четыре файла, и половина парка ушла бы
    качать образ, которого ещё нет.
    """

    __tablename__ = "firmware_releases"

    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    """Целое, строго растущее. Понижение роутер игнорирует, поэтому вернуть
    прежнюю прошивку выпуском с меньшим номером нельзя — только новым, большим."""

    notes: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    """Строка в лог роутера: «фикс WAN на WR3000E». Необязательна."""

    rollout: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """Доля парка 0..100. Роутер берёт свой номер 0..99 из хеша MAC и обновляется,
    если номер меньше этого значения. Разбиение устойчивое: расширение доли
    добавляет новые устройства, не трогая уже обновившиеся."""

    rollout_max: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """До какой доли выпуск доходил. Нужно именно оно, а не текущее значение:
    после экстренной остановки `rollout` равен нулю, а обновившиеся роутеры
    никуда не делись — по истории должно быть видно, скольких это коснулось."""

    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    """Кто выпустил. Журнала действий у нас больше нет, а прошивка едет
    на весь парк — вопрос «кто это выкатил» задают первым же делом."""

    images: Mapped[list[FirmwareImage]] = relationship(
        back_populates="release", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_firmware_releases_published_at", "published_at"),)


class FirmwareImage(BigIntPkMixin, Base):
    """Образ одной модели в выпуске.

    `sha256` и `size_bytes` считает сервер при загрузке файла и руками они
    не правятся: ошибка в одном знаке тихо отменяет обновление у всего парка —
    роутер молча бросает закачку и ждёт следующих суток.
    """

    __tablename__ = "firmware_images"

    release_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("firmware_releases.id", ondelete="CASCADE"), nullable=False
    )
    model_key: Mapped[str] = mapped_column(String(64), nullable=False)
    """Ключ модели ровно в том виде, в каком его называет прошивка,
    вместе с запятой: `cudy,wr3000e-v1`."""

    file_name: Mapped[str] = mapped_column(String(200), nullable=False)
    url_path: Mapped[str] = mapped_column(String(300), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    uploaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    release: Mapped[FirmwareRelease] = relationship(back_populates="images")

    __table_args__ = (
        UniqueConstraint("release_id", "model_key", name="uq_firmware_images_release_id_model_key"),
    )
