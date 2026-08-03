"""Перечисления домена. В БД хранятся строковыми значениями (VARCHAR).

Нативные ENUM-типы Postgres сознательно не используются: добавление статуса
не должно требовать ALTER TYPE и блокировок на проде.
"""

from __future__ import annotations

from enum import StrEnum


class AdminRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    SUPPORT = "support"
    LOGIST = "logist"


class ActorType(StrEnum):
    ADMIN = "admin"
    SYSTEM = "system"
    BOT = "bot"
    CLIENT = "client"


class OrderStatus(StrEnum):
    NEW = "new"
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    PACKING = "packing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    DONE = "done"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


ACTIVE_ORDER_STATUSES = (
    OrderStatus.NEW,
    OrderStatus.AWAITING_PAYMENT,
    OrderStatus.PAID,
    OrderStatus.PACKING,
    OrderStatus.SHIPPED,
)


class OrderItemType(StrEnum):
    PRODUCT = "product"
    PLAN = "plan"
    DELIVERY = "delivery"


class PaymentProviderName(StrEnum):
    YOOKASSA = "yookassa"
    SBP = "sbp"
    CRYPTOBOT = "cryptobot"
    COD = "cod"
    """Оплата при получении — деньги забирает служба доставки."""
    MANUAL = "manual"
    """Ручное подтверждение админом (перевод на карту, наличные)."""


class PaymentStatus(StrEnum):
    PENDING = "pending"
    WAITING_FOR_CAPTURE = "waiting_for_capture"
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"
    FAILED = "failed"
    REFUNDED = "refunded"


class PaymentPurpose(StrEnum):
    ORDER = "order"
    SUBSCRIPTION = "subscription"


class DeliveryMethod(StrEnum):
    CDEK = "cdek"
    POST = "post"
    BOXBERRY = "boxberry"
    PICKUP = "pickup"


class DeliveryStatus(StrEnum):
    NEW = "new"
    READY = "ready"
    SHIPPED = "shipped"
    IN_TRANSIT = "in_transit"
    ARRIVED = "arrived"
    DELIVERED = "delivered"
    RETURNED = "returned"


class DeviceStatus(StrEnum):
    NEW = "new"
    """Заведено на складе, ещё не отгружено."""
    ASSIGNED = "assigned"
    """Привязано к заказу при отгрузке, но клиент не активировал."""
    ACTIVE = "active"
    REVOKED = "revoked"
    """Отвязано клиентом или админом."""
    BLOCKED = "blocked"
    """Заблокировано за нарушение (фрод)."""


class DeviceServiceStatus(StrEnum):
    UNKNOWN = "unknown"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class CommandType(StrEnum):
    UPDATE_SUBSCRIPTION = "update_subscription"
    RESTART_SERVICE = "restart_service"
    REBOOT = "reboot"
    UPDATE_PANEL = "update_panel"
    SET_CONFIG = "set_config"
    REVOKE = "revoke"


class CommandStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    ACKED = "acked"
    FAILED = "failed"
    EXPIRED = "expired"


class SubscriptionStatus(StrEnum):
    PENDING = "pending"
    """Оплачена, но роутер ещё не активирован — отсчёт не начат."""
    ACTIVE = "active"
    GRACE = "grace"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class SubscriptionEventType(StrEnum):
    CREATED = "created"
    ACTIVATED = "activated"
    EXTENDED = "extended"
    RENEWED = "renewed"
    GRACE_STARTED = "grace_started"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    MANUAL_ADJUST = "manual_adjust"
    BONUS = "bonus"
    PLAN_CHANGED = "plan_changed"
    DEVICE_CHANGED = "device_changed"


class NodeProtocol(StrEnum):
    VLESS_REALITY = "vless_reality"
    VLESS_WS_TLS = "vless_ws_tls"


class NodeStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    MAINTENANCE = "maintenance"


class ActivationCodeStatus(StrEnum):
    NEW = "new"
    ISSUED = "issued"
    """Напечатан и отправлен с партией коробок."""
    USED = "used"
    EXPIRED = "expired"
    REVOKED = "revoked"


class PromoDiscountType(StrEnum):
    PERCENT = "percent"
    FIXED = "fixed"


class ReferralStatus(StrEnum):
    PENDING = "pending"
    REWARDED = "rewarded"
    REJECTED = "rejected"


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_CLIENT = "waiting_client"
    CLOSED = "closed"


class MessageDirection(StrEnum):
    IN = "in"
    OUT = "out"


class MediaType(StrEnum):
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"
    VOICE = "voice"
    ANIMATION = "animation"


class BroadcastStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    CANCELLED = "cancelled"


class BroadcastTargetStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    BLOCKED = "blocked"
    """Пользователь заблокировал бота."""


class VatCode(StrEnum):
    """Коды ставки НДС по 54-ФЗ (значения ЮKassa)."""

    NONE = "1"
    ZERO = "2"
    VAT_10 = "3"
    VAT_20 = "4"
    VAT_10_110 = "5"
    VAT_20_120 = "6"
