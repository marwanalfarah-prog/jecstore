"""Admin product catalog screens."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.core.templating import templates
from app.db.session import get_db
from app.models.enums import (
    AttributeInputType,
    AttributeVisibility,
    DiscountKind,
    OverlapRule,
    ReviewStatus,
)
from app.models.identity import User
from app.services import approvals, catalog_admin
from app.services import catalog_actions  # noqa: F401 - registers replay handlers
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, has_permission, require_permission

router = APIRouter(prefix="/products")


@router.get("")
def product_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "view")),
    db: Session = Depends(get_db),
) -> Response:
    visible = request.query_params.get("visible")
    visible_filter = None if visible not in {"1", "0"} else visible == "1"
    category_id = _optional_int(request.query_params.get("category"))
    result = catalog_admin.active_products(
        db,
        search=request.query_params.get("q"),
        category_id=category_id,
        visible=visible_filter,
        page=max(_optional_int(request.query_params.get("page")) or 1, 1),
    )
    return templates.TemplateResponse(
        request,
        "admin/products/list.html",
        admin_context(
            db,
            staff,
            rows=result.rows,
            total=result.total,
            page=result.page,
            per_page=result.per_page,
            total_pages=result.total_pages,
            categories=catalog_admin.active_categories(db),
            flash=request.query_params.get("flash"),
        ),
    )


@router.get("/new")
def new_product(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "create_product")),
    db: Session = Depends(get_db),
) -> Response:
    return templates.TemplateResponse(
        request,
        "admin/products/new.html",
        admin_context(
            db,
            staff,
            categories=catalog_admin.active_categories(db),
            publishers=catalog_admin.active_publishers(db),
            overlap_rules=list(OverlapRule),
        ),
    )


@router.post("/new")
async def create_product(
    request: Request,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "create_product")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    params = _product_params(form)
    params["base_price_amt"] = _decimal(form.get("base_price_amt")) or Decimal("0")
    params["sku"] = _text(form.get("sku"))
    params["barcode"] = _text(form.get("barcode"))
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="create_product",
        params=params,
        summary_en=f"Create product {params['name_en']}",
        summary_ar=f"إنشاء منتج {params['name_ar']}",
        target_table="scd_product",
    )
    db.commit()
    if result.pending:
        return RedirectResponse("/admin/products?flash=pending", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(
        f"/admin/products/{result.value.pk_product_id}?flash=saved",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Tags (Part I §15)
# ---------------------------------------------------------------------------
#
# Registered above `/{product_id}` on purpose: FastAPI matches in declaration
# order, so a `/products/tags` declared after it is swallowed by the path
# parameter and answers 422 instead of rendering.


@router.get("/tags")
def tag_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "view")),
    db: Session = Depends(get_db),
) -> Response:
    return templates.TemplateResponse(
        request,
        "admin/products/tags.html",
        admin_context(
            db,
            staff,
            rows=catalog_admin.active_tags(db),
            can_manage=has_permission(db, staff, "catalog", "manage_tags"),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/tags")
async def create_tag(
    request: Request,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "manage_tags")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="manage_tags",
        params={
            "operation": "create",
            "name_ar": _text(form.get("name_ar")) or "",
            "name_en": _text(form.get("name_en")) or "",
            "slug": _text(form.get("slug")),
        },
        summary_en=f"Create tag {_text(form.get('name_en'))}",
        target_table="lkp_tag",
    )
    db.commit()
    return _tags_back("pending" if result.pending else "created")


@router.post("/tags/{tag_id}")
async def update_tag(
    request: Request,
    tag_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "manage_tags")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="manage_tags",
        params={
            "operation": "update",
            "tag_id": tag_id,
            "name_ar": _text(form.get("name_ar")) or "",
            "name_en": _text(form.get("name_en")) or "",
            "slug": _text(form.get("slug")),
        },
        summary_en=f"Rename tag {tag_id}",
        target_table="lkp_tag",
        target_row_id=tag_id,
    )
    db.commit()
    return _tags_back("pending" if result.pending else "saved")


@router.post("/tags/{tag_id}/close")
def close_tag(
    request: Request,
    tag_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "manage_tags")),
    db: Session = Depends(get_db),
) -> Response:
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="manage_tags",
        params={"operation": "close", "tag_id": tag_id},
        summary_en=f"Retire tag {tag_id}",
        target_table="lkp_tag",
        target_row_id=tag_id,
    )
    db.commit()
    return _tags_back("pending" if result.pending else "closed")


@router.post("/{product_id}/tags")
async def assign_tags(
    request: Request,
    product_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "manage_tags")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="manage_tags",
        params={
            "operation": "assign",
            "product_id": product_id,
            "tag_ids": [int(value) for value in form.getlist("tag_ids") if value],
        },
        summary_en=f"Set tags on product {product_id}",
        target_table="lkp_product_tag",
        target_row_id=product_id,
    )
    db.commit()
    return _back(product_id, "pending" if result.pending else "saved")


def _tags_back(flash: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/products/tags?flash={flash}", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Custom product details (Part I section 5.2)
# ---------------------------------------------------------------------------


@router.get("/attributes")
def attribute_list(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "view")),
    db: Session = Depends(get_db),
) -> Response:
    return templates.TemplateResponse(
        request,
        "admin/products/attributes.html",
        admin_context(
            db,
            staff,
            rows=catalog_admin.active_attributes(db),
            input_types=list(AttributeInputType),
            visibility_options=list(AttributeVisibility),
            can_manage=has_permission(db, staff, "catalog", "edit_product"),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/attributes")
async def create_attribute(
    request: Request,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    params = _attribute_params(form, operation="create_attribute")
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="edit_product",
        params=params,
        summary_en=f"Create product detail {params['name_en']}",
        target_table="lkp_product_attribute",
    )
    db.commit()
    return _attributes_back("pending" if result.pending else "created")


@router.post("/attributes/{attribute_id}")
async def update_attribute(
    request: Request,
    attribute_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    params = _attribute_params(form, operation="update_attribute")
    params["attribute_id"] = attribute_id
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="edit_product",
        params=params,
        summary_en=f"Update product detail {attribute_id}",
        target_table="lkp_product_attribute",
        target_row_id=attribute_id,
    )
    db.commit()
    return _attributes_back("pending" if result.pending else "saved")


@router.post("/attributes/{attribute_id}/close")
def close_attribute(
    request: Request,
    attribute_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="edit_product",
        params={"operation": "close_attribute", "attribute_id": attribute_id},
        summary_en=f"Retire product detail {attribute_id}",
        target_table="lkp_product_attribute",
        target_row_id=attribute_id,
    )
    db.commit()
    return _attributes_back("pending" if result.pending else "closed")


def _attributes_back(flash: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/products/attributes?flash={flash}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{product_id}/attributes")
async def set_product_attributes(
    request: Request,
    product_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    values = _attribute_value_params(
        form,
        attributes=[row.attribute for row in catalog_admin.active_attributes(db)],
        variants=catalog_admin.product_variants(db, product_id),
    )
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="edit_product",
        params={
            "operation": "set_attribute_values",
            "product_id": product_id,
            "values": json.dumps(values),
        },
        summary_en=f"Set custom details on product {product_id}",
        target_table="scd_product_attribute_value",
        target_row_id=product_id,
    )
    db.commit()
    return _back(product_id, "pending" if result.pending else "saved")


@router.post("/{product_id}/attributes/definitions")
async def create_attribute_from_product(
    request: Request,
    product_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    catalog_admin.product_detail(db, product_id)
    form = await request.form()
    params = _attribute_params(form, operation="create_attribute")
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="edit_product",
        params=params,
        summary_en=f"Create product detail {params['name_en']}",
        target_table="lkp_product_attribute",
    )
    db.commit()
    return _back(product_id, "pending" if result.pending else "created")


@router.get("/reviews")
def reviews(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "view")),
    db: Session = Depends(get_db),
) -> Response:
    selected_status = request.query_params.get("status") or ReviewStatus.PENDING.value
    if selected_status not in {item.value for item in ReviewStatus}:
        selected_status = None
    return templates.TemplateResponse(
        request,
        "admin/products/reviews.html",
        admin_context(
            db,
            staff,
            reviews=catalog_admin.active_reviews(db, status=selected_status),
            statuses=list(ReviewStatus),
            selected_status=selected_status,
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/reviews/{review_id}/moderate")
async def moderate_review(
    request: Request,
    review_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "moderate_reviews")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    status_value = str(form.get("status") or "")
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="moderate_reviews",
        params={
            "review_id": review_id,
            "status": status_value,
            "note": _text(form.get("note")),
        },
        summary_en=f"Moderate review {review_id} as {status_value}",
        target_table="scd_product_review",
        target_row_id=review_id,
    )
    db.commit()
    flash = "pending" if result.pending else "saved"
    return RedirectResponse(f"/admin/products/reviews?flash={flash}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{product_id}")
def product_detail(
    request: Request,
    product_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("catalog", "view")),
    db: Session = Depends(get_db),
) -> Response:
    product = catalog_admin.product_detail(db, product_id)
    return templates.TemplateResponse(
        request,
        "admin/products/detail.html",
        admin_context(
            db,
            staff,
            product=product,
            variants=catalog_admin.product_variants(db, product_id),
            images=catalog_admin.product_images(db, product_id),
            discounts=catalog_admin.product_discounts(db, product_id),
            categories=catalog_admin.active_categories(db),
            publishers=catalog_admin.active_publishers(db),
            primary_category_id=catalog_admin.primary_category_id(db, product_id),
            all_tags=catalog_admin.active_tags(db),
            product_tag_ids=catalog_admin.product_tag_ids(db, product_id),
            can_manage_tags=has_permission(db, staff, "catalog", "manage_tags"),
            attribute_rows=catalog_admin.active_attributes(db),
            attribute_value_index=catalog_admin.attribute_value_index(db, product_id),
            attribute_input_types=list(AttributeInputType),
            attribute_visibility_options=list(AttributeVisibility),
            can_manage_attributes=has_permission(db, staff, "catalog", "edit_product"),
            overlap_rules=list(OverlapRule),
            discount_kinds=list(DiscountKind),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/{product_id}")
async def update_product(
    request: Request,
    product_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    params = _product_params(form)
    params.update(
        {
            "product_id": product_id,
            "operation": "update",
            "min_stock_level": _optional_int(form.get("min_stock_level")),
            "optimal_stock_level": _optional_int(form.get("optimal_stock_level")),
            "max_stock_level": _optional_int(form.get("max_stock_level")),
        }
    )
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="edit_product",
        params=params,
        summary_en=f"Edit product {params['name_en']}",
        summary_ar=f"تعديل منتج {params['name_ar']}",
        target_table="scd_product",
        target_row_id=product_id,
    )
    db.commit()
    return _back(product_id, "pending" if result.pending else "saved")


@router.post("/{product_id}/price")
async def change_price(
    request: Request,
    product_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "change_price")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    amount = _decimal(form.get("base_price_amt"))
    if amount is None:
        raise ValidationFailed("Price is required.")
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="change_price",
        params={"product_id": product_id, "base_price_amt": amount},
        summary_en=f"Change product {product_id} price to {amount}",
        target_table="scd_product",
        target_row_id=product_id,
    )
    db.commit()
    return _back(product_id, "pending" if result.pending else "saved")


@router.post("/{product_id}/variants")
async def create_variant(
    request: Request,
    product_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="edit_product",
        params={
            "operation": "create_variant",
            "product_id": product_id,
            "sku": _text(form.get("sku")),
            "name_ar": _text(form.get("name_ar")),
            "name_en": _text(form.get("name_en")),
            "barcode": _text(form.get("barcode")),
            "price_override_amt": _decimal(form.get("price_override_amt")),
            "weight_grams": _optional_int(form.get("weight_grams")),
            "is_active": form.get("is_active") == "1",
            "sort_order": _optional_int(form.get("sort_order")) or 0,
        },
        summary_en=f"Create variant for product {product_id}",
        target_table="scd_product_variant",
    )
    db.commit()
    return _back(product_id, "pending" if result.pending else "variant_added")


@router.post("/{product_id}/variants/{variant_id}")
async def update_variant(
    request: Request,
    product_id: int,
    variant_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    """Edit one variant in place, from its own row on the product page.

    Each row saves on its own so a shopkeeper correcting one price cannot
    accidentally rewrite the rest of the table.
    """
    form = await request.form()
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="edit_product",
        params={
            "operation": "update_variant",
            "variant_id": variant_id,
            "sku": _text(form.get("sku")),
            "name_ar": _text(form.get("name_ar")),
            "name_en": _text(form.get("name_en")),
            "barcode": _text(form.get("barcode")),
            "price_override_amt": _decimal(form.get("price_override_amt")),
            "weight_grams": _optional_int(form.get("weight_grams")),
            "is_active": form.get("is_active") == "1",
            "sort_order": _optional_int(form.get("sort_order")) or 0,
        },
        summary_en=f"Update variant {variant_id}",
        target_table="scd_product_variant",
        target_row_id=variant_id,
    )
    db.commit()
    return _back(product_id, "pending" if result.pending else "variant_saved")


@router.post("/{product_id}/variants/{variant_id}/close")
def close_variant(
    request: Request,
    product_id: int,
    variant_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "edit_product")),
    db: Session = Depends(get_db),
) -> Response:
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="edit_product",
        params={"operation": "close_variant", "variant_id": variant_id},
        summary_en=f"Close variant {variant_id}",
        target_table="scd_product_variant",
        target_row_id=variant_id,
    )
    db.commit()
    return _back(product_id, "pending" if result.pending else "saved")


@router.post("/{product_id}/discounts")
async def create_product_discount(
    request: Request,
    product_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "apply_discount")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    params = _discount_params(form)
    params.update({"discount_scope": "product", "product_id": product_id, "category_id": None})
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="apply_discount",
        params=params,
        summary_en=f"Apply discount to product {product_id}",
        target_table="scd_discount",
    )
    db.commit()
    return _back(product_id, "pending" if result.pending else "saved")


@router.post("/{product_id}/discounts/{discount_id}/close")
def close_discount(
    request: Request,
    product_id: int,
    discount_id: int,
    staff: User = Depends(current_staff),
    decision: GrantDecision = Depends(require_permission("catalog", "apply_discount")),
    db: Session = Depends(get_db),
) -> Response:
    result = approvals.execute_or_submit(
        db,
        request,
        decision,
        staff,
        module="catalog",
        action="apply_discount",
        params={"operation": "close", "discount_id": discount_id},
        summary_en=f"Close discount {discount_id}",
        target_table="scd_discount",
        target_row_id=discount_id,
    )
    db.commit()
    return _back(product_id, "pending" if result.pending else "saved")


def _product_params(form) -> dict:
    return {
        "name_ar": str(form.get("name_ar") or "").strip(),
        "name_en": str(form.get("name_en") or "").strip(),
        "category_id": _optional_int(form.get("category_id")),
        "publisher_id": _optional_int(form.get("publisher_id")),
        "slug_ar": _text(form.get("slug_ar")),
        "slug_en": _text(form.get("slug_en")),
        "description_ar": _text(form.get("description_ar")),
        "description_en": _text(form.get("description_en")),
        "short_description_ar": _text(form.get("short_description_ar")),
        "short_description_en": _text(form.get("short_description_en")),
        "isbn": _text(form.get("isbn")),
        "is_visible": form.get("is_visible") == "1",
        "published": form.get("published") == "1",
        "discount_overlap_rule": str(form.get("discount_overlap_rule") or OverlapRule.BEST_FOR_CUSTOMER.value),
    }


def _discount_params(form) -> dict:
    return {
        "name_ar": str(form.get("discount_name_ar") or "").strip(),
        "name_en": str(form.get("discount_name_en") or "").strip(),
        "discount_kind": str(form.get("discount_kind") or DiscountKind.PERCENTAGE.value),
        "percentage": _decimal(form.get("percentage")),
        "fixed_price_amt": _decimal(form.get("fixed_price_amt")),
        "starts_dt": _datetime(form.get("starts_dt")),
        "ends_dt": _datetime(form.get("ends_dt")),
        "include_subcategories": form.get("include_subcategories") == "1",
        "priority": _optional_int(form.get("priority")) or 0,
    }


def _attribute_params(form, *, operation: str) -> dict:
    input_type = str(form.get("input_type") or AttributeInputType.TEXT.value)
    choices = (
        _attribute_choices(form)
        if input_type == AttributeInputType.DROPDOWN.value
        else []
    )
    return {
        "operation": operation,
        "name_ar": str(form.get("name_ar") or "").strip(),
        "name_en": str(form.get("name_en") or "").strip(),
        "attribute_code": _text(form.get("attribute_code")),
        "input_type": input_type,
        "visibility": str(form.get("visibility") or AttributeVisibility.PUBLIC.value),
        "is_filterable": form.get("is_filterable") == "1",
        "is_comparable": form.get("is_comparable") == "1",
        "sort_order": _optional_int(form.get("sort_order")) or 0,
        "choices": json.dumps(
            [{"value_ar": value_ar, "value_en": value_en} for value_ar, value_en in choices]
        ),
    }


def _attribute_choices(form) -> list[tuple[str, str]]:
    values_ar = str(form.get("choices_ar") or "").splitlines()
    values_en = str(form.get("choices_en") or "").splitlines()
    rows: list[tuple[str, str]] = []
    for index in range(max(len(values_ar), len(values_en))):
        value_ar = values_ar[index].strip() if index < len(values_ar) else ""
        value_en = values_en[index].strip() if index < len(values_en) else ""
        if bool(value_ar) != bool(value_en):
            raise ValidationFailed("Dropdown choices need both languages.")
        if value_ar and value_en:
            rows.append((value_ar, value_en))
    return rows


def _attribute_value_params(form, *, attributes, variants) -> list[dict[str, object]]:
    rendered_attribute_ids = {
        int(value) for value in form.getlist("attribute_ids") if str(value).strip()
    }
    rendered_variant_ids = {
        int(value) for value in form.getlist("variant_ids") if str(value).strip()
    }

    values: list[dict[str, object]] = []
    for attribute in attributes:
        attribute_id = attribute.pk_product_attribute_id
        if rendered_attribute_ids and attribute_id not in rendered_attribute_ids:
            continue
        values.append(_attribute_value_param(form, attribute, None, f"p_{attribute_id}"))

        for variant in variants:
            variant_id = variant.pk_product_variant_id
            if rendered_variant_ids and variant_id not in rendered_variant_ids:
                continue
            values.append(
                _attribute_value_param(
                    form,
                    attribute,
                    variant_id,
                    f"v_{variant_id}_{attribute_id}",
                )
            )
    return values


def _attribute_value_param(
    form,
    attribute,
    variant_id: int | None,
    prefix: str,
) -> dict[str, object]:
    value: dict[str, object] = {
        "attribute_id": attribute.pk_product_attribute_id,
        "variant_id": variant_id,
    }
    if attribute.input_type == AttributeInputType.DROPDOWN.value:
        choice_id = _optional_int(form.get(f"{prefix}_choice_id"))
        if choice_id is not None:
            value["choice_id"] = choice_id
        return value

    value["value_ar"] = _text(form.get(f"{prefix}_value_ar"))
    value["value_en"] = _text(form.get(f"{prefix}_value_en"))
    return value


def _decimal(value) -> Decimal | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValidationFailed("Enter a valid amount.") from exc


def _optional_int(value) -> int | None:
    value = str(value or "").strip()
    if not value:
        return None
    return int(value)


def _datetime(value) -> dt.datetime | None:
    value = str(value or "").strip()
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed


def _text(value) -> str | None:
    value = str(value or "").strip()
    return value or None


def _back(product_id: int, flash: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/products/{product_id}?flash={flash}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
