"""Image upload endpoints (Part I §5.2, §5.4, §4, §5.6).

One route per target rather than a generic "upload anywhere" endpoint: each
knows which permission guards it and which record it attaches to, so an upload
cannot be pointed at something the uploader may not edit.

§17.4 calls a full photography pass a launch blocker, so these are the screens
that pass will run through.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import get_db
from app.models.catalog import Category, Product, ProductImage, ProductVariant, Publisher
from app.models.identity import User
from app.models.marketing import HomepageSection
from app.services import media
from app.services.permissions import GrantDecision
from app.web.admin.deps import current_staff, require_permission

log = get_logger(__name__)
router = APIRouter(prefix="/uploads")


def _redirect(target: str, flash: str = "saved") -> RedirectResponse:
    separator = "&" if "?" in target else "?"
    return RedirectResponse(
        f"{target}{separator}flash={flash}", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Products (Part I §5.2, §5.4)
# ---------------------------------------------------------------------------


@router.post("/products/{product_id}/main")
def product_main_image(
    request: Request,
    product_id: int,
    image: UploadFile = File(...),
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    """The one image used for listings and thumbnails (§5.2)."""
    product = db.get(Product, product_id)
    if product is None or not product.scd_active_flag:
        raise NotFound("That product does not exist.")

    stored = media.store(image, folder="products")
    previous = product.main_image_path
    product.main_image_path = stored.path
    product.scd_changed_by = staff.pk_user_id

    _reindex(db, product)
    db.commit()

    # Reclaim the replaced file. The product row itself is versioned, so the
    # history of *which* image was used survives in the audit log.
    if previous and previous != stored.path:
        media.delete(previous)

    return _redirect(f"/admin/products/{product_id}")


@router.post("/products/{product_id}/gallery")
async def product_gallery(
    request: Request,
    product_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    """Add to the unlimited internal/gallery images (§5.2).

    Accepts several files at once, and may attach them to a specific variant —
    §5.4 allows an optional gallery per variant on top of the product's own.
    """
    form = await request.form()
    # Check against Starlette's UploadFile, not FastAPI's: the form parser
    # produces the former, and fastapi.UploadFile is a *subclass* of it — so
    # isinstance against FastAPI's class silently matches nothing.
    uploads = [
        f for f in form.getlist("images")
        if isinstance(f, StarletteUploadFile) and f.filename
    ]
    if not uploads:
        raise ValidationFailed("Choose at least one image.")

    product = db.get(Product, product_id)
    if product is None or not product.scd_active_flag:
        raise NotFound("That product does not exist.")

    variant_id = form.get("variant_id")
    variant_id = int(variant_id) if variant_id and str(variant_id).strip() else None
    if variant_id is not None:
        variant = db.get(ProductVariant, variant_id)
        if variant is None or variant.fk_product_id != product_id:
            raise ValidationFailed("That variant does not belong to this product.")

    now = utcnow()
    next_order = db.scalar(
        select(ProductImage.sort_order)
        .where(
            ProductImage.fk_product_id == product_id,
            ProductImage.scd_active_flag.is_(True),
        )
        .order_by(ProductImage.sort_order.desc())
        .limit(1)
    )
    position = (next_order or 0) + 1

    for index, upload in enumerate(uploads):
        stored = media.store(upload, folder="products")
        db.add(
            ProductImage(
                fk_product_id=product_id,
                fk_product_variant_id=variant_id,
                image_path=stored.path,
                # Alt text is bilingual and indexed; seeded from the product
                # name so an image is never announced as nothing at all.
                alt_text_ar=product.name_ar,
                alt_text_en=product.name_en,
                sort_order=position + index,
                scd_active_from=now,
                scd_changed_by=staff.pk_user_id,
            )
        )

    db.commit()
    return _redirect(f"/admin/products/{product_id}")


@router.post("/products/images/{image_id}/remove")
def remove_product_image(
    request: Request,
    image_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    """Close the image row. The record is never deleted (Part II §6)."""
    image = db.get(ProductImage, image_id)
    if image is None or not image.scd_active_flag:
        raise NotFound("That image does not exist.")

    product_id = image.fk_product_id
    image.close(changed_by=staff.pk_user_id)
    db.commit()

    return _redirect(f"/admin/products/{product_id}")


@router.post("/products/images/{image_id}/alt")
def set_alt_text(
    request: Request,
    image_id: int,
    alt_text_ar: str = Form(""),
    alt_text_en: str = Form(""),
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    """Alt text, in both languages (§1, §17.7).

    It is read aloud and indexed, so it cannot be one language for both
    audiences.
    """
    image = db.get(ProductImage, image_id)
    if image is None or not image.scd_active_flag:
        raise NotFound("That image does not exist.")

    image.alt_text_ar = alt_text_ar.strip() or None
    image.alt_text_en = alt_text_en.strip() or None
    image.scd_changed_by = staff.pk_user_id
    db.commit()

    return _redirect(f"/admin/products/{image.fk_product_id}")


# ---------------------------------------------------------------------------
# Categories, publishers, banners
# ---------------------------------------------------------------------------


@router.post("/categories/{category_id}/image")
def category_image(
    request: Request,
    category_id: int,
    image: UploadFile = File(...),
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "create_category")),
    db: Session = Depends(get_db),
) -> Response:
    category = db.get(Category, category_id)
    if category is None or not category.scd_active_flag:
        raise NotFound("That category does not exist.")

    stored = media.store(image, folder="categories")
    previous = category.image_path
    category.image_path = stored.path
    category.scd_changed_by = staff.pk_user_id
    db.commit()

    if previous and previous != stored.path:
        media.delete(previous)
    return _redirect("/admin/categories")


@router.post("/publishers/{publisher_id}/logo")
def publisher_logo(
    request: Request,
    publisher_id: int,
    image: UploadFile = File(...),
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "manage_publishers")),
    db: Session = Depends(get_db),
) -> Response:
    """Publisher logo for the homepage strip and landing page (§5.6, §4)."""
    publisher = db.get(Publisher, publisher_id)
    if publisher is None or not publisher.scd_active_flag:
        raise NotFound("That publisher does not exist.")

    stored = media.store(image, folder="publishers")
    previous = publisher.logo_path
    publisher.logo_path = stored.path
    publisher.scd_changed_by = staff.pk_user_id
    db.commit()

    if previous and previous != stored.path:
        media.delete(previous)
    return _redirect("/admin/products")


@router.post("/homepage/{section_id}/banner")
def homepage_banner(
    request: Request,
    section_id: int,
    language: str = Form("ar"),
    image: UploadFile = File(...),
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_homepage")),
    db: Session = Depends(get_db),
) -> Response:
    """Banner artwork, per language (§4).

    Separate images per language because banners routinely carry baked-in text
    — one artwork cannot serve both audiences.
    """
    if language not in {"ar", "en"}:
        raise ValidationFailed("Choose Arabic or English.")

    section = db.get(HomepageSection, section_id)
    if section is None or not section.scd_active_flag:
        raise NotFound("That homepage section does not exist.")

    stored = media.store(image, folder="banners")
    field = f"image_path_{language}"
    previous = getattr(section, field)
    setattr(section, field, stored.path)
    section.scd_changed_by = staff.pk_user_id
    db.commit()

    if previous and previous != stored.path:
        media.delete(previous)
    return _redirect("/admin/content")


# ---------------------------------------------------------------------------


def _reindex(db: Session, product: Product) -> None:
    from app.services.search import reindex_product

    reindex_product(db, product)
