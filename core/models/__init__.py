"""Все модели импортируются здесь: Alembic и relationship-строки видят полный реестр."""

from core.models.base import Base
from core.models.catalog import Plan, Product
from core.models.content import Article, Broadcast, BroadcastTarget
from core.models.device import (
    Device,
    DeviceCommand,
    DeviceEvent,
    Heartbeat,
    SubscriptionAccessLog,
)
from core.models.node import Node, NodeAssignment, NodeGroup
from core.models.order import Delivery, Order, OrderItem
from core.models.payment import Payment
from core.models.promo import ActivationCode, ActivationCodeBatch, PromoCode, PromoUsage
from core.models.subscription import Subscription, SubscriptionEvent
from core.models.support import Ticket, TicketMessage
from core.models.system import AuditLog, Setting
from core.models.user import AdminUser, Referral, User

__all__ = [
    "ActivationCode",
    "ActivationCodeBatch",
    "AdminUser",
    "Article",
    "AuditLog",
    "Base",
    "Broadcast",
    "BroadcastTarget",
    "Delivery",
    "Device",
    "DeviceCommand",
    "DeviceEvent",
    "Heartbeat",
    "Node",
    "NodeAssignment",
    "NodeGroup",
    "Order",
    "OrderItem",
    "Payment",
    "Plan",
    "Product",
    "PromoCode",
    "PromoUsage",
    "Referral",
    "Setting",
    "Subscription",
    "SubscriptionAccessLog",
    "SubscriptionEvent",
    "Ticket",
    "TicketMessage",
    "User",
]
