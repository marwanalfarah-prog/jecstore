"""Invoice and receipt generation (Part I §9, §1.1).

§9 requires a printable and emailable invoice per order. §1.1 adds the rule that
governs everything here: **the currency must be clearly specified whenever an
invoice is downloaded.**

That rule is why an invoice is not simply "the order page in a print
stylesheet". An invoice reprinted two years later must show what the customer
actually agreed to, so it is built entirely from values **frozen on the order at
sale time** — line prices, unit costs, the USD rate, the product names, the
delivery address. Nothing is re-derived from the live catalog, because a later
rename or price change must not rewrite history.

PDF rendering goes through WeasyPrint where its native libraries exist, and
falls back to the same print-ready HTML otherwise — see ``services/exports.py``
for why that split exists.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.i18n import CURRENCY_DECIMALS, convert_jod, format_number
from app.core.logging import get_logger
from app.models.enums import Currency, RefundDestination, ReturnStatus
from app.models.identity import User
from app.models.inventory import Branch
from app.models.orders import (
    Order,
    OrderLine,
    OrderReturn,
    OrderReturnLine,
    Payment,
    PaymentChannel,
)
from app.services.pricing import q

log = get_logger(__name__)


@dataclass(slots=True)
class InvoiceLine:
    name: str
    variant_label: str | None
    sku: str | None
    quantity: int
    unit_price_amt: Decimal
    list_price_amt: Decimal
    line_total_amt: Decimal
    #: How many of this line came back on a settled return. The ordered
    #: quantity is never edited down — §9's invoice has to keep showing what
    #: the customer agreed to — so the return is shown against it instead.
    returned_quantity: int = 0

    @property
    def was_discounted(self) -> bool:
        return self.list_price_amt > self.unit_price_amt

    @property
    def was_returned(self) -> bool:
        return self.returned_quantity > 0

    @property
    def kept_quantity(self) -> int:
        return self.quantity - self.returned_quantity


@dataclass(slots=True)
class InvoiceReturn:
    """One settled return against this order (Part I §12).

    Only refunded returns appear. A request still being inspected has moved no
    money and returned no goods, so putting it on the invoice would credit the
    customer for something nobody has agreed to yet — §12 is explicit that the
    condition check gates the refund.
    """

    return_number: str
    refunded_dt: dt.datetime | None
    refund_amt: Decimal
    #: Cash out of a money box, or converted to رصيد. The customer needs to
    #: know which: one of them is spendable anywhere and one is not.
    destination: str | None
    lines: list[tuple[str, int]] = field(default_factory=list)

    @property
    def to_store_credit(self) -> bool:
        return self.destination == RefundDestination.STORE_CREDIT


@dataclass(slots=True)
class InvoicePayment:
    channel: str
    amount_amt: Decimal
    reference: str | None
    paid_dt: dt.datetime

    @property
    def is_refund(self) -> bool:
        return self.amount_amt < 0


@dataclass(slots=True)
class Invoice:
    """Everything the document shows, resolved once.

    A view model rather than the ORM rows, so the template cannot accidentally
    reach past the frozen values into live catalog data.
    """

    order: Order
    customer: User | None
    lines: list[InvoiceLine]
    payments: list[InvoicePayment]
    language: str
    #: The currency the document is *denominated* in. JOD always; a USD figure
    #: is shown alongside when the customer shopped in USD, never instead.
    currency: str = Currency.JOD
    pickup_branch: Branch | None = None
    paid_amt: Decimal = Decimal("0")
    returns: list[InvoiceReturn] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def is_rtl(self) -> bool:
        return self.language == "ar"

    @property
    def returned_amt(self) -> Decimal:
        """Credited back on settled returns."""
        return q(sum((r.refund_amt for r in self.returns), Decimal("0")))

    @property
    def net_total_amt(self) -> Decimal:
        """What the order came to once returns are taken off.

        `order.total_amt` is the figure agreed at sale and stays that way, so
        the returns are subtracted here rather than written back onto the order.
        """
        return q(Decimal(self.order.total_amt) - self.returned_amt)

    @property
    def balance_amt(self) -> Decimal:
        """What is still owed — or, when negative, still owed *to* the customer.

        Measured against the net total, not the original. A refund writes a
        negative payment row, so charging it against the pre-return total
        counted it twice: the customer's payment went down, the amount owed did
        not, and a fully settled order printed a balance due for exactly the
        sum that had just been handed back.
        """
        return q(self.net_total_amt - self.paid_amt)

    @property
    def has_returns(self) -> bool:
        return bool(self.returns)

    @property
    def is_refunded(self) -> bool:
        """Every last dinar came back."""
        return self.paid_amt <= 0 and Decimal(self.order.total_amt) > 0

    @property
    def shows_secondary_currency(self) -> bool:
        """True when a USD equivalent is worth printing alongside the JOD total.

        Only when the customer actually shopped in USD — printing a conversion
        nobody asked for invites the reader to think they were charged in it.
        """
        return self.order.display_currency == Currency.USD

    def secondary_total(self) -> str:
        """The USD equivalent, at the rate frozen at time of sale (§1.1)."""
        rate = Decimal(self.order.usd_rate_at_sale or 0)
        converted = convert_jod(Decimal(self.order.total_amt), Currency.USD, rate)
        decimals = CURRENCY_DECIMALS[Currency.USD]
        return format_number(converted, self.language, decimals=decimals)


def build(
    db: Session, order: Order, *, language: str = "ar"
) -> Invoice:
    """Assemble the invoice for one order from its frozen data."""
    lines = db.scalars(
        select(OrderLine)
        .where(
            OrderLine.fk_order_id == order.pk_order_id,
            OrderLine.scd_active_flag.is_(True),
        )
        .order_by(OrderLine.pk_order_line_id)
    ).all()

    payment_rows = db.scalars(
        select(Payment)
        .where(Payment.fk_order_id == order.pk_order_id)
        .order_by(Payment.pk_payment_id)
    ).all()
    channels = {
        c.pk_payment_channel_id: c
        for c in db.scalars(select(PaymentChannel)).all()
    }

    paid = q(sum((Decimal(p.amount_amt) for p in payment_rows), Decimal("0")))
    settled_returns, returned_per_line = _returns(db, order, language)

    return Invoice(
        order=order,
        customer=db.get(User, order.fk_user_id),
        language=language,
        pickup_branch=(
            db.get(Branch, order.fk_pickup_branch_id)
            if order.fk_pickup_branch_id
            else None
        ),
        paid_amt=paid,
        returns=settled_returns,
        lines=[
            InvoiceLine(
                # Names copied onto the line at sale, so a later product rename
                # cannot rewrite a past invoice (§9).
                name=(line.product_name_ar if language == "ar" else line.product_name_en)
                or line.sku
                or "",
                variant_label=(
                    line.variant_label_ar if language == "ar" else line.variant_label_en
                ),
                sku=line.sku,
                quantity=line.quantity,
                unit_price_amt=Decimal(line.unit_price_amt),
                list_price_amt=Decimal(line.list_price_amt),
                line_total_amt=Decimal(line.line_total_amt),
                returned_quantity=returned_per_line.get(line.pk_order_line_id, 0),
            )
            for line in lines
        ],
        payments=[
            InvoicePayment(
                channel=(
                    (
                        channels[p.fk_payment_channel_id].name_ar
                        if language == "ar"
                        else channels[p.fk_payment_channel_id].name_en
                    )
                    if p.fk_payment_channel_id in channels
                    else ""
                ),
                amount_amt=Decimal(p.amount_amt),
                reference=p.reference,
                paid_dt=p.created_dt,
            )
            for p in payment_rows
        ],
    )


def _returns(
    db: Session, order: Order, language: str
) -> tuple[list[InvoiceReturn], dict[int, int]]:
    """Settled returns against this order, and the quantity back per line.

    Refunded only. A return still under inspection has moved no money and
    settled no goods — §12 gates the refund on the condition check — so
    crediting it on the invoice would promise the customer something no member
    of staff has agreed to.
    """
    rows = db.scalars(
        select(OrderReturn)
        .where(
            OrderReturn.fk_order_id == order.pk_order_id,
            OrderReturn.status == ReturnStatus.REFUNDED,
            OrderReturn.scd_active_flag.is_(True),
        )
        .order_by(OrderReturn.pk_order_return_id)
    ).all()
    if not rows:
        return [], {}

    return_lines = db.scalars(
        select(OrderReturnLine).where(
            OrderReturnLine.fk_order_return_id.in_(
                [r.pk_order_return_id for r in rows]
            ),
            OrderReturnLine.scd_active_flag.is_(True),
        )
    ).all()

    order_lines = {
        line.pk_order_line_id: line
        for line in db.scalars(
            select(OrderLine).where(OrderLine.fk_order_id == order.pk_order_id)
        ).all()
    }

    per_line: dict[int, int] = {}
    per_return: dict[int, list[tuple[str, int]]] = {}
    for line in return_lines:
        per_line[line.fk_order_line_id] = (
            per_line.get(line.fk_order_line_id, 0) + line.quantity
        )
        source = order_lines.get(line.fk_order_line_id)
        name = ""
        if source is not None:
            name = (
                source.product_name_ar if language == "ar" else source.product_name_en
            ) or (source.sku or "")
        per_return.setdefault(line.fk_order_return_id, []).append(
            (name, line.quantity)
        )

    return (
        [
            InvoiceReturn(
                return_number=row.return_number,
                refunded_dt=row.refunded_dt,
                refund_amt=Decimal(row.refund_amt),
                destination=row.refund_destination,
                lines=per_return.get(row.pk_order_return_id, []),
            )
            for row in rows
        ],
        per_line,
    )


def for_order_number(
    db: Session, order_number: str, *, user_id: int | None = None, language: str = "ar"
) -> Invoice:
    """Look up an order and build its invoice.

    ``user_id`` scopes the lookup to one customer, so an order number cannot
    expose somebody else's invoice just because it was guessed.
    """
    stmt = select(Order).where(
        Order.order_number == order_number, Order.scd_active_flag.is_(True)
    )
    if user_id is not None:
        stmt = stmt.where(Order.fk_user_id == user_id)

    order = db.scalars(stmt).first()
    if order is None:
        raise NotFound("That order could not be found.")
    return build(db, order, language=language)


def render_html(request: Request, invoice: Invoice) -> str:
    """Render the print-ready invoice document."""
    from app.core.templating import templates

    response = templates.TemplateResponse(
        request, "documents/invoice.html", {"invoice": invoice}
    )
    return response.body.decode("utf-8")


def render_pdf(request: Request, invoice: Invoice) -> bytes:
    from app.services.exports import PdfUnavailable, pdf_available

    if not pdf_available():
        raise PdfUnavailable()

    from weasyprint import HTML

    return HTML(
        string=render_html(request, invoice), base_url=str(request.base_url)
    ).write_pdf()


def document_filename(invoice: Invoice, fmt: str) -> str:
    return f"{invoice.order.order_number}.{fmt}"


def queue_invoice_email(db: Session, invoice: Invoice) -> None:
    """Email the customer their invoice (§9: "printable/emailable").

    Sends the order-confirmation template with a link rather than an attachment:
    it keeps the message small, works where PDF rendering is unavailable, and
    the link is access-controlled so a forwarded email does not leak the
    invoice.
    """
    from app.core.config import settings
    from app.models.enums import EmailTemplateCode
    from app.services.email import queue_template_email

    if invoice.customer is None:
        return

    queue_template_email(
        db,
        EmailTemplateCode.ORDER_CONFIRMATION,
        recipient=invoice.customer.email,
        language=invoice.customer.preferred_language,
        # Keyed so re-sending the same invoice cannot spam the customer.
        idempotency_key=f"invoice:{invoice.order.pk_order_id}:{invoice.paid_amt}",
        params={
            "order_number": invoice.order.order_number,
            # Currency is always labelled (§1.1).
            "total": f"{invoice.order.total_amt} {Currency.JOD}",
            "username": invoice.customer.username,
            "invoice_url": (
                f"{settings.app_base_url}/account/orders/"
                f"{invoice.order.order_number}/invoice"
            ),
        },
    )
    log.info(
        "invoice_emailed",
        extra={"order_number": invoice.order.order_number},
    )
