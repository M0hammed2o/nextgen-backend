"""
Payment message builder — generates WhatsApp payment instruction text.

build_payment_message(order, business) is the single entry point.
The order flow calls this; it never builds payment text inline.
"""

from __future__ import annotations

from shared.utils.money import format_currency


def _payment_reference(order, business) -> str:
    """Return the EFT/payment reference to show to the customer."""
    prefix = getattr(business, "eft_reference_prefix", None)
    if prefix:
        # Convert BO-000123 → BAR-000123
        seq_part = order.order_number.split("-")[-1]
        return f"{prefix}-{seq_part}"
    return order.order_number


def build_payment_message(order, business) -> str | None:
    """
    Build the WhatsApp payment instruction message.

    Returns None if the business does not require online payment.
    The text varies based on which payment methods are enabled.
    """
    if not getattr(business, "online_payment_required", False):
        return None

    methods = getattr(business, "payment_methods_enabled", None) or []
    currency = getattr(business, "currency", "ZAR")
    total = format_currency(order.total_cents, currency)
    reference = _payment_reference(order, business)

    lines = [
        f"Your order *{order.order_number}* has been accepted ✅\n",
        "Please complete payment before we start preparing your order.\n",
        f"💰 *Amount due: {total}*",
        f"📋 *Reference: {reference}*\n",
    ]

    if "DIRECT_EFT" in methods:
        lines.append("*Bank Transfer (EFT) Details:*")
        bank_name = getattr(business, "eft_bank_name", None) or "—"
        account_name = getattr(business, "eft_account_name", None) or "—"
        account_number = getattr(business, "eft_account_number", None) or "—"
        branch_code = getattr(business, "eft_branch_code", None) or "—"
        lines += [
            f"  Bank: {bank_name}",
            f"  Account Name: {account_name}",
            f"  Account Number: {account_number}",
            f"  Branch Code: {branch_code}",
            f"  Reference: *{reference}*",
            "",
        ]

    if "PAYMENT_LINK" in methods:
        link = getattr(order, "payment_link_url", None)
        if link:
            lines.append(f"💳 *Pay online:* {link}\n")

    lines.append("We'll notify you once payment has been confirmed. 🙏")

    return "\n".join(lines)


def build_payment_confirmed_message(order) -> str:
    """
    WhatsApp message sent when payment is confirmed.
    """
    return (
        f"Payment received ✅\n\n"
        f"Your order *{order.order_number}* payment has been confirmed. "
        f"We're now preparing your order! 👨‍🍳"
    )


def build_payment_timeout_message(order) -> str:
    """
    WhatsApp message sent when an unpaid order is auto-cancelled.
    """
    return (
        f"❌ Your order *{order.order_number}* has been cancelled.\n\n"
        f"Payment was not received within the required time. "
        f"Please place a new order if you still wish to proceed. 🙏"
    )
