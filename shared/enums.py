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
    PENDING_DELIVERY_FEE = "PENDING_DELIVERY_FEE"  # delivery order waiting for staff to set fee
    FEE_SENT = "FEE_SENT"                          # staff set fee, waiting for customer approval
    NEW = "NEW"
    ACCEPTED = "ACCEPTED"
    IN_PROGRESS = "IN_PROGRESS"                    # "Preparing" in the UI
    READY = "READY"
    COLLECTED = "COLLECTED"                        # takeaway/pickup terminal state
    DELIVERED = "DELIVERED"                        # delivery terminal state
    COMPLETED = "COMPLETED"                         # dine-in terminal state, and grab-and-go POS sales
    CANCELLED = "CANCELLED"


# Valid status transitions: {current_status: [allowed_next_statuses]}
ORDER_STATUS_TRANSITIONS: dict[OrderStatus, list[OrderStatus]] = {
    OrderStatus.PENDING_DELIVERY_FEE: [OrderStatus.FEE_SENT, OrderStatus.CANCELLED],
    OrderStatus.FEE_SENT: [OrderStatus.NEW, OrderStatus.CANCELLED],
    OrderStatus.NEW: [OrderStatus.ACCEPTED, OrderStatus.CANCELLED],
    OrderStatus.ACCEPTED: [OrderStatus.IN_PROGRESS, OrderStatus.CANCELLED],
    OrderStatus.IN_PROGRESS: [OrderStatus.READY, OrderStatus.CANCELLED],
    OrderStatus.READY: [
        OrderStatus.COLLECTED, OrderStatus.DELIVERED, OrderStatus.COMPLETED, OrderStatus.CANCELLED,
    ],
    OrderStatus.COLLECTED: [],
    OrderStatus.DELIVERED: [],
    OrderStatus.COMPLETED: [],
    OrderStatus.CANCELLED: [],
}


class OrderMode(str, enum.Enum):
    PICKUP = "PICKUP"
    DELIVERY = "DELIVERY"
    DINE_IN = "DINE_IN"


class OrderSource(str, enum.Enum):
    """
    Where an order came from.

    POS/WHATSAPP/MANUAL/ADMIN are all *in-store* channels: the restaurant
    took the order and the money itself. The external values below are
    marketplace channels where the platform already collected payment from
    the customer, and the order reaches us through a daily report rather
    than through a till.

    Deliberately ONE enum rather than a parallel "channel" concept: an
    external sale is a real Order, so it resolves the same recipes, posts to
    the same stock ledger, and appears in the same reporting. Splitting it
    would mean two inventory systems that could disagree.
    """
    WHATSAPP = "WHATSAPP"
    MANUAL = "MANUAL"
    ADMIN = "ADMIN"
    POS = "POS"
    # Marketplace channels - paid on the platform, imported from a report.
    UBER_EATS = "UBER_EATS"
    MR_D = "MR_D"
    OTHER_EXTERNAL = "OTHER_EXTERNAL"


#: Sources whose money the restaurant never handles, so they must never
#: reach till cash-up. Used by backend/app/pos/till.py.
EXTERNAL_ORDER_SOURCES: frozenset[str] = frozenset({
    OrderSource.UBER_EATS.value,
    OrderSource.MR_D.value,
    OrderSource.OTHER_EXTERNAL.value,
})

#: Everything the restaurant sold and collected for itself.
IN_STORE_ORDER_SOURCES: frozenset[str] = frozenset({
    OrderSource.POS.value,
    OrderSource.WHATSAPP.value,
    OrderSource.MANUAL.value,
    OrderSource.ADMIN.value,
})


class ExternalPlatform(str, enum.Enum):
    """
    A delivery marketplace we can import a daily report from.

    Kept separate from OrderSource so an import adapter, a saved product
    mapping and a batch can all name a platform without implying that every
    OrderSource value is importable.
    """
    UBER_EATS = "UBER_EATS"
    MR_D = "MR_D"
    GENERIC = "GENERIC"


#: Which OrderSource an imported platform's orders are recorded as.
PLATFORM_TO_ORDER_SOURCE: dict[str, str] = {
    ExternalPlatform.UBER_EATS.value: OrderSource.UBER_EATS.value,
    ExternalPlatform.MR_D.value: OrderSource.MR_D.value,
    ExternalPlatform.GENERIC.value: OrderSource.OTHER_EXTERNAL.value,
}


class ExternalImportStatus(str, enum.Enum):
    """
    PREVIEW    - parsed and validated, nothing written
    COMPLETED  - orders created and stock posted
    BLOCKED    - unresolved items or a closed-period conflict stopped it
    FAILED     - the file could not be processed
    """
    PREVIEW = "PREVIEW"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class ExternalMappingKind(str, enum.Enum):
    """
    ITEM     - an external product name -> a NextGen menu item (+ size option)
    MODIFIER - an external modifier -> an add-on, an option, or a removal
    """
    ITEM = "ITEM"
    MODIFIER = "MODIFIER"


class PaymentStatus(str, enum.Enum):
    UNPAID = "UNPAID"            # online payment required, instructions not yet sent
    PENDING = "PENDING"          # payment instructions sent / card at collection
    PAID = "PAID"                # payment confirmed
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"  # amount_cents < total_cents refunded
    REFUNDED = "REFUNDED"        # payment refunded in full
    FAILED = "FAILED"            # payment attempt failed
    VOIDED = "VOIDED"            # a mis-rung POS sale reversed with no external refund transaction
    CASH_ON_COLLECTION = "CASH_ON_COLLECTION"  # traditional cash flow


class POSPaymentMethod(str, enum.Enum):
    """
    Payment methods a cashier can select at the POS till itself — distinct
    from Order.payment_method's broader freeform value (which also covers
    online/WhatsApp-flow methods like PAYSHAP/PAYFAST payment links). CARD
    here means "any physical card terminal" (Yoco/iKhokha/etc.) confirmed
    manually by the cashier — see Business.pos_card_enabled and friends for
    which specific terminal brands a business has switched on.
    """
    CASH = "CASH"
    CARD = "CARD"
    OTHER = "OTHER"


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
    CHOOSING_ORDER_MODE = "CHOOSING_ORDER_MODE"      # waiting for pickup/delivery answer
    CONFIRMING_ORDER = "CONFIRMING_ORDER"
    COLLECTING_DETAILS = "COLLECTING_DETAILS"        # name/phone/address
    WAITING_DELIVERY_FEE_APPROVAL = "WAITING_DELIVERY_FEE_APPROVAL"  # delivery: waiting for staff to set fee
    COLLECTING_PAYMENT = "COLLECTING_PAYMENT"                        # waiting for cash/card choice
    ORDER_PLACED = "ORDER_PLACED"
    HANDOFF = "HANDOFF"


# ── Assets ───────────────────────────────────────────────────────────────────

class AssetKind(str, enum.Enum):
    MENU_ITEM_IMAGE = "MENU_ITEM_IMAGE"
    BUSINESS_LOGO = "BUSINESS_LOGO"
    SPECIAL_IMAGE = "SPECIAL_IMAGE"
    # Onboarding reference documents a restaurant uploads about ITSELF. These
    # are inputs we read to reproduce something the client already has — an
    # existing menu, an existing till slip. Neither is ever rendered to a
    # customer; the live menu and the printed receipt are always the
    # structured records.
    MENU_REFERENCE = "MENU_REFERENCE"
    RECEIPT_REFERENCE = "RECEIPT_REFERENCE"


# ── Audit ────────────────────────────────────────────────────────────────────

class AuditScope(str, enum.Enum):
    PLATFORM = "PLATFORM"
    BUSINESS = "BUSINESS"


# ── Inventory: units & measurement ───────────────────────────────────────────
#
# Canonical storage rule (see backend/app/inventory/units.py):
#   every stock quantity in the database is an INTEGER count of
#   thousandths ("milli") of the ingredient's canonical unit. Grams,
#   millilitres and whole units are the only canonical units; kilograms,
#   litres, cases and packs are *purchase* units that convert at the edge.
#   Integers are used for the same reason money is stored in cents —
#   binary floating point silently corrupts stock arithmetic.

class CanonicalUnit(str, enum.Enum):
    """The three units a stock quantity may actually be stored in."""
    GRAM = "GRAM"              # mass      — cheese, chicken, flour
    MILLILITRE = "MILLILITRE"  # volume    — oil, sauce (when poured)
    UNIT = "UNIT"              # countable — dough balls, boxes, cans


class MeasureFamily(str, enum.Enum):
    """Groups units that may legally convert between each other."""
    MASS = "MASS"
    VOLUME = "VOLUME"
    COUNT = "COUNT"


class CountMethod(str, enum.Enum):
    """
    How staff are instructed to physically count this ingredient during a
    stocktake. Shown on the count sheet — a half-full cheese bag counted
    as "0.5" when the method is WEIGHT is the single most common source of
    an unexplainable variance.
    """
    UNIT_COUNT = "UNIT_COUNT"  # count whole items
    WEIGHT = "WEIGHT"          # put it on a scale
    VOLUME = "VOLUME"          # measure the volume


# ── Inventory: stock ledger ──────────────────────────────────────────────────

class StockMovementType(str, enum.Enum):
    """
    Every reason stock may move. The ledger is append-only: a correction is
    always a new compensating movement, never an edit of an existing row.
    """
    OPENING_BALANCE = "OPENING_BALANCE"
    GOODS_RECEIVED = "GOODS_RECEIVED"
    SALE_CONSUMPTION = "SALE_CONSUMPTION"
    SALE_REVERSAL = "SALE_REVERSAL"
    REMAKE_CONSUMPTION = "REMAKE_CONSUMPTION"
    WASTAGE = "WASTAGE"
    STAFF_MEAL = "STAFF_MEAL"
    COMPLIMENTARY = "COMPLIMENTARY"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"
    STOCKTAKE_ADJUSTMENT = "STOCKTAKE_ADJUSTMENT"
    SUPPLIER_RETURN = "SUPPLIER_RETURN"


class StockSourceType(str, enum.Enum):
    """What kind of record a movement was caused by (with source_id)."""
    ORDER = "ORDER"
    GOODS_RECEIPT = "GOODS_RECEIPT"
    WASTAGE_ENTRY = "WASTAGE_ENTRY"
    STOCKTAKE = "STOCKTAKE"
    MANUAL = "MANUAL"


# ── Inventory: wastage ───────────────────────────────────────────────────────

class WastageReason(str, enum.Enum):
    """
    Reason codes are what make a variance explainable. OTHER always
    requires a note — an unexplained "other" is indistinguishable from
    shrinkage at report time.
    """
    SPOILED = "SPOILED"
    EXPIRED = "EXPIRED"
    DROPPED = "DROPPED"
    BURNT = "BURNT"
    INCORRECT_PREPARATION = "INCORRECT_PREPARATION"
    REMAKE = "REMAKE"
    DAMAGED_PACKAGING = "DAMAGED_PACKAGING"
    STAFF_MEAL = "STAFF_MEAL"
    COMPLIMENTARY = "COMPLIMENTARY"
    TRAINING = "TRAINING"
    OTHER = "OTHER"


# ── Inventory: stocktake ─────────────────────────────────────────────────────

class StocktakeStatus(str, enum.Enum):
    """
    DRAFT   — counting in progress, lines may be edited
    COMPLETED — counts submitted, variance computed, awaiting sign-off
    APPROVED  — locked forever; adjustments posted to the ledger
    CANCELLED — abandoned before completion
    """
    DRAFT = "DRAFT"
    COMPLETED = "COMPLETED"
    APPROVED = "APPROVED"
    CANCELLED = "CANCELLED"


# ── Receipts / printing ──────────────────────────────────────────────────────

class ReceiptPaperWidth(int, enum.Enum):
    """Standard thermal roll widths. 58 mm ≈ 32 chars, 80 mm ≈ 48 chars."""
    MM_58 = 58
    MM_80 = 80


class PrinterProfile(str, enum.Enum):
    """Which physical printer a document is destined for."""
    CUSTOMER_RECEIPT = "CUSTOMER_RECEIPT"
    KITCHEN = "KITCHEN"
