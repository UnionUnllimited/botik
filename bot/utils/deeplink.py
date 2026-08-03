"""Разбор payload у /start.

Поддерживаемые формы (см. ТЗ, п. 4.1):
    ref_<tg_id>    — пришёл по реферальной ссылке
    dev_<mac_hash> — переход из панели роутера
    order_<id>     — возврат после оплаты
    utm_<source>   — метка источника трафика
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.config import settings


class PayloadKind(StrEnum):
    REFERRAL = "ref"
    DEVICE = "dev"
    ORDER = "order"
    UTM = "utm"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StartPayload:
    kind: PayloadKind
    value: str
    raw: str

    @property
    def as_int(self) -> int | None:
        return int(self.value) if self.value.isdigit() else None


def parse_start_payload(raw: str | None) -> StartPayload | None:
    if not raw:
        return None
    raw = raw.strip()[:128]
    prefix, _, value = raw.partition("_")
    kind = {
        "ref": PayloadKind.REFERRAL,
        "dev": PayloadKind.DEVICE,
        "order": PayloadKind.ORDER,
        "utm": PayloadKind.UTM,
    }.get(prefix.lower(), PayloadKind.UNKNOWN)
    if kind is PayloadKind.UNKNOWN:
        return StartPayload(kind=kind, value=raw, raw=raw)
    return StartPayload(kind=kind, value=value.strip(), raw=raw)


def build_deeplink(payload: str) -> str:
    """Ссылка вида https://t.me/<bot>?start=<payload>."""
    username = settings.app.bot_username.lstrip("@")
    return f"https://t.me/{username}?start={payload}"


def referral_link(tg_id: int) -> str:
    return build_deeplink(f"ref_{tg_id}")
