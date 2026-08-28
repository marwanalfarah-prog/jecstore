"""Admin consignment (Part I §7).

The detail screen is the "who holds what, what's sold, what's owed" monitoring
page §7 asks for, with the settlement workflow attached to it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationFailed
from app.core.templating import templates
from app.db.session import get_db
from app.models.catalog import Product, ProductVariant
from app.models.consignment import Consignment, ConsignmentItem, Consignor
from app.models.enums import (
    ConsignmentDirection,
    ConsignmentSplitBasis,
    WriteOffReason,
)
from app.models.identity import User
from app.models.money import MoneyBox
from app.services import approvals, consignment
from app.services import consignment_actions  # noqa: F401 - registers the handlers
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/consignment")


@router.get("")
def arrangement_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("consignment", "view")),
    db: Session = Depends(get_db),
) -> Response:
    arrangements = consignment.list_arrangements(
        db,
        direction=request.query_params.get("direction"),
        open_only=request.query_params.get("open") == "1",
    )
    consignors = {
        c.pk_consignor_id: c
        for c in db.scalars(
            select(Consignor).where(Consignor.scd_active_flag.is_(True))
        ).all()
    }
    positions = {
        a.pk_consignment_id: consignment.settlement_position(db, a)
        for a in arrangements
    }

    return templates.TemplateResponse(
        request,
        "admin/consignment/list.html",
        admin_context(
            db, staff,
            arrangements=arrangements,
            consignors=consignors,
            positions=positions,
            directions=list(ConsignmentDirection),
        ),
    )


@router.get("/new")
def new_arrangement(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("consignment", "create_arrangement")),
    db: Session = Depends(get_db),
) -> Response:
    return templates.TemplateResponse(
        request,
        "admin/consignment/new.html",
        admin_context(
            db, staff,
            consignors=db.scalars(
                select(Consignor).where(Consignor.scd_active_flag.is_(True))
            ).all(),
            directions=list(ConsignmentDirection),
            bases=list(ConsignmentSplitBasis),
        ),
    )


@router.post("/new")
async def create_arrangement(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("consignment", "create_arrangement")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()

    consignor_id = form.get("consignor_id")
    if not consignor_id:
        # Creating the counterparty inline saves a round trip for the common
        # case of a one-off bazaar partner.
        name = str(form.get("new_consignor_name") or "").strip()
        if not name:
            raise ValidationFailed("Choose or name a consignor.")
        consignor_id = consignment.create_consignor(
            db,
            name=name,
            contact_person=str(form.get("contact_person") or "") or None,
            phone_number=str(form.get("phone_number") or "") or None,
            email=str(form.get("email") or "") or None,
        ).pk_consignor_id

    arrangement = consignment.create_arrangement(
        db,
        reference=str(form.get("reference") or "").strip() or _reference(db),
        consignor_id=int(consignor_id),
        direction=str(form["direction"]),
        default_our_share_percentage=_decimal(form.get("default_our_share_percentage"))
        or Decimal("0"),
        split_basis=str(form.get("split_basis") or ConsignmentSplitBasis.DISCOUNTED_PRICE),
        promocodes_eligible=form.get("promocodes_eligible") == "1",
        starts_date=_date(form.get("starts_date")),
        ends_date=_date(form.get("ends_date")),
        note=str(form.get("note") or "") or None,
    )
    db.commit()

    return RedirectResponse(
        f"/admin/consignment/{arrangement.pk_consignment_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/{consignment_id}")
def arrangement_detail(
    request: Request,
    consignment_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("consignment", "view")),
    db: Session = Depends(get_db),
) -> Response:
    arrangement = _get(db, consignment_id)
    return templates.TemplateResponse(
        request,
        "admin/consignment/detail.html",
        admin_context(
            db, staff,
            arrangement=arrangement,
            consignor=db.get(Consignor, arrangement.fk_consignor_id),
            holdings=consignment.holdings(db, arrangement),
            position=consignment.settlement_position(db, arrangement),
            settlements=consignment.settlements_for(db, arrangement),
            variants=_variants(db),
            boxes=_boxes(db),
            reasons=list(WriteOffReason),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/{consignment_id}/items")
def add_items(
    request: Request,
    consignment_id: int,
    variant_id: int = Form(...),
    quantity: int = Form(...),
    agreed_price_amt: str | None = Form(None),
    our_share_percentage: str | None = Form(None),
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("consignment", "create_arrangement")),
    db: Session = Depends(get_db),
) -> Response:
    arrangement = _get(db, consignment_id)
    consignment.place_items(
        db, arrangement,
        variant_id=variant_id,
        quantity=quantity,
        agreed_price_amt=_decimal(agreed_price_amt),
        our_share_percentage=_decimal(our_share_percentage),
        actor_user_id=staff.pk_user_id,
    )
    db.commit()
    return _back(consignment_id, "saved")


@router.post("/{consignment_id}/items/{item_id}/return")
def return_items(
    request: Request,
    consignment_id: int,
    item_id: int,
    quantity: str | None = Form(None),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("consignment", "record_return")),
    db: Session = Depends(get_db),
) -> Response:
    """Return unsold units. A partial quantity is §7's partial recall."""
    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="consignment", action="record_return",
        params={
            "item_id": item_id,
            "quantity": int(quantity) if quantity and str(quantity).strip() else None,
        },
        summary_en=f"Return consigned units (item {item_id})",
        target_table="scd_consignment_item", target_row_id=item_id,
    )
    db.commit()
    return _back(consignment_id, "pending" if result.pending else "saved")


@router.post("/{consignment_id}/items/{item_id}/loss")
def record_loss(
    request: Request,
    consignment_id: int,
    item_id: int,
    quantity: int = Form(...),
    reason: str = Form(WriteOffReason.DAMAGED),
    note: str | None = Form(None),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("inventory", "write_off")),
    db: Session = Depends(get_db),
) -> Response:
    """Damage or loss in custody (§7) — guarded like any other write-off."""
    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="inventory", action="write_off",
        params={
            "consignment_item_id": item_id,
            "quantity": quantity,
            "reason": reason,
            "note": note,
            # The generic write-off handler needs these; the consignment
            # variant resolves the pool from the arrangement instead.
            "variant_id": None,
            "stock_pool_id": None,
        },
        summary_en=f"Consigned units lost or damaged (item {item_id})",
        target_table="scd_consignment_item", target_row_id=item_id,
    )
    db.commit()
    return _back(consignment_id, "pending" if result.pending else "saved")


@router.post("/{consignment_id}/settle")
def settle(
    request: Request,
    consignment_id: int,
    money_box_id: int | None = Form(None),
    note: str | None = Form(None),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("consignment", "settle")),
    db: Session = Depends(get_db),
) -> Response:
    arrangement = _get(db, consignment_id)
    position = consignment.settlement_position(db, arrangement)

    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="consignment", action="settle",
        params={
            "consignment_id": consignment_id,
            "money_box_id": money_box_id,
            "note": note,
        },
        summary_en=(
            f"Settle {arrangement.reference}: "
            f"{'pay' if position.we_owe else 'collect'} "
            f"{abs(position.net_owed_amt)} JOD"
        ),
        target_table="scd_consignment", target_row_id=consignment_id,
    )
    db.commit()
    return _back(consignment_id, "pending" if result.pending else "saved")


@router.post("/{consignment_id}/close")
def close(
    request: Request,
    consignment_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("consignment", "create_arrangement")),
    db: Session = Depends(get_db),
) -> Response:
    arrangement = _get(db, consignment_id)
    consignment.close_arrangement(db, arrangement)
    db.commit()
    return _back(consignment_id, "saved")


# ---------------------------------------------------------------------------


def _get(db: Session, consignment_id: int) -> Consignment:
    arrangement = db.scalars(
        select(Consignment).where(
            Consignment.pk_consignment_id == consignment_id,
            Consignment.scd_active_flag.is_(True),
        )
    ).first()
    if arrangement is None:
        raise NotFound("That consignment arrangement does not exist.")
    return arrangement


def _variants(db: Session, limit: int = 500):
    return [
        (variant, product)
        for variant, product in db.execute(
            select(ProductVariant, Product)
            .join(Product, Product.pk_product_id == ProductVariant.fk_product_id)
            .where(
                ProductVariant.scd_active_flag.is_(True),
                Product.scd_active_flag.is_(True),
            )
            .order_by(Product.name_en)
            .limit(limit)
        ).all()
    ]


def _boxes(db: Session) -> list[MoneyBox]:
    return list(
        db.scalars(
            select(MoneyBox).where(
                MoneyBox.scd_active_flag.is_(True), MoneyBox.is_open_flag.is_(True)
            )
        ).all()
    )


def _reference(db: Session) -> str:
    from app.services.inventory import next_reference

    return next_reference(db, Consignment, "CNS", "reference")


def _decimal(raw) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValidationFailed("That is not a valid number.") from exc


def _date(raw) -> dt.date | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return dt.date.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise ValidationFailed("That is not a valid date.") from exc


def _back(consignment_id: int, flash: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/consignment/{consignment_id}?flash={flash}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
