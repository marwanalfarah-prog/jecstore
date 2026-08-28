"""Shipping cost resolution (Part I §2.2, §8).

Rules are keyed by destination, most specific first: a rule for the exact
governorate beats a country-wide fallback, which beats a global default. Ties
break on the higher ``priority``.

The third outcome matters as much as a number. Part I §2.2 describes shipping as
"by Jordanian governorate, **or** 'not included, will be contacted'" — so an
address outside the configured rules does not block the order and does not guess
a price. It places the order with shipping unpriced and flags it for staff to
quote, which is what ``shipping_quote_pending_flag`` on the order records.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import FulfillmentMethod
from app.models.orders import ShippingRule
from app.services.pricing import q


@dataclass(slots=True)
class ShippingQuote:
    """What shipping costs, or why it has no price yet."""

    amount_amt: Decimal
    #: True when the destination falls outside every configured rule, or the
    #: matched rule says to quote on contact (Part I §2.2).
    quote_on_contact: bool
    rule: ShippingRule | None = None
    #: Set when a free-shipping threshold was met, so the cart can say so.
    free_threshold_met: bool = False

    @property
    def is_free(self) -> bool:
        return self.amount_amt == 0 and not self.quote_on_contact


#: Pickup is never charged — the customer collects it themselves.
PICKUP_QUOTE = ShippingQuote(amount_amt=Decimal("0"), quote_on_contact=False)


def resolve(
    db: Session,
    *,
    method: str,
    subtotal_amt: Decimal,
    country_id: int | None = None,
    province_id: int | None = None,
) -> ShippingQuote:
    """Resolve shipping for one destination and basket subtotal."""
    if method == FulfillmentMethod.PICKUP:
        return PICKUP_QUOTE

    rule = _best_rule(db, country_id=country_id, province_id=province_id)

    if rule is None:
        # No rule covers this destination — staff will contact the customer.
        return ShippingQuote(amount_amt=Decimal("0"), quote_on_contact=True)

    if rule.quote_on_contact_flag:
        return ShippingQuote(amount_amt=Decimal("0"), quote_on_contact=True, rule=rule)

    if rule.free_above_amt is not None and subtotal_amt >= rule.free_above_amt:
        return ShippingQuote(
            amount_amt=Decimal("0"),
            quote_on_contact=False,
            rule=rule,
            free_threshold_met=True,
        )

    return ShippingQuote(amount_amt=q(rule.cost_amt), quote_on_contact=False, rule=rule)


def _best_rule(
    db: Session, *, country_id: int | None, province_id: int | None
) -> ShippingRule | None:
    """Most specific match wins: governorate → country → global."""
    rules = db.scalars(
        select(ShippingRule).where(ShippingRule.scd_active_flag.is_(True))
    ).all()

    def specificity(rule: ShippingRule) -> int | None:
        """Higher is more specific; ``None`` means the rule does not apply."""
        if rule.fk_province_id is not None:
            return 2 if rule.fk_province_id == province_id else None
        if rule.fk_country_id is not None:
            return 1 if rule.fk_country_id == country_id else None
        return 0  # global fallback

    scored = [
        (score, rule.priority, rule)
        for rule in rules
        if (score := specificity(rule)) is not None
    ]
    if not scored:
        return None

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]
