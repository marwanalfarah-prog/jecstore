"""Report definitions, as exportable datasets (Part I §2.2).

Each function here turns a report into a :class:`Dataset` — the same shape the
screen renders and the CSV/Excel/PDF writers consume. That is what makes §2.2's
"every report and dashboard view must be exportable" true by construction: a
report is defined once, and the export follows.

Labels resolve through the normal ``t()`` translator so an Arabic export has
Arabic headers, not English ones with Arabic data underneath.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.i18n import format_date, translate
from app.models.consignment import Consignment, ConsignmentSettlement, Consignor
from app.models.enums import OrderStatus
from app.models.inventory import OperatingCost
from app.models.orders import Order
from app.services import consignment as consignment_service
from app.services import inventory, money
from app.services.exports import Column, Dataset

#: Report keys the export endpoint accepts.
#: §2.2 names them: "sales, inventory, consignment, money boxes, promocodes,
#: returns, staff activity". The last three were the outstanding ones.
REPORT_KEYS: tuple[str, ...] = (
    "financial_statement",
    "operating_costs",
    "sales",
    "inventory",
    "low_stock",
    "consignment",
    "money_boxes",
    "promocodes",
    "returns",
    "staff_activity",
)


def _t(key: str, language: str, **params) -> str:
    return translate(key, language, **params)


def _period(start_date: dt.date, end_date: dt.date, language: str) -> dict[str, str]:
    return {
        _t("exports.period", language): (
            f"{format_date(start_date, language)} – {format_date(end_date, language)}"
        )
    }


# ---------------------------------------------------------------------------
# Financial statement (Part I §11)
# ---------------------------------------------------------------------------


def financial_statement(
    db: Session, *, start_date: dt.date, end_date: dt.date, language: str = "en"
) -> Dataset:
    """The §11 statement as a line-item report.

    Rendered as label/amount rows rather than one wide row, because that is how
    an accountant reads a P&L and how it pastes into their own workbook.
    """
    statement = money.financial_statement(db, start_date=start_date, end_date=end_date)

    lines = [
        ("exports.gross_sales", statement.gross_sales_amt),
        ("exports.refunds", -statement.refunds_amt),
        ("exports.net_sales", statement.net_sales_amt),
        ("exports.cogs", -statement.cogs_amt),
        ("exports.gross_profit", statement.gross_profit_amt),
        ("exports.operating_costs", -statement.operating_costs_amt),
        ("exports.consignment_payouts", -statement.consignment_payouts_amt),
        ("exports.consignment_collections", statement.consignment_collections_amt),
        ("exports.net_profit", statement.net_profit_amt),
    ]

    return Dataset(
        title=_t("exports.financial_statement", language),
        language=language,
        columns=[
            Column("line", _t("exports.line_item", language)),
            Column("amount", _t("admin.amount", language), kind="money"),
        ],
        rows=[{"line": _t(key, language), "amount": amount} for key, amount in lines],
        meta=_period(start_date, end_date, language),
        totals={"line": _t("exports.net_profit", language), "amount": statement.net_profit_amt},
    )


# ---------------------------------------------------------------------------
# Operating costs (Part I §11)
# ---------------------------------------------------------------------------


def operating_costs(
    db: Session,
    *,
    start_date: dt.date,
    end_date: dt.date,
    category_code: str | None = None,
    language: str = "en",
) -> Dataset:
    rows = money.operating_costs(
        db, start_date=start_date, end_date=end_date, category_code=category_code
    )

    meta = _period(start_date, end_date, language)
    if category_code:
        meta[_t("exports.category", language)] = category_code

    return Dataset(
        title=_t("exports.operating_costs", language),
        language=language,
        columns=[
            Column("date", _t("exports.date", language), kind="date"),
            Column("name", _t("admin.product", language)),
            Column("category", _t("exports.category", language)),
            Column("amount", _t("admin.amount", language), kind="money"),
        ],
        rows=[
            {
                "date": cost.incurred_date,
                "name": cost.name_ar if language == "ar" else cost.name_en,
                "category": cost.category_code,
                "amount": cost.amount_amt,
            }
            for cost in rows
        ],
        meta=meta,
        totals={
            "date": "",
            "name": _t("cart.total", language),
            "category": "",
            "amount": sum((Decimal(c.amount_amt) for c in rows), Decimal("0")),
        },
    )


# ---------------------------------------------------------------------------
# Sales (Part I §9)
# ---------------------------------------------------------------------------


def sales(
    db: Session, *, start_date: dt.date, end_date: dt.date, language: str = "en"
) -> Dataset:
    """Orders placed in the period, excluding cancellations."""
    start_dt = dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc)
    end_dt = dt.datetime.combine(
        end_date + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc
    )

    orders = db.scalars(
        select(Order)
        .where(
            Order.scd_active_flag.is_(True),
            Order.placed_dt >= start_dt,
            Order.placed_dt < end_dt,
            Order.status != OrderStatus.CANCELLED,
        )
        .order_by(Order.placed_dt)
    ).all()

    return Dataset(
        title=_t("exports.sales", language),
        language=language,
        columns=[
            Column("number", _t("orders.order_number", language)),
            Column("placed", _t("admin.placed", language), kind="date"),
            Column("status", _t("admin.status", language)),
            Column("payment", _t("orders.payment_status", language)),
            Column("subtotal", _t("cart.subtotal", language), kind="money"),
            Column("discount", _t("cart.discount", language), kind="money"),
            Column("shipping", _t("cart.shipping", language), kind="money"),
            Column("total", _t("cart.total", language), kind="money"),
        ],
        rows=[
            {
                "number": o.order_number,
                "placed": o.placed_dt,
                "status": _t(f"orders.status_{o.status}", language),
                "payment": _t(f"orders.payment_{o.payment_status}", language),
                "subtotal": o.subtotal_amt,
                "discount": Decimal(o.invoice_discount_amt or 0)
                + Decimal(o.promocode_discount_amt or 0),
                "shipping": o.shipping_amt,
                "total": o.total_amt,
            }
            for o in orders
        ],
        meta=_period(start_date, end_date, language),
        totals={
            "number": "",
            "placed": "",
            "status": "",
            "payment": _t("cart.total", language),
            "subtotal": sum((Decimal(o.subtotal_amt) for o in orders), Decimal("0")),
            "discount": sum(
                (
                    Decimal(o.invoice_discount_amt or 0) + Decimal(o.promocode_discount_amt or 0)
                    for o in orders
                ),
                Decimal("0"),
            ),
            "shipping": sum((Decimal(o.shipping_amt) for o in orders), Decimal("0")),
            "total": sum((Decimal(o.total_amt) for o in orders), Decimal("0")),
        },
    )


# ---------------------------------------------------------------------------
# Inventory (Part I §11)
# ---------------------------------------------------------------------------


def inventory_positions(
    db: Session, *, only_low: bool = False, language: str = "en"
) -> Dataset:
    rows = inventory.stock_positions(db, only_low=only_low, limit=5000)

    return Dataset(
        title=_t(
            "exports.low_stock" if only_low else "exports.inventory", language
        ),
        language=language,
        columns=[
            Column("product", _t("admin.product", language)),
            Column("sku", "SKU"),
            Column("on_hand", _t("inventory.on_hand", language), kind="number"),
            Column("reserved", _t("inventory.reserved", language), kind="number"),
            Column("sellable", _t("inventory.sellable", language), kind="number"),
            Column("minimum", _t("inventory.min", language), kind="number"),
            Column("restock", _t("exports.restock_to_optimal", language), kind="number"),
            Column("avg_cost", _t("inventory.avg_cost", language), kind="money"),
            Column("stock_value", _t("exports.stock_value", language), kind="money"),
        ],
        rows=[
            {
                "product": r.product.name_ar if language == "ar" else r.product.name_en,
                "sku": r.variant.sku,
                "on_hand": r.on_hand,
                "reserved": r.reserved,
                "sellable": r.sellable,
                "minimum": r.minimum,
                "restock": r.restock_to_optimal,
                "avg_cost": r.average_cost_amt,
                # What the shelf is worth at cost — the figure an accountant
                # actually needs from an inventory export.
                "stock_value": Decimal(r.on_hand) * Decimal(r.average_cost_amt),
            }
            for r in rows
        ],
        meta={_t("exports.generated", language): format_date(dt.date.today(), language)},
        totals={
            "product": _t("cart.total", language),
            "on_hand": sum(r.on_hand for r in rows),
            "sellable": sum(r.sellable for r in rows),
            "stock_value": sum(
                (Decimal(r.on_hand) * Decimal(r.average_cost_amt) for r in rows),
                Decimal("0"),
            ),
        },
    )


# ---------------------------------------------------------------------------
# Consignment (Part I §7)
# ---------------------------------------------------------------------------


def consignment_positions(db: Session, *, language: str = "en") -> Dataset:
    """Who holds what and what is owed, across every open arrangement."""
    arrangements = consignment_service.list_arrangements(db, open_only=False)
    consignors = {
        c.pk_consignor_id: c
        for c in db.scalars(select(Consignor).where(Consignor.scd_active_flag.is_(True))).all()
    }

    rows = []
    for arrangement in arrangements:
        position = consignment_service.settlement_position(db, arrangement)
        held = sum(h.outstanding for h in consignment_service.holdings(db, arrangement))
        rows.append(
            {
                "reference": arrangement.reference,
                "consignor": (
                    consignors[arrangement.fk_consignor_id].name
                    if arrangement.fk_consignor_id in consignors
                    else ""
                ),
                "direction": _t(
                    f"consignment.direction_{arrangement.direction}", language
                ),
                "held": held,
                "our_share": position.our_share_amt,
                "their_share": position.their_share_amt,
                "net_owed": position.net_owed_amt,
            }
        )

    return Dataset(
        title=_t("exports.consignment", language),
        language=language,
        columns=[
            Column("reference", _t("admin.reference", language)),
            Column("consignor", _t("consignment.consignor", language)),
            Column("direction", _t("consignment.direction", language)),
            Column("held", _t("consignment.outstanding", language), kind="number"),
            Column("our_share", _t("consignment.our_share", language), kind="money"),
            Column("their_share", _t("consignment.their_share", language), kind="money"),
            Column("net_owed", _t("consignment.net_owed", language), kind="money"),
        ],
        rows=rows,
        meta={_t("exports.generated", language): format_date(dt.date.today(), language)},
        totals={
            "reference": _t("cart.total", language),
            "net_owed": sum((r["net_owed"] for r in rows), Decimal("0")),
        },
    )


# ---------------------------------------------------------------------------
# Promocodes (Part I §13, named by §2.2)
# ---------------------------------------------------------------------------


def promocodes(
    db: Session, *, start_date: dt.date, end_date: dt.date, language: str = "en"
) -> Dataset:
    """What each code was used for, and what it cost.

    Redemption rows are insert-only and carry a signed reversal when an order is
    cancelled (Part I §13), so summing ``discount_amt`` over them gives a code's
    true net cost — a stored counter on the promocode could not.
    """
    from app.models.marketing import Promocode, PromocodeRedemption

    start, end = _period_bounds(start_date, end_date)

    totals = {
        promocode_id: (int(uses), amount or Decimal("0"))
        for promocode_id, uses, amount in db.execute(
            select(
                PromocodeRedemption.fk_promocode_id,
                func.count(),
                func.sum(PromocodeRedemption.discount_amt),
            )
            .where(
                PromocodeRedemption.created_dt >= start,
                PromocodeRedemption.created_dt < end,
            )
            .group_by(PromocodeRedemption.fk_promocode_id)
        ).all()
    }

    codes = db.scalars(
        select(Promocode)
        .where(Promocode.scd_active_flag.is_(True))
        .order_by(Promocode.code)
    ).all()

    rows = []
    for code in codes:
        uses, discount = totals.get(code.pk_promocode_id, (0, Decimal("0")))
        rows.append(
            {
                "code": code.code,
                "name": (code.name_ar if language == "ar" else code.name_en) or "",
                "kind": _t(f"promocodes.kind_{code.promocode_kind}", language),
                "expires": (
                    format_date(code.expires_dt.date(), language)
                    if code.expires_dt
                    else ""
                ),
                "uses": uses,
                "discount": discount,
            }
        )

    return Dataset(
        title=_t("exports.promocodes", language),
        language=language,
        columns=[
            Column("code", _t("promocodes.code", language)),
            Column("name", _t("promocodes.name", language)),
            Column("kind", _t("promocodes.kind", language)),
            Column("expires", _t("promocodes.expires", language), kind="date"),
            Column("uses", _t("promocodes.redemptions", language), kind="number"),
            Column("discount", _t("promocodes.discount_given", language), kind="money"),
        ],
        rows=rows,
        meta=_period(start_date, end_date, language),
        totals={
            "code": _t("cart.total", language),
            "uses": sum(row["uses"] for row in rows),
            "discount": sum((row["discount"] for row in rows), Decimal("0")),
        },
    )


# ---------------------------------------------------------------------------
# Returns (Part I §12, named by §2.2)
# ---------------------------------------------------------------------------


def returns(
    db: Session, *, start_date: dt.date, end_date: dt.date, language: str = "en"
) -> Dataset:
    """Every return raised in the period, with what it refunded and why.

    Withdrawn requests are included and labelled rather than filtered out: a
    report that silently drops them makes the refusal rate look worse than it
    is, which is the reason §12 keeps WITHDRAWN distinct from REJECTED at all.
    """
    from app.models.orders import Order, OrderReturn

    start, end = _period_bounds(start_date, end_date)

    pairs = db.execute(
        select(OrderReturn, Order)
        .join(Order, Order.pk_order_id == OrderReturn.fk_order_id)
        .where(
            OrderReturn.scd_active_flag.is_(True),
            OrderReturn.scd_active_from >= start,
            OrderReturn.scd_active_from < end,
        )
        .order_by(OrderReturn.scd_active_from.desc())
    ).all()

    rows = [
        {
            "return_number": row.return_number,
            "order_number": order.order_number,
            "raised": format_date(row.scd_active_from.date(), language),
            "reason": _t(f"returns.reason_{row.reason_code}", language),
            "status": _t(f"returns.status_{row.status}", language),
            "destination": (
                _t(f"admin.refund_{row.refund_destination}", language)
                if row.refund_destination
                else ""
            ),
            "refund": row.refund_amt,
        }
        for row, order in pairs
    ]

    return Dataset(
        title=_t("exports.returns", language),
        language=language,
        columns=[
            Column("return_number", _t("returns.return_number", language)),
            Column("order_number", _t("orders.order_number", language)),
            Column("raised", _t("returns.raised", language), kind="date"),
            Column("reason", _t("returns.reason", language)),
            Column("status", _t("admin.status", language)),
            Column("destination", _t("admin.refund_destination", language)),
            Column("refund", _t("returns.refund_amount", language), kind="money"),
        ],
        rows=rows,
        meta=_period(start_date, end_date, language),
        totals={
            "return_number": _t("cart.total", language),
            "refund": sum((row["refund"] for row in rows), Decimal("0")),
        },
    )


# ---------------------------------------------------------------------------
# Staff activity (Part I §2.2, §2.8)
# ---------------------------------------------------------------------------


def staff_activity(
    db: Session, *, start_date: dt.date, end_date: dt.date, language: str = "en"
) -> Dataset:
    """What each staff member did, counted per module and action.

    Read from the audit log rather than the activity stream: §2.2 asks who
    changed what, and the audit log is the immutable record of exactly that.
    Impersonated actions are counted in their own column, because §2.2.2 is
    explicit that they must never be conflated with the target's own work.
    """
    from app.models.access import AuditLog

    start, end = _period_bounds(start_date, end_date)

    grouped = db.execute(
        select(
            AuditLog.actor_username,
            AuditLog.module_code,
            AuditLog.action_code,
            func.count(),
            func.sum(case((AuditLog.impersonator_user_id.is_not(None), 1), else_=0)),
            func.max(AuditLog.created_dt),
        )
        .where(AuditLog.created_dt >= start, AuditLog.created_dt < end)
        .group_by(AuditLog.actor_username, AuditLog.module_code, AuditLog.action_code)
        .order_by(func.count().desc())
    ).all()

    rows = [
        {
            "actor": actor or _t("audit.system", language),
            "module": module,
            "action": action,
            "count": int(total),
            "impersonated": int(impersonated or 0),
            "last": format_date(last.date(), language) if last else "",
        }
        for actor, module, action, total, impersonated, last in grouped
    ]

    return Dataset(
        title=_t("exports.staff_activity", language),
        language=language,
        columns=[
            Column("actor", _t("audit.actor", language)),
            Column("module", _t("audit.module", language)),
            Column("action", _t("audit.action", language)),
            Column("count", _t("audit.actions_taken", language), kind="number"),
            Column("impersonated", _t("audit.impersonated", language), kind="number"),
            Column("last", _t("audit.last_action", language), kind="date"),
        ],
        rows=rows,
        meta=_period(start_date, end_date, language),
        totals={
            "actor": _t("cart.total", language),
            "count": sum(row["count"] for row in rows),
        },
    )


def _period_bounds(
    start_date: dt.date, end_date: dt.date
) -> tuple[dt.datetime, dt.datetime]:
    """The half-open UTC window for an inclusive pair of dates.

    The end bound is midnight on the day *after* ``end_date``: comparing against
    midnight on the end date itself silently drops everything that happened on
    the last day of the period, which is a report's most recent — and most
    scrutinised — day.
    """
    return (
        dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc),
        dt.datetime.combine(
            end_date + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc
        ),
    )


# ---------------------------------------------------------------------------
# Money boxes (Part I §10)
# ---------------------------------------------------------------------------


def money_boxes(db: Session, *, language: str = "en") -> Dataset:
    from app.models.money import MoneyBox

    balances = money.box_balances(db)
    boxes = db.scalars(
        select(MoneyBox).where(MoneyBox.scd_active_flag.is_(True))
    ).all()

    return Dataset(
        title=_t("exports.money_boxes", language),
        language=language,
        columns=[
            Column("name", _t("admin.money_box", language)),
            Column("code", _t("admin.reference", language)),
            Column("opening", _t("exports.opening_balance", language), kind="money"),
            Column("balance", _t("exports.balance", language), kind="money"),
            Column("status", _t("admin.status", language)),
        ],
        rows=[
            {
                "name": box.name_ar if language == "ar" else box.name_en,
                "code": box.box_code,
                "opening": box.opening_balance_amt,
                "balance": balances.get(box.pk_money_box_id, Decimal("0")),
                "status": _t(
                    "content.enabled" if box.is_open_flag else "content.disabled", language
                ),
            }
            for box in boxes
        ],
        meta={_t("exports.generated", language): format_date(dt.date.today(), language)},
        totals={
            "name": _t("cart.total", language),
            "balance": sum(balances.values(), Decimal("0")),
        },
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def build(
    db: Session,
    report_key: str,
    *,
    language: str = "en",
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    category_code: str | None = None,
) -> Dataset:
    """Build any report by key — the single entry point the export route uses."""
    from app.core.errors import NotFound

    today = dt.date.today()
    start_date = start_date or today.replace(day=1)
    end_date = end_date or today

    match report_key:
        case "financial_statement":
            return financial_statement(
                db, start_date=start_date, end_date=end_date, language=language
            )
        case "operating_costs":
            return operating_costs(
                db, start_date=start_date, end_date=end_date,
                category_code=category_code, language=language,
            )
        case "sales":
            return sales(db, start_date=start_date, end_date=end_date, language=language)
        case "inventory":
            return inventory_positions(db, language=language)
        case "low_stock":
            return inventory_positions(db, only_low=True, language=language)
        case "consignment":
            return consignment_positions(db, language=language)
        case "money_boxes":
            return money_boxes(db, language=language)
        case "promocodes":
            return promocodes(
                db, start_date=start_date, end_date=end_date, language=language
            )
        case "returns":
            return returns(
                db, start_date=start_date, end_date=end_date, language=language
            )
        case "staff_activity":
            return staff_activity(
                db, start_date=start_date, end_date=end_date, language=language
            )
        case _:
            raise NotFound("That report does not exist.")
