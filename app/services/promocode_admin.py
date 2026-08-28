"""Admin promocode operations.

Runtime validation lives in app.services.promocodes. This module is the write
side for admin CRUD: it validates the code definition, stores codes upper-case,
and replaces restriction sets by closing old rows and inserting new ones.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.db.base import utcnow
from app.models.catalog import Category, Product
from app.models.enums import PromocodeKind
from app.models.marketing import Promocode, PromocodeRestriction
from app.services.pricing import q
from app.services.promocodes import normalize_code, redemption_count


@dataclass(frozen=True, slots=True)
class RestrictionSpec:
    target_type: str
    target_id: int
    is_exclusion: bool = False


def active_promocodes(db: Session) -> list[Promocode]:
    return list(
        db.scalars(
            select(Promocode)
            .where(Promocode.scd_active_flag.is_(True))
            .order_by(Promocode.pk_promocode_id.desc())
        ).all()
    )


def active_restrictions(db: Session, promocode_id: int) -> list[PromocodeRestriction]:
    return list(
        db.scalars(
            select(PromocodeRestriction)
            .where(
                PromocodeRestriction.fk_promocode_id == promocode_id,
                PromocodeRestriction.scd_active_flag.is_(True),
            )
            .order_by(PromocodeRestriction.pk_promocode_restriction_id)
        ).all()
    )


def redemption_counts(db: Session, promocodes: list[Promocode]) -> dict[int, int]:
    return {
        promocode.pk_promocode_id: redemption_count(db, promocode.pk_promocode_id)
        for promocode in promocodes
    }


def create_promocode(
    db: Session,
    *,
    code: str,
    promocode_kind: str,
    name_ar: str | None = None,
    name_en: str | None = None,
    percentage: Decimal | None = None,
    max_discount_amt: Decimal | None = None,
    fixed_amount_amt: Decimal | None = None,
    minimum_order_amt: Decimal | None = None,
    starts_dt: dt.datetime | None = None,
    expires_dt: dt.datetime | None = None,
    single_use_globally: bool = False,
    max_total_uses: int | None = None,
    max_uses_per_customer: int | None = None,
    stacks_with_item_discount: bool = False,
    applies_to_consigned: bool = False,
    note: str | None = None,
    restrictions: list[RestrictionSpec] | None = None,
    actor_user_id: int | None = None,
) -> Promocode:
    code = _valid_code(db, code)
    _validate_kind(
        promocode_kind,
        percentage=percentage,
        max_discount_amt=max_discount_amt,
        fixed_amount_amt=fixed_amount_amt,
    )
    _validate_limits(
        max_total_uses=max_total_uses,
        max_uses_per_customer=max_uses_per_customer,
        starts_dt=starts_dt,
        expires_dt=expires_dt,
    )
    now = utcnow()
    promocode = Promocode(
        code=code,
        name_ar=_blank(name_ar),
        name_en=_blank(name_en),
        promocode_kind=promocode_kind,
        percentage=q(Decimal(percentage)) if percentage is not None else None,
        max_discount_amt=q(Decimal(max_discount_amt)) if max_discount_amt is not None else None,
        fixed_amount_amt=q(Decimal(fixed_amount_amt)) if fixed_amount_amt is not None else None,
        minimum_order_amt=q(Decimal(minimum_order_amt)) if minimum_order_amt is not None else None,
        starts_dt=starts_dt,
        expires_dt=expires_dt,
        single_use_globally_flag=single_use_globally,
        max_total_uses=max_total_uses,
        max_uses_per_customer=max_uses_per_customer,
        stacks_with_item_discount_flag=stacks_with_item_discount,
        applies_to_consigned_flag=applies_to_consigned,
        note=_blank(note),
        scd_active_from=now,
        scd_changed_by=actor_user_id,
    )
    db.add(promocode)
    db.flush()
    replace_restrictions(
        db,
        promocode_id=promocode.pk_promocode_id,
        restrictions=restrictions or [],
        actor_user_id=actor_user_id,
    )
    db.flush()
    return promocode


def update_promocode(
    db: Session,
    *,
    promocode_id: int,
    code: str,
    promocode_kind: str,
    name_ar: str | None = None,
    name_en: str | None = None,
    percentage: Decimal | None = None,
    max_discount_amt: Decimal | None = None,
    fixed_amount_amt: Decimal | None = None,
    minimum_order_amt: Decimal | None = None,
    starts_dt: dt.datetime | None = None,
    expires_dt: dt.datetime | None = None,
    single_use_globally: bool = False,
    max_total_uses: int | None = None,
    max_uses_per_customer: int | None = None,
    stacks_with_item_discount: bool = False,
    applies_to_consigned: bool = False,
    note: str | None = None,
    restrictions: list[RestrictionSpec] | None = None,
    actor_user_id: int | None = None,
) -> Promocode:
    promocode = get_active(db, promocode_id)
    code = _valid_code(db, code, exclude_id=promocode_id)
    _validate_kind(
        promocode_kind,
        percentage=percentage,
        max_discount_amt=max_discount_amt,
        fixed_amount_amt=fixed_amount_amt,
    )
    _validate_limits(
        max_total_uses=max_total_uses,
        max_uses_per_customer=max_uses_per_customer,
        starts_dt=starts_dt,
        expires_dt=expires_dt,
    )
    promocode.code = code
    promocode.name_ar = _blank(name_ar)
    promocode.name_en = _blank(name_en)
    promocode.promocode_kind = promocode_kind
    promocode.percentage = q(Decimal(percentage)) if percentage is not None else None
    promocode.max_discount_amt = (
        q(Decimal(max_discount_amt)) if max_discount_amt is not None else None
    )
    promocode.fixed_amount_amt = (
        q(Decimal(fixed_amount_amt)) if fixed_amount_amt is not None else None
    )
    promocode.minimum_order_amt = (
        q(Decimal(minimum_order_amt)) if minimum_order_amt is not None else None
    )
    promocode.starts_dt = starts_dt
    promocode.expires_dt = expires_dt
    promocode.single_use_globally_flag = single_use_globally
    promocode.max_total_uses = max_total_uses
    promocode.max_uses_per_customer = max_uses_per_customer
    promocode.stacks_with_item_discount_flag = stacks_with_item_discount
    promocode.applies_to_consigned_flag = applies_to_consigned
    promocode.note = _blank(note)
    promocode.scd_changed_by = actor_user_id
    replace_restrictions(
        db,
        promocode_id=promocode.pk_promocode_id,
        restrictions=restrictions or [],
        actor_user_id=actor_user_id,
    )
    db.flush()
    return promocode


def close_promocode(
    db: Session,
    *,
    promocode_id: int,
    actor_user_id: int | None = None,
) -> Promocode:
    promocode = get_active(db, promocode_id)
    for restriction in active_restrictions(db, promocode_id):
        restriction.close(changed_by=actor_user_id)
    promocode.close(changed_by=actor_user_id)
    db.flush()
    return promocode


def replace_restrictions(
    db: Session,
    *,
    promocode_id: int,
    restrictions: list[RestrictionSpec],
    actor_user_id: int | None = None,
) -> None:
    get_active(db, promocode_id)
    for existing in active_restrictions(db, promocode_id):
        existing.close(changed_by=actor_user_id)

    now = utcnow()
    seen: set[tuple[str, int, bool]] = set()
    for spec in restrictions:
        if spec.target_type not in {"product", "category"}:
            raise ValidationFailed("Restriction target must be a product or category.")
        if spec.target_type == "product":
            product = db.get(Product, spec.target_id)
            if product is None or not product.scd_active_flag:
                raise NotFound("That restricted product does not exist.")
            key = ("product", product.pk_product_id, spec.is_exclusion)
            fk_product_id = product.pk_product_id
            fk_category_id = None
        else:
            category = db.get(Category, spec.target_id)
            if category is None or not category.scd_active_flag:
                raise NotFound("That restricted category does not exist.")
            key = ("category", category.pk_category_id, spec.is_exclusion)
            fk_product_id = None
            fk_category_id = category.pk_category_id
        if key in seen:
            continue
        seen.add(key)
        db.add(
            PromocodeRestriction(
                fk_promocode_id=promocode_id,
                fk_product_id=fk_product_id,
                fk_category_id=fk_category_id,
                is_exclusion_flag=spec.is_exclusion,
                scd_active_from=now,
                scd_changed_by=actor_user_id,
            )
        )


def get_active(db: Session, promocode_id: int) -> Promocode:
    promocode = db.get(Promocode, promocode_id)
    if promocode is None or not promocode.scd_active_flag:
        raise NotFound("That promocode does not exist.")
    return promocode


def _valid_code(
    db: Session,
    raw_code: str,
    *,
    exclude_id: int | None = None,
) -> str:
    code = normalize_code(raw_code)
    if not code:
        raise ValidationFailed("Promocode code is required.")
    stmt = select(Promocode).where(
        Promocode.code == code,
        Promocode.scd_active_flag.is_(True),
    )
    if exclude_id is not None:
        stmt = stmt.where(Promocode.pk_promocode_id != exclude_id)
    if db.scalars(stmt).first() is not None:
        raise Conflict("That promocode code is already active.")
    return code


def _validate_kind(
    promocode_kind: str,
    *,
    percentage: Decimal | None,
    max_discount_amt: Decimal | None,
    fixed_amount_amt: Decimal | None,
) -> None:
    if promocode_kind not in {item.value for item in PromocodeKind}:
        raise ValidationFailed("Choose a promocode type.")
    if promocode_kind in {
        PromocodeKind.PERCENTAGE.value,
        PromocodeKind.PERCENTAGE_CAPPED.value,
    }:
        if percentage is None or Decimal(percentage) <= 0 or Decimal(percentage) > 100:
            raise ValidationFailed("Percentage promocodes must be between 0 and 100.")
    if promocode_kind == PromocodeKind.PERCENTAGE_CAPPED.value and (
        max_discount_amt is None or Decimal(max_discount_amt) <= 0
    ):
        raise ValidationFailed("Capped percentage promocodes need a cap.")
    if promocode_kind == PromocodeKind.FIXED_AMOUNT.value and (
        fixed_amount_amt is None or Decimal(fixed_amount_amt) <= 0
    ):
        raise ValidationFailed("Fixed amount promocodes need an amount.")


def _validate_limits(
    *,
    max_total_uses: int | None,
    max_uses_per_customer: int | None,
    starts_dt: dt.datetime | None,
    expires_dt: dt.datetime | None,
) -> None:
    if max_total_uses is not None and max_total_uses <= 0:
        raise ValidationFailed("Total use limit must be positive.")
    if max_uses_per_customer is not None and max_uses_per_customer <= 0:
        raise ValidationFailed("Per-customer use limit must be positive.")
    if starts_dt and expires_dt and expires_dt <= starts_dt:
        raise ValidationFailed("Expiry must be after the start.")


def _blank(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None
