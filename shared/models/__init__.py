"""
All SQLAlchemy models — import here so Alembic autogenerate discovers them.
"""

from shared.models.base import Base

from shared.models.admin import AdminUser
from shared.models.ai_emails import AiEmailCaseStudy, AiEmailImportBatch, AiEmailLead
from shared.models.analytics import DailyUsage
from shared.models.asset import Asset
from shared.models.audit import AuditEvent, MessageOutbox
from shared.models.billing_invoice import BillingInvoice
from shared.models.business import Business
from shared.models.commercial_invoice import CommercialInvoice, CommercialInvoiceLine
from shared.models.customer import ConversationSession, Customer
from shared.models.digital_receipt import DigitalReceipt
from shared.models.external_orders import ExternalImportBatch, ExternalProductMapping
from shared.models.inventory import (
    GoodsReceipt,
    GoodsReceiptLine,
    Ingredient,
    StockMovement,
    Supplier,
)
from shared.models.loyalty import LoyaltyProgram, LoyaltyReward, LoyaltyTransaction
from shared.models.menu import MenuAddOn, MenuCategory, MenuItem
from shared.models.message import Message
from shared.models.order import Order, OrderEvent, OrderItem
from shared.models.push_subscription import PushSubscription
from shared.models.recipe import Recipe, RecipeItem
from shared.models.specials import Special
from shared.models.stocktake import StocktakeLine, StocktakeSession
from shared.models.till import TillSession
from shared.models.user import AdminRefreshToken, BusinessUser, RefreshToken
from shared.models.wastage import WastageEntry
from shared.models.whatsapp_connection import WhatsAppConnection

__all__ = [
    "Base",
    "AdminUser",
    "AdminRefreshToken",
    "AiEmailCaseStudy",
    "AiEmailImportBatch",
    "AiEmailLead",
    "Asset",
    "AuditEvent",
    "BillingInvoice",
    "Business",
    "BusinessUser",
    "CommercialInvoice",
    "CommercialInvoiceLine",
    "ConversationSession",
    "Customer",
    "DailyUsage",
    "DigitalReceipt",
    "ExternalImportBatch",
    "ExternalProductMapping",
    "GoodsReceipt",
    "GoodsReceiptLine",
    "Ingredient",
    "LoyaltyProgram",
    "LoyaltyReward",
    "LoyaltyTransaction",
    "MenuAddOn",
    "MenuCategory",
    "MenuItem",
    "Message",
    "MessageOutbox",
    "Order",
    "OrderEvent",
    "OrderItem",
    "PushSubscription",
    "Recipe",
    "RecipeItem",
    "RefreshToken",
    "Special",
    "StockMovement",
    "StocktakeLine",
    "StocktakeSession",
    "Supplier",
    "TillSession",
    "WastageEntry",
    "WhatsAppConnection",
]
