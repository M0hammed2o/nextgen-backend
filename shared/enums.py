"""
NextGen AI Platform — Shared Enums
Used by both backend (data plane) and admin_api (control plane).
"""

import enum


# ── User & Auth ──────────────────────────────────────────────────────────────

class BusinessUserRole(str, enum.Enum):
    OWNER = "OWNER"
    MANAGER = "MANAGER"
    STAFF = "STAFF"


class AdminRole(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"


# ── Business ─────────────────────────────────────────────────────────────────

class BillingStatus(str, enum.Enum):
    TRIAL = "TRIAL"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"


class PlanTier(str, enum.Enum):
    STARTER = "STARTER"
    GROWTH = "GROWTH"
    ENTERPRISE = "ENTERPRISE"


# ── Orders ───────────────────────────────────────────────────────────────────

class OrderStatus(str, enum.Enum):
    NEW = "NEW"
    ACCEPTED = "ACCEPTED"
    IN_PROGRESS = "IN_PROGRESS"
    READY = "READY"
    COLLECTED = "COLLECTED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


# Valid status transitions: {current_status: [allowed_next_statuses]}
ORDER_STATUS_TRANSITIONS: dict[OrderStatus, list[OrderStatus]] = {
    OrderStatus.NEW: [OrderStatus.ACCEPTED, OrderStatus.CANCELLED],
    OrderStatus.ACCEPTED: [OrderStatus.IN_PROGRESS, OrderStatus.CANCELLED],
    OrderStatus.IN_PROGRESS: [OrderStatus.READY, OrderStatus.CANCELLED],
    OrderStatus.READY: [OrderStatus.COLLECTED, OrderStatus.DELIVERED, OrderStatus.CANCELLED],
    OrderStatus.COLLECTED: [],
    OrderStatus.DELIVERED: [],
    OrderStatus.CANCELLED: [],
}


class OrderMode(str, enum.Enum):
    PICKUP = "PICKUP"
    DELIVERY = "DELIVERY"
    DINE_IN = "DINE_IN"


class OrderSource(str, enum.Enum):
    WHATSAPP = "WHATSAPP"
    MANUAL = "MANUAL"
    ADMIN = "ADMIN"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    CASH_ON_COLLECTION = "CASH_ON_COLLECTION"


# ── Messages ─────────────────────────────────────────────────────────────────

class MessageDirection(str, enum.Enum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class MessageIntent(str, enum.Enum):
    GREETING = "GREETING"
    MENU_REQUEST = "MENU_REQUEST"
    ORDER_START = "ORDER_START"
    ORDER_ADD = "ORDER_ADD"
    ORDER_REMOVE = "ORDER_REMOVE"
    ORDER_CONFIRM = "ORDER_CONFIRM"
    ORDER_CANCEL = "ORDER_CANCEL"
    ORDER_TRACK = "ORDER_TRACK"
    VIEW_CART = "VIEW_CART"
    SPECIALS_REQUEST = "SPECIALS_REQUEST"
    HOURS_REQUEST = "HOURS_REQUEST"
    LOCATION_REQUEST = "LOCATION_REQUEST"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    OPT_OUT = "OPT_OUT"
    RECOMMENDATION = "RECOMMENDATION"
    UNKNOWN = "UNKNOWN"


# ── Conversation ─────────────────────────────────────────────────────────────

class ConversationState(str, enum.Enum):
    IDLE = "IDLE"
    GREETING = "GREETING"
    BROWSING_MENU = "BROWSING_MENU"
    BUILDING_CART = "BUILDING_CART"
    CHOOSING_OPTIONS = "CHOOSING_OPTIONS"
    CONFIRMING_ORDER = "CONFIRMING_ORDER"
    COLLECTING_DETAILS = "COLLECTING_DETAILS"  # name/phone/address
    ORDER_PLACED = "ORDER_PLACED"
    HANDOFF = "HANDOFF"


# ── Assets ───────────────────────────────────────────────────────────────────

class AssetKind(str, enum.Enum):
    MENU_ITEM_IMAGE = "MENU_ITEM_IMAGE"
    BUSINESS_LOGO = "BUSINESS_LOGO"
    SPECIAL_IMAGE = "SPECIAL_IMAGE"


# ── Audit ────────────────────────────────────────────────────────────────────

class AuditScope(str, enum.Enum):
    PLATFORM = "PLATFORM"
    BUSINESS = "BUSINESS"
