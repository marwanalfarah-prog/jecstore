"""Shared enumerations.

Stored as short strings, not integers: a raw row stays readable during a cash
audit or an accounting handoff, and the value survives a database migration
without a lookup table to join. Values are stable identifiers — display labels
are translated at the presentation layer (see ``locales/``), never here.
"""

from __future__ import annotations

from enum import StrEnum


class Language(StrEnum):
    AR = "ar"
    EN = "en"


class Currency(StrEnum):
    """Prices are stored in JOD only; USD is display-only (Part I §1.1)."""

    JOD = "JOD"
    USD = "USD"


# --- Identity & access -----------------------------------------------------


class RoleCode(StrEnum):
    """The five account types in Part I §2.1. Seeded, but not a closed set —
    Admin can create further roles, which inherit the same permission model."""

    ADMIN = "admin"
    GENERAL_SECRETARIAT = "general_secretariat"
    STORE_MANAGER = "store_manager"
    STORE_EMPLOYEE = "store_employee"
    CUSTOMER = "customer"


class CustomerType(StrEnum):
    INDIVIDUAL = "individual"
    COMPANY = "company"


class ApprovalMode(StrEnum):
    """Per-permission approval mode (Part I §2.2.1)."""

    SINGLE = "single"
    MAKER_CHECKER = "maker_checker"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class GrantScope(StrEnum):
    """Whether a permission/approval rule attaches to a role or one username."""

    ROLE = "role"
    USER = "user"


class SessionEndReason(StrEnum):
    LOGOUT = "logout"
    IDLE_TIMEOUT = "idle_timeout"
    PASSWORD_CHANGED = "password_changed"
    FORCED_BY_ADMIN = "forced_by_admin"
    IMPERSONATION_ENDED = "impersonation_ended"


class ActivityEvent(StrEnum):
    """Everything written to the insert-only activity log (Part I §2.8)."""

    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    RATE_LIMITED = "rate_limited"
    ACCOUNT_LOCKED = "account_locked"
    PAGE_VIEW = "page_view"
    PRODUCT_VIEW = "product_view"
    CART_ITEM_ADDED = "cart_item_added"
    CART_ITEM_REMOVED = "cart_item_removed"
    CART_ABANDONED = "cart_abandoned"
    CART_CONVERTED = "cart_converted"
    IMPERSONATION_STARTED = "impersonation_started"
    IMPERSONATION_ENDED = "impersonation_ended"


# --- Catalog ---------------------------------------------------------------


class DiscountKind(StrEnum):
    PERCENTAGE = "percentage"
    FIXED_PRICE = "fixed_price"


class DiscountScope(StrEnum):
    PRODUCT = "product"
    CATEGORY = "category"


class OverlapRule(StrEnum):
    """How two active discounts on one product combine — configurable per
    product, not one global rule (Part I §5.5)."""

    BEST_FOR_CUSTOMER = "best_for_customer"
    ADDITIVE = "additive"
    FIRST_MATCH = "first_match"


class AttributeInputType(StrEnum):
    TEXT = "text"
    DROPDOWN = "dropdown"


class AttributeVisibility(StrEnum):
    PUBLIC = "public"
    ADMIN_ONLY = "admin_only"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# --- Inventory -------------------------------------------------------------


class StockPoolKind(StrEnum):
    BRANCH = "branch"
    CENTRAL_STORAGE = "central_storage"
    CONSIGNMENT_OUT = "consignment_out"


class MovementKind(StrEnum):
    """Every reason stock moves. ``WRITE_OFF`` gives damage/loss/expiry a
    defined home rather than nowhere to go (Part I §11)."""

    SHIPMENT_IN = "shipment_in"
    SALE = "sale"
    RETURN_IN = "return_in"
    TRANSFER_OUT = "transfer_out"
    TRANSFER_IN = "transfer_in"
    WRITE_OFF = "write_off"
    STOCK_TAKE_ADJUSTMENT = "stock_take_adjustment"
    CONSIGNMENT_OUT = "consignment_out"
    CONSIGNMENT_RETURN = "consignment_return"
    RESERVATION_HOLD = "reservation_hold"
    RESERVATION_RELEASE = "reservation_release"


#: Movements that change how many units are physically present in a pool.
#: Summing these reproduces ``SCD_STOCK_LEVEL.quantity_on_hand``.
ON_HAND_MOVEMENT_KINDS: frozenset[str] = frozenset({
    MovementKind.SHIPMENT_IN,
    MovementKind.SALE,
    MovementKind.RETURN_IN,
    MovementKind.TRANSFER_OUT,
    MovementKind.TRANSFER_IN,
    MovementKind.WRITE_OFF,
    MovementKind.STOCK_TAKE_ADJUSTMENT,
    MovementKind.CONSIGNMENT_OUT,
    MovementKind.CONSIGNMENT_RETURN,
})

#: Movements that change how many present units are *promised* to a placed
#: order. These never touch on-hand — on checkout quantities go on hold and are
#: only deducted at hand-over (Part I §8) — so they are summed separately to
#: reproduce ``SCD_STOCK_LEVEL.quantity_reserved``. Keeping the two sets apart
#: is what lets the reconciliation report check each projection against its own
#: slice of the ledger instead of conflating them.
RESERVATION_MOVEMENT_KINDS: frozenset[str] = frozenset({
    MovementKind.RESERVATION_HOLD,
    MovementKind.RESERVATION_RELEASE,
})


class WriteOffReason(StrEnum):
    DAMAGED = "damaged"
    LOST = "lost"
    EXPIRED = "expired"


# --- Orders ----------------------------------------------------------------


class FulfillmentMethod(StrEnum):
    """Tracked per line item — one order may mix both (Part I §9)."""

    PICKUP = "pickup"
    SHIPPING = "shipping"


class OrderStatus(StrEnum):
    """Order-level rollup of its line statuses."""

    PLACED = "placed"
    IN_PREPARATION = "in_preparation"
    PARTIALLY_FULFILLED = "partially_fulfilled"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


class LineFulfillmentStatus(StrEnum):
    """The pipeline in Part I §9, held per line so split/partial fulfilment and
    backorders are representable without a second order."""

    ORDERED_FOR_PICKUP = "ordered_for_pickup"
    ORDERED_FOR_DELIVERY = "ordered_for_delivery"
    BACKORDERED = "backordered"
    READY_FOR_PICKUP = "ready_for_pickup"
    ON_ROUTE = "on_route"
    DELIVERED = "delivered"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


class PaymentStatus(StrEnum):
    NOT_PAID = "not_paid"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class ReturnStatus(StrEnum):
    """A return is inspected before any money moves (Part I §12)."""

    REQUESTED = "requested"
    UNDER_INSPECTION = "under_inspection"
    APPROVED = "approved"
    REJECTED = "rejected"
    REFUNDED = "refunded"
    #: The customer changed their mind before staff looked at it. Distinct from
    #: REJECTED so a shopper who withdrew a request is not shown as refused, and
    #: so the returns queue does not read as if staff turned people away.
    WITHDRAWN = "withdrawn"


class RefundDestination(StrEnum):
    MONEY_BOX = "money_box"
    STORE_CREDIT = "store_credit"


# --- Money -----------------------------------------------------------------


class MoneyDirection(StrEnum):
    IN = "in"
    OUT = "out"


class MoneyReason(StrEnum):
    SALE = "sale"
    REFUND = "refund"
    CANCELLATION_REFUND = "cancellation_refund"
    CONSIGNMENT_SETTLEMENT = "consignment_settlement"
    SHIPMENT_PURCHASE = "shipment_purchase"
    OPERATING_COST = "operating_cost"
    OPENING_BALANCE = "opening_balance"
    TRANSFER_BETWEEN_BOXES = "transfer_between_boxes"
    RECONCILIATION_ADJUSTMENT = "reconciliation_adjustment"
    STORE_CREDIT_TOPUP = "store_credit_topup"
    STORE_CREDIT_SPEND = "store_credit_spend"
    OTHER = "other"


# --- Consignment -----------------------------------------------------------


class ConsignmentDirection(StrEnum):
    OUTBOUND = "outbound"  # our items, held by someone else to sell
    INBOUND = "inbound"    # their items, held by us to sell


class ConsignmentSplitBasis(StrEnum):
    """Whether the revenue split is taken on the discounted or original price —
    Admin's choice, per arrangement (Part I §7)."""

    DISCOUNTED_PRICE = "discounted_price"
    ORIGINAL_PRICE = "original_price"


class ConsignmentItemState(StrEnum):
    HELD = "held"
    SOLD = "sold"
    RETURNED = "returned"
    RECALLED = "recalled"
    DAMAGED_OR_LOST = "damaged_or_lost"


# --- Promocodes ------------------------------------------------------------


class PromocodeKind(StrEnum):
    PERCENTAGE = "percentage"
    PERCENTAGE_CAPPED = "percentage_capped"
    FIXED_AMOUNT = "fixed_amount"


# --- Homepage / content ----------------------------------------------------


class HomepageSectionKind(StrEnum):
    """Auto-populating carousels ship as section types so Admin drops one in
    without curating it by hand (Part I §4)."""

    BANNER = "banner"
    CUSTOM_HTML = "custom_html"
    PROMO_BLOCK = "promo_block"
    CURATED_PRODUCTS = "curated_products"
    NEW_ARRIVALS = "new_arrivals"
    DISCOUNTED = "discounted"
    BEST_SELLERS = "best_sellers"
    MOST_VIEWED = "most_viewed"
    CATEGORY_SHOWCASE = "category_showcase"
    PUBLISHER_CAROUSEL = "publisher_carousel"
    ANNOUNCEMENT = "announcement"


class AuthTokenPurpose(StrEnum):
    """What a single-use link is for (Part I §2.5, §2.7).

    Purpose is part of the lookup, so a verification link can never be replayed
    as a password reset — the two arrive by the same channel and would
    otherwise be interchangeable to anyone holding one.
    """

    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_RESET = "password_reset"


class EmailTemplateCode(StrEnum):
    """One shared template system, not one-off emails (Part I §2.7)."""

    WELCOME = "welcome"
    EMAIL_VERIFICATION = "email_verification"
    FORGOT_PASSWORD = "forgot_password"
    ORDER_CONFIRMATION = "order_confirmation"
    ORDER_STATUS_CHANGE = "order_status_change"
    PAYMENT_RECEIVED = "payment_received"
    LOW_STOCK_ALERT = "low_stock_alert"
    RETURN_PROCESSED = "return_processed"
    NEWSLETTER = "newsletter"
