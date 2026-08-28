"""Barcode generation and label sheets (Part I §11, §5.4).

Two halves of the same requirement:

* **Scanning** an existing barcode resolves to a specific variant (§5.4), so a
  scan at the counter lands staff on the right item and the right stock pool.
* **Printing** labels for stock that has none yet — new items, new variants, or
  an existing item receiving a barcode for the first time — including bulk
  printing for a whole shipment at once (§11).

Barcodes are rendered as inline SVG rather than PNG so a label sheet prints
crisply at any size without a bitmap round-trip, and so the sheet is a plain
HTML page the browser can print directly.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import barcode
from barcode.writer import SVGWriter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.catalog import Product, ProductVariant
from app.models.inventory import ShipmentLine

log = get_logger(__name__)

#: Code128 encodes the full ASCII range and has no fixed length, which suits
#: SKU-shaped values. EAN-13 would need a real GS1 prefix the store does not own.
SYMBOLOGY = "code128"

#: Barcode values are ASCII, uppercase, and free of characters that confuse
#: scanners configured for a keyboard wedge.
_ALLOWED = re.compile(r"[^A-Z0-9\-]")


@dataclass(slots=True)
class Label:
    """One label on a printed sheet."""

    variant_id: int
    barcode_value: str
    svg: str
    product_name_ar: str
    product_name_en: str
    sku: str
    variant_label: str | None = None


def normalize_barcode(value: str) -> str:
    cleaned = _ALLOWED.sub("", (value or "").strip().upper())
    if not cleaned:
        raise ValidationFailed("A barcode needs at least one usable character.")
    return cleaned[:60]


def generate_value(variant: ProductVariant) -> str:
    """A stable barcode for a variant that has none.

    Derived from the SKU where possible so the printed number means something
    to staff reading it aloud; falls back to the variant id, which is unique by
    construction.
    """
    if variant.sku:
        try:
            return normalize_barcode(variant.sku)
        except ValidationFailed:
            pass
    return f"JEC{variant.pk_product_variant_id:08d}"


def render_svg(value: str) -> str:
    """Render one barcode as an inline SVG fragment."""
    buffer = io.BytesIO()
    writer = SVGWriter()
    symbol = barcode.get(SYMBOLOGY, value, writer=writer)
    symbol.write(
        buffer,
        options={
            "module_width": 0.25,
            "module_height": 9.0,
            "font_size": 7,
            "text_distance": 3.0,
            "quiet_zone": 2.0,
        },
    )
    svg = buffer.getvalue().decode("utf-8")

    # Strip the XML prolog and DOCTYPE so the fragment can be embedded directly
    # in a page rather than loaded as a separate document.
    svg = re.sub(r"<\?xml[^>]*\?>\s*", "", svg)
    svg = re.sub(r"<!DOCTYPE[^>]*>\s*", "", svg, flags=re.I)
    return svg.strip()


def assign_barcode(
    db: Session, variant: ProductVariant, value: str | None = None
) -> str:
    """Give a variant a barcode, refusing to duplicate an existing one.

    A duplicate barcode is worse than none: a scan would resolve to the wrong
    item, and the stock movement would land against the wrong variant.
    """
    candidate = normalize_barcode(value) if value else generate_value(variant)

    clash = db.scalars(
        select(ProductVariant).where(
            ProductVariant.barcode == candidate,
            ProductVariant.pk_product_variant_id != variant.pk_product_variant_id,
            ProductVariant.scd_active_flag.is_(True),
        )
    ).first()
    if clash is not None:
        raise ValidationFailed(
            "That barcode is already assigned to another item.",
            details={"barcode": candidate, "variant_id": clash.pk_product_variant_id},
        )

    variant.barcode = candidate
    log.info(
        "barcode_assigned",
        extra={"variant": variant.pk_product_variant_id, "barcode": candidate},
    )
    return candidate


def resolve_scan(db: Session, scanned: str) -> ProductVariant:
    """Turn a scanned code into a variant (§5.4).

    Falls back to SKU, because staff frequently type the SKU when a label is
    damaged, and refusing that would send them to a dead end.
    """
    value = normalize_barcode(scanned)

    variant = db.scalars(
        select(ProductVariant).where(
            ProductVariant.barcode == value,
            ProductVariant.scd_active_flag.is_(True),
        )
    ).first()
    if variant is not None:
        return variant

    variant = db.scalars(
        select(ProductVariant).where(
            ProductVariant.sku == value,
            ProductVariant.scd_active_flag.is_(True),
        )
    ).first()
    if variant is None:
        raise NotFound("No item matches that barcode.")
    return variant


def labels_for_variants(
    db: Session, variant_ids: list[int], *, copies: int = 1, assign_missing: bool = True
) -> list[Label]:
    """Build a label sheet, assigning barcodes to anything lacking one."""
    if not variant_ids:
        return []
    copies = max(1, min(copies, 50))

    variants = db.scalars(
        select(ProductVariant).where(
            ProductVariant.pk_product_variant_id.in_(variant_ids),
            ProductVariant.scd_active_flag.is_(True),
        )
    ).all()
    products = {
        p.pk_product_id: p
        for p in db.scalars(
            select(Product).where(
                Product.pk_product_id.in_([v.fk_product_id for v in variants] or [0])
            )
        ).all()
    }

    labels: list[Label] = []
    for variant in variants:
        value = variant.barcode
        if not value and assign_missing:
            value = assign_barcode(db, variant)
        if not value:
            continue

        product = products.get(variant.fk_product_id)
        label = Label(
            variant_id=variant.pk_product_variant_id,
            barcode_value=value,
            svg=render_svg(value),
            product_name_ar=(product.name_ar if product else ""),
            product_name_en=(product.name_en if product else ""),
            sku=variant.sku or "",
        )
        labels.extend([label] * copies)

    log.info("labels_generated", extra={"count": len(labels)})
    return labels


def labels_for_shipment(db: Session, shipment_id: int) -> list[Label]:
    """Bulk labels for a whole shipment — one per unit received (§11).

    This is the case §11 names explicitly: a delivery arrives, and every unit
    needs a label before it reaches the shelf.
    """
    lines = db.scalars(
        select(ShipmentLine).where(
            ShipmentLine.fk_shipment_id == shipment_id,
            ShipmentLine.scd_active_flag.is_(True),
        )
    ).all()
    if not lines:
        return []

    labels: list[Label] = []
    for line in lines:
        labels.extend(
            labels_for_variants(
                db, [line.fk_product_variant_id], copies=min(line.quantity, 50)
            )
        )
    return labels
