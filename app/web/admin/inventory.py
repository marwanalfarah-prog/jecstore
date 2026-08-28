"""Admin inventory: stock, shipments, transfers, stock takes, labels (Part I §11)."""

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
from app.models.enums import Currency, WriteOffReason
from app.models.identity import User
from app.models.inventory import (
    Shipment,
    ShipmentLine,
    StockPool,
    StockTake,
    StockTransfer,
)
from app.services import approvals, barcodes, inventory
from app.services import inventory_actions  # noqa: F401 - registers the handlers
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/inventory")


# ---------------------------------------------------------------------------
# Stock positions
# ---------------------------------------------------------------------------


@router.get("")
def stock_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("inventory", "view")),
    db: Session = Depends(get_db),
) -> Response:
    only_low = request.query_params.get("filter") == "low"
    # The service caps this read; the screen has to say so, or 200 rows read as
    # "that is the whole catalog".
    row_limit = 200
    rows = inventory.stock_positions(
        db, only_low=only_low, query=request.query_params.get("q"), limit=row_limit
    )
    return templates.TemplateResponse(
        request,
        "admin/inventory/list.html",
        admin_context(
            db, staff,
            rows=rows,
            row_limit=row_limit,
            only_low=only_low,
            low_count=len([r for r in rows if r.is_low]) if not only_low else len(rows),
        ),
    )


@router.get("/item/{variant_id}")
def item_detail(
    request: Request,
    variant_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("inventory", "view")),
    db: Session = Depends(get_db),
) -> Response:
    """Per-item dashboard: every in/out movement, plus its invoice history (§11)."""
    variant = db.get(ProductVariant, variant_id)
    if variant is None:
        raise NotFound("That item does not exist.")

    shipment_lines = inventory.shipments_for_variant(db, variant_id)
    shipments = {
        s.pk_shipment_id: s
        for s in db.scalars(
            select(Shipment).where(
                Shipment.pk_shipment_id.in_(
                    [l.fk_shipment_id for l in shipment_lines] or [0]
                )
            )
        ).all()
    }

    return templates.TemplateResponse(
        request,
        "admin/inventory/item.html",
        admin_context(
            db, staff,
            variant=variant,
            product=db.get(Product, variant.fk_product_id),
            movements=inventory.movement_history(db, variant_id),
            shipment_lines=shipment_lines,
            shipments=shipments,
            pools=_pools(db),
            reasons=list(WriteOffReason),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/item/{variant_id}/write-off")
def write_off(
    request: Request,
    variant_id: int,
    stock_pool_id: int = Form(...),
    quantity: int = Form(...),
    reason: str = Form(...),
    note: str | None = Form(None),
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("inventory", "write_off")),
    db: Session = Depends(get_db),
) -> Response:
    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="inventory", action="write_off",
        params={
            "variant_id": variant_id, "stock_pool_id": stock_pool_id,
            "quantity": quantity, "reason": reason, "note": note,
        },
        summary_en=f"Write off {quantity} units ({reason})",
        target_table="scd_product_variant", target_row_id=variant_id,
    )
    db.commit()
    return RedirectResponse(
        f"/admin/inventory/item/{variant_id}?flash="
        f"{'pending' if result.pending else 'saved'}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Shipments
# ---------------------------------------------------------------------------


@router.get("/shipments")
def shipment_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("inventory", "view")),
    db: Session = Depends(get_db),
) -> Response:
    rows = db.scalars(
        select(Shipment)
        .where(Shipment.scd_active_flag.is_(True))
        .order_by(Shipment.pk_shipment_id.desc())
        .limit(100)
    ).all()
    return templates.TemplateResponse(
        request, "admin/inventory/shipments.html",
        admin_context(db, staff, shipments=rows),
    )


@router.get("/shipments/new")
def new_shipment(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("inventory", "receive_shipment")),
    db: Session = Depends(get_db),
) -> Response:
    return templates.TemplateResponse(
        request, "admin/inventory/shipment_new.html",
        admin_context(
            db, staff,
            variants=_variants(db),
            pools=_pools(db),
            currencies=[Currency.JOD, Currency.USD],
            suggested_reference=inventory.next_reference(db, Shipment, "SHP", "reference"),
        ),
    )


@router.post("/shipments/new")
async def create_shipment(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("inventory", "receive_shipment")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()

    # Line fields arrive as parallel arrays: variant_id[], quantity[], unit_cost[].
    variant_ids = form.getlist("variant_id")
    quantities = form.getlist("quantity")
    unit_costs = form.getlist("unit_cost")
    line_pools = form.getlist("line_pool_id")

    lines: list[inventory.ShipmentLineInput] = []
    for index, raw_variant in enumerate(variant_ids):
        if not str(raw_variant).strip():
            continue
        quantity = int(quantities[index] or 0)
        if quantity <= 0:
            continue
        pool_raw = line_pools[index] if index < len(line_pools) else ""
        lines.append(
            inventory.ShipmentLineInput(
                variant_id=int(raw_variant),
                quantity=quantity,
                unit_cost_amt=_decimal(unit_costs[index]) or Decimal("0"),
                stock_pool_id=int(pool_raw) if str(pool_raw).strip() else None,
            )
        )

    if not lines:
        raise ValidationFailed("Add at least one item to the shipment.")

    shipment = inventory.create_shipment(
        db,
        reference=str(form.get("reference") or "").strip()
        or inventory.next_reference(db, Shipment, "SHP", "reference"),
        lines=lines,
        supplier_name=str(form.get("supplier_name") or "") or None,
        invoice_date=_date(form.get("invoice_date")),
        currency=str(form.get("currency") or Currency.JOD),
        usd_rate_used=_decimal(form.get("usd_rate_used")),
        stock_pool_id=int(form["stock_pool_id"]) if form.get("stock_pool_id") else None,
        invoice_file_path=str(form.get("invoice_file_path") or "") or None,
        no_invoice_available=form.get("no_invoice_available") == "1",
        shipping_cost_amt=_decimal(form.get("shipping_cost_amt")) or Decimal("0"),
        customs_cost_amt=_decimal(form.get("customs_cost_amt")) or Decimal("0"),
        note=str(form.get("note") or "") or None,
        actor_user_id=staff.pk_user_id,
    )
    db.commit()

    return RedirectResponse(
        f"/admin/inventory/shipments/{shipment.pk_shipment_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/shipments/{shipment_id}")
def shipment_detail(
    request: Request,
    shipment_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("inventory", "view")),
    db: Session = Depends(get_db),
) -> Response:
    shipment = db.get(Shipment, shipment_id)
    if shipment is None:
        raise NotFound("That shipment does not exist.")

    lines = db.scalars(
        select(ShipmentLine).where(
            ShipmentLine.fk_shipment_id == shipment_id,
            ShipmentLine.scd_active_flag.is_(True),
        )
    ).all()
    return templates.TemplateResponse(
        request, "admin/inventory/shipment_detail.html",
        admin_context(
            db, staff,
            shipment=shipment,
            lines=lines,
            variants=_variant_map(db, [l.fk_product_variant_id for l in lines]),
        ),
    )


# ---------------------------------------------------------------------------
# Stock takes
# ---------------------------------------------------------------------------


@router.get("/stock-takes")
def stock_take_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("inventory", "stock_take")),
    db: Session = Depends(get_db),
) -> Response:
    rows = db.scalars(
        select(StockTake)
        .where(StockTake.scd_active_flag.is_(True))
        .order_by(StockTake.pk_stock_take_id.desc())
        .limit(50)
    ).all()
    return templates.TemplateResponse(
        request, "admin/inventory/stock_takes.html",
        admin_context(
            db, staff, stock_takes=rows, pools=_pools(db),
            suggested_reference=inventory.next_reference(db, StockTake, "STK", "reference"),
        ),
    )


@router.post("/stock-takes/new")
def open_stock_take(
    request: Request,
    stock_pool_id: int = Form(...),
    reference: str | None = Form(None),
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("inventory", "stock_take")),
    db: Session = Depends(get_db),
) -> Response:
    stock_take = inventory.open_stock_take(
        db,
        reference=(reference or "").strip()
        or inventory.next_reference(db, StockTake, "STK", "reference"),
        stock_pool_id=stock_pool_id,
    )
    db.commit()
    return RedirectResponse(
        f"/admin/inventory/stock-takes/{stock_take.pk_stock_take_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/stock-takes/{stock_take_id}")
def stock_take_detail(
    request: Request,
    stock_take_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("inventory", "stock_take")),
    db: Session = Depends(get_db),
) -> Response:
    stock_take = db.get(StockTake, stock_take_id)
    if stock_take is None:
        raise NotFound("That stock take does not exist.")

    return templates.TemplateResponse(
        request, "admin/inventory/stock_take_detail.html",
        admin_context(
            db, staff,
            stock_take=stock_take,
            report=inventory.variance_report(db, stock_take),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/stock-takes/{stock_take_id}/count")
async def record_count(
    request: Request,
    stock_take_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("inventory", "stock_take")),
    db: Session = Depends(get_db),
) -> Response:
    stock_take = db.get(StockTake, stock_take_id)
    if stock_take is None:
        raise NotFound("That stock take does not exist.")

    form = await request.form()
    counts = {
        int(key.removeprefix("count_")): int(value)
        for key, value in form.items()
        if key.startswith("count_") and str(value).strip().lstrip("-").isdigit()
    }
    inventory.record_count(db, stock_take, counts)
    db.commit()
    return RedirectResponse(
        f"/admin/inventory/stock-takes/{stock_take_id}?flash=saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/stock-takes/{stock_take_id}/close")
def close_stock_take(
    request: Request,
    stock_take_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("inventory", "adjust_stock")),
    db: Session = Depends(get_db),
) -> Response:
    """Closing writes the adjustment movements, so it is a guarded action."""
    result = approvals.execute_or_submit(
        db, request, decision, staff,
        module="inventory", action="adjust_stock",
        params={"stock_take_id": stock_take_id},
        summary_en=f"Close stock take {stock_take_id} and post variances",
        target_table="scd_stock_take", target_row_id=stock_take_id,
    )
    db.commit()
    return RedirectResponse(
        f"/admin/inventory/stock-takes/{stock_take_id}?flash="
        f"{'pending' if result.pending else 'saved'}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------


@router.get("/transfers")
def transfer_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(
        require_permission("inventory", "transfer_between_branches")
    ),
    db: Session = Depends(get_db),
) -> Response:
    rows = db.scalars(
        select(StockTransfer)
        .where(StockTransfer.scd_active_flag.is_(True))
        .order_by(StockTransfer.pk_stock_transfer_id.desc())
        .limit(50)
    ).all()
    return templates.TemplateResponse(
        request, "admin/inventory/transfers.html",
        admin_context(
            db, staff, transfers=rows, pools=_pools(db), variants=_variants(db),
            suggested_reference=inventory.next_reference(
                db, StockTransfer, "TRF", "reference"
            ),
        ),
    )


@router.post("/transfers/new")
async def create_transfer(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(
        require_permission("inventory", "transfer_between_branches")
    ),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    variant_ids = form.getlist("variant_id")
    quantities = form.getlist("quantity")

    lines = [
        (int(variant_id), int(quantities[index]))
        for index, variant_id in enumerate(variant_ids)
        if str(variant_id).strip()
        and index < len(quantities)
        and str(quantities[index]).strip().isdigit()
        and int(quantities[index]) > 0
    ]
    if not lines:
        raise ValidationFailed("Add at least one item to transfer.")

    transfer = inventory.create_transfer(
        db,
        reference=str(form.get("reference") or "").strip()
        or inventory.next_reference(db, StockTransfer, "TRF", "reference"),
        from_pool_id=int(form["from_pool_id"]),
        to_pool_id=int(form["to_pool_id"]),
        lines=lines,
        note=str(form.get("note") or "") or None,
        actor_user_id=staff.pk_user_id,
    )
    db.commit()
    return RedirectResponse(
        f"/admin/inventory/transfers?highlight={transfer.pk_stock_transfer_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/transfers/{transfer_id}/receive")
def receive_transfer(
    request: Request,
    transfer_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(
        require_permission("inventory", "transfer_between_branches")
    ),
    db: Session = Depends(get_db),
) -> Response:
    transfer = db.get(StockTransfer, transfer_id)
    if transfer is None:
        raise NotFound("That transfer does not exist.")
    inventory.receive_transfer(db, transfer, actor_user_id=staff.pk_user_id)
    db.commit()
    return RedirectResponse(
        "/admin/inventory/transfers", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Barcode labels (Part I §11)
# ---------------------------------------------------------------------------


@router.get("/labels")
def label_sheet(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("inventory", "print_barcodes")),
    db: Session = Depends(get_db),
) -> Response:
    """A printable sheet. ``?shipment=`` prints a whole delivery at once."""
    shipment_id = request.query_params.get("shipment")
    variant_param = request.query_params.get("variants", "")
    copies = int(request.query_params.get("copies", 1) or 1)

    if shipment_id:
        labels = barcodes.labels_for_shipment(db, int(shipment_id))
    else:
        variant_ids = [
            int(part) for part in variant_param.split(",") if part.strip().isdigit()
        ]
        labels = barcodes.labels_for_variants(db, variant_ids, copies=copies)

    db.commit()  # persists any barcodes assigned on the fly
    return templates.TemplateResponse(
        request, "admin/inventory/labels.html",
        admin_context(db, staff, labels=labels),
    )


@router.get("/scan")
def scan(
    request: Request,
    code: str,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("inventory", "view")),
    db: Session = Depends(get_db),
) -> Response:
    """Resolve a scanned barcode straight to its item (§5.4)."""
    variant = barcodes.resolve_scan(db, code)
    return RedirectResponse(
        f"/admin/inventory/item/{variant.pk_product_variant_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pools(db: Session) -> list[StockPool]:
    return list(
        db.scalars(
            select(StockPool)
            .where(StockPool.scd_active_flag.is_(True))
            .order_by(StockPool.pk_stock_pool_id)
        ).all()
    )


def _variants(db: Session, limit: int = 500) -> list[tuple[ProductVariant, Product]]:
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


def _variant_map(db: Session, variant_ids: list[int]) -> dict[int, ProductVariant]:
    return {
        v.pk_product_variant_id: v
        for v in db.scalars(
            select(ProductVariant).where(
                ProductVariant.pk_product_variant_id.in_(variant_ids or [0])
            )
        ).all()
    }


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
