"""Catalog actions registered with the maker-checker engine."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.services import approvals, catalog_admin


@approvals.register("catalog", "create_category")
def create_or_update_category(
    db: Session,
    params: dict[str, Any],
    actor_user_id: int | None,
):
    if params.get("operation") == "update":
        return catalog_admin.update_category(
            db,
            category_id=int(params["category_id"]),
            name_ar=params["name_ar"],
            name_en=params["name_en"],
            parent_category_id=params.get("parent_category_id"),
            slug_ar=params.get("slug_ar"),
            slug_en=params.get("slug_en"),
            description_ar=params.get("description_ar"),
            description_en=params.get("description_en"),
            image_path=params.get("image_path"),
            sort_order=int(params.get("sort_order") or 0),
            is_visible=bool(params.get("is_visible", True)),
            actor_user_id=actor_user_id,
        )
    return catalog_admin.create_category(
        db,
        name_ar=params["name_ar"],
        name_en=params["name_en"],
        parent_category_id=params.get("parent_category_id"),
        slug_ar=params.get("slug_ar"),
        slug_en=params.get("slug_en"),
        description_ar=params.get("description_ar"),
        description_en=params.get("description_en"),
        image_path=params.get("image_path"),
        sort_order=int(params.get("sort_order") or 0),
        is_visible=bool(params.get("is_visible", True)),
        actor_user_id=actor_user_id,
    )


@approvals.register("catalog", "delete_category")
def delete_category(
    db: Session,
    params: dict[str, Any],
    actor_user_id: int | None,
):
    return catalog_admin.close_category(
        db,
        category_id=int(params["category_id"]),
        actor_user_id=actor_user_id,
    )


@approvals.register("catalog", "manage_publishers")
def manage_publisher(
    db: Session,
    params: dict[str, Any],
    actor_user_id: int | None,
):
    if params.get("operation") == "update":
        return catalog_admin.update_publisher(
            db,
            publisher_id=int(params["publisher_id"]),
            name_ar=params["name_ar"],
            name_en=params["name_en"],
            slug=params.get("slug"),
            logo_path=params.get("logo_path"),
            description_ar=params.get("description_ar"),
            description_en=params.get("description_en"),
            show_on_homepage=bool(params.get("show_on_homepage", True)),
            sort_order=int(params.get("sort_order") or 0),
            actor_user_id=actor_user_id,
        )
    return catalog_admin.create_publisher(
        db,
        name_ar=params["name_ar"],
        name_en=params["name_en"],
        slug=params.get("slug"),
        logo_path=params.get("logo_path"),
        description_ar=params.get("description_ar"),
        description_en=params.get("description_en"),
        show_on_homepage=bool(params.get("show_on_homepage", True)),
        sort_order=int(params.get("sort_order") or 0),
        actor_user_id=actor_user_id,
    )


@approvals.register("catalog", "manage_tags")
def manage_tags(
    db: Session,
    params: dict[str, Any],
    actor_user_id: int | None,
):
    """Create, rename, retire a tag, or set one product's tags (Part I §15).

    Four operations behind one action rather than four permissions: a member of
    staff who may label the catalog may label it, and splitting "rename a tag"
    from "apply a tag" would be a distinction Admin has to maintain on
    /admin/access without ever wanting to act on it.
    """
    operation = params.get("operation")

    if operation == "update":
        return catalog_admin.update_tag(
            db,
            tag_id=int(params["tag_id"]),
            name_ar=params["name_ar"],
            name_en=params["name_en"],
            slug=params.get("slug"),
            actor_user_id=actor_user_id,
        )
    if operation == "close":
        return catalog_admin.close_tag(
            db, tag_id=int(params["tag_id"]), actor_user_id=actor_user_id
        )
    if operation == "assign":
        return catalog_admin.set_product_tags(
            db,
            product_id=int(params["product_id"]),
            tag_ids=[int(value) for value in params.get("tag_ids") or []],
            actor_user_id=actor_user_id,
        )
    return catalog_admin.create_tag(
        db,
        name_ar=params["name_ar"],
        name_en=params["name_en"],
        slug=params.get("slug"),
        actor_user_id=actor_user_id,
    )


@approvals.register("catalog", "create_product")
def create_product(
    db: Session,
    params: dict[str, Any],
    actor_user_id: int | None,
):
    return catalog_admin.create_product(
        db,
        name_ar=params["name_ar"],
        name_en=params["name_en"],
        base_price_amt=Decimal(params["base_price_amt"]),
        category_id=params.get("category_id"),
        publisher_id=params.get("publisher_id"),
        sku=params.get("sku"),
        barcode=params.get("barcode"),
        slug_ar=params.get("slug_ar"),
        slug_en=params.get("slug_en"),
        description_ar=params.get("description_ar"),
        description_en=params.get("description_en"),
        short_description_ar=params.get("short_description_ar"),
        short_description_en=params.get("short_description_en"),
        isbn=params.get("isbn"),
        main_image_path=params.get("main_image_path"),
        is_visible=bool(params.get("is_visible", True)),
        published=bool(params.get("published", False)),
        discount_overlap_rule=params.get("discount_overlap_rule") or "best_for_customer",
        actor_user_id=actor_user_id,
    )


@approvals.register("catalog", "edit_product")
def edit_product(
    db: Session,
    params: dict[str, Any],
    actor_user_id: int | None,
):
    operation = params.get("operation")
    if operation == "create_variant":
        return catalog_admin.create_variant(
            db,
            product_id=int(params["product_id"]),
            sku=params.get("sku"),
            name_ar=params.get("name_ar"),
            name_en=params.get("name_en"),
            barcode=params.get("barcode"),
            price_override_amt=_decimal(params.get("price_override_amt")),
            main_image_path=params.get("main_image_path"),
            weight_grams=params.get("weight_grams"),
            is_active=bool(params.get("is_active", True)),
            sort_order=int(params.get("sort_order") or 0),
            actor_user_id=actor_user_id,
        )
    if operation == "update_variant":
        return catalog_admin.update_variant(
            db,
            variant_id=int(params["variant_id"]),
            sku=params.get("sku"),
            name_ar=params.get("name_ar"),
            name_en=params.get("name_en"),
            barcode=params.get("barcode"),
            price_override_amt=_decimal(params.get("price_override_amt")),
            main_image_path=params.get("main_image_path"),
            weight_grams=params.get("weight_grams"),
            is_active=bool(params.get("is_active", True)),
            sort_order=int(params.get("sort_order") or 0),
            actor_user_id=actor_user_id,
        )
    if operation == "close_variant":
        return catalog_admin.close_variant(
            db,
            variant_id=int(params["variant_id"]),
            actor_user_id=actor_user_id,
        )
    if operation == "create_attribute":
        return catalog_admin.create_attribute(
            db,
            name_ar=params["name_ar"],
            name_en=params["name_en"],
            attribute_code=params.get("attribute_code"),
            input_type=params.get("input_type") or "text",
            visibility=params.get("visibility") or "public",
            is_filterable=bool(params.get("is_filterable", False)),
            is_comparable=bool(params.get("is_comparable", True)),
            sort_order=int(params.get("sort_order") or 0),
            choices=_choices(params.get("choices")),
            actor_user_id=actor_user_id,
        )
    if operation == "update_attribute":
        return catalog_admin.update_attribute(
            db,
            attribute_id=int(params["attribute_id"]),
            name_ar=params["name_ar"],
            name_en=params["name_en"],
            visibility=params.get("visibility") or "public",
            is_filterable=bool(params.get("is_filterable", False)),
            is_comparable=bool(params.get("is_comparable", True)),
            sort_order=int(params.get("sort_order") or 0),
            choices=_choices(params.get("choices")),
            actor_user_id=actor_user_id,
        )
    if operation == "close_attribute":
        return catalog_admin.close_attribute(
            db,
            attribute_id=int(params["attribute_id"]),
            actor_user_id=actor_user_id,
        )
    if operation == "set_attribute_values":
        return catalog_admin.set_attribute_values(
            db,
            product_id=int(params["product_id"]),
            values=_values(params.get("values")),
            actor_user_id=actor_user_id,
        )
    return catalog_admin.update_product(
        db,
        product_id=int(params["product_id"]),
        name_ar=params["name_ar"],
        name_en=params["name_en"],
        category_id=params.get("category_id"),
        publisher_id=params.get("publisher_id"),
        slug_ar=params.get("slug_ar"),
        slug_en=params.get("slug_en"),
        description_ar=params.get("description_ar"),
        description_en=params.get("description_en"),
        short_description_ar=params.get("short_description_ar"),
        short_description_en=params.get("short_description_en"),
        isbn=params.get("isbn"),
        # Absent key, not a null one: the details form no longer carries
        # the image, so omitting it must preserve what was uploaded.
        main_image_path=params.get("main_image_path", catalog_admin.KEEP),
        is_visible=bool(params.get("is_visible", True)),
        published=bool(params.get("published", False)),
        discount_overlap_rule=params.get("discount_overlap_rule") or "best_for_customer",
        min_stock_level=params.get("min_stock_level"),
        optimal_stock_level=params.get("optimal_stock_level"),
        max_stock_level=params.get("max_stock_level"),
        actor_user_id=actor_user_id,
    )


@approvals.register("catalog", "change_price")
def change_price(
    db: Session,
    params: dict[str, Any],
    actor_user_id: int | None,
):
    return catalog_admin.change_price(
        db,
        product_id=int(params["product_id"]),
        base_price_amt=Decimal(params["base_price_amt"]),
        actor_user_id=actor_user_id,
    )


@approvals.register("catalog", "apply_discount")
def apply_discount(
    db: Session,
    params: dict[str, Any],
    actor_user_id: int | None,
):
    if params.get("operation") == "close":
        return catalog_admin.close_discount(
            db,
            discount_id=int(params["discount_id"]),
            actor_user_id=actor_user_id,
        )
    return catalog_admin.create_discount(
        db,
        name_ar=params["name_ar"],
        name_en=params["name_en"],
        discount_scope=params["discount_scope"],
        product_id=params.get("product_id"),
        category_id=params.get("category_id"),
        include_subcategories=bool(params.get("include_subcategories", True)),
        discount_kind=params["discount_kind"],
        percentage=_decimal(params.get("percentage")),
        fixed_price_amt=_decimal(params.get("fixed_price_amt")),
        starts_dt=params.get("starts_dt"),
        ends_dt=params.get("ends_dt"),
        priority=int(params.get("priority") or 0),
        actor_user_id=actor_user_id,
    )


@approvals.register("catalog", "moderate_reviews")
def moderate_review(
    db: Session,
    params: dict[str, Any],
    actor_user_id: int | None,
):
    return catalog_admin.moderate_review(
        db,
        review_id=int(params["review_id"]),
        status=params["status"],
        note=params.get("note"),
        actor_user_id=actor_user_id,
    )


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    return Decimal(str(value))


def _choices(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, str):
        value = json.loads(value) if value.strip() else []
    rows: list[tuple[str, str]] = []
    for item in value or []:
        if isinstance(item, dict):
            rows.append(
                (str(item.get("value_ar") or ""), str(item.get("value_en") or ""))
            )
        else:
            value_ar, value_en = item
            rows.append((str(value_ar or ""), str(value_en or "")))
    return rows


def _values(value: Any) -> list[dict[str, object]]:
    if isinstance(value, str):
        value = json.loads(value) if value.strip() else []
    return list(value or [])
