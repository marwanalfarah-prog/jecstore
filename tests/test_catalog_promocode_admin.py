"""Catalog and promocode admin write services."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import CategoryNotEmpty, Conflict
from app.db.base import utcnow
from app.models.catalog import (
    Category,
    Product,
    ProductReview,
    ProductVariant,
)
from app.models.enums import PromocodeKind, ReviewStatus
from app.models.marketing import PromocodeRestriction
from app.services import catalog_admin, promocode_admin
from tests.test_checkout import db, store  # noqa: F401 - fixtures


def test_create_product_adds_primary_category_and_default_variant(
    db: Session,
    store: dict,
):
    category = db.scalars(select(Category)).first()

    product = catalog_admin.create_product(
        db,
        name_ar="كتاب جديد",
        name_en="New Book",
        base_price_amt=Decimal("9.500"),
        category_id=category.pk_category_id,
        sku="SKU-NEW",
    )
    db.commit()

    assert catalog_admin.primary_category_id(db, product.pk_product_id) == category.pk_category_id
    variant = db.scalars(
        select(ProductVariant).where(ProductVariant.fk_product_id == product.pk_product_id)
    ).one()
    assert variant.sku == "SKU-NEW"


def test_duplicate_active_sku_is_blocked(db: Session, store: dict):
    category = db.scalars(select(Category)).first()

    with pytest.raises(Conflict):
        catalog_admin.create_product(
            db,
            name_ar="نسخة",
            name_en="Duplicate",
            base_price_amt=Decimal("5.000"),
            category_id=category.pk_category_id,
            sku=store["variant"].sku,
        )


def test_close_category_with_product_link_is_blocked(db: Session, store: dict):
    category = db.scalars(select(Category)).first()

    with pytest.raises(CategoryNotEmpty):
        catalog_admin.close_category(db, category_id=category.pk_category_id)


def test_category_parent_change_rebuilds_descendant_paths(db: Session):
    root = catalog_admin.create_category(db, name_ar="جذر", name_en="Root")
    child = catalog_admin.create_category(
        db,
        name_ar="فرعي",
        name_en="Child",
        parent_category_id=root.pk_category_id,
    )
    grandchild = catalog_admin.create_category(
        db,
        name_ar="فرعي ثان",
        name_en="Grandchild",
        parent_category_id=child.pk_category_id,
    )
    db.commit()

    catalog_admin.update_category(
        db,
        category_id=child.pk_category_id,
        name_ar=child.name_ar,
        name_en=child.name_en,
        parent_category_id=None,
    )
    db.commit()

    assert child.ancestor_path == "/"
    assert grandchild.ancestor_path == f"/{child.pk_category_id}/"
    assert grandchild.depth == 1


def test_review_moderation_records_status_and_actor(db: Session, store: dict):
    review = ProductReview(
        fk_product_id=store["product"].pk_product_id,
        fk_user_id=store["user"].pk_user_id,
        rating=5,
        title="Good",
        body="Useful",
        submitted_dt=utcnow(),
        status=ReviewStatus.PENDING,
        scd_active_from=utcnow(),
    )
    db.add(review)
    db.commit()

    catalog_admin.moderate_review(
        db,
        review_id=review.pk_product_review_id,
        status=ReviewStatus.APPROVED,
        note="Approved",
        actor_user_id=store["user"].pk_user_id,
    )
    db.commit()

    assert review.status == ReviewStatus.APPROVED
    assert review.moderated_by_user_id == store["user"].pk_user_id
    assert review.moderation_note == "Approved"


def test_promocode_restrictions_are_replaced_by_closing_old_rows(
    db: Session,
    store: dict,
):
    category = db.scalars(select(Category)).first()
    promo = promocode_admin.create_promocode(
        db,
        code="summer",
        promocode_kind=PromocodeKind.PERCENTAGE,
        percentage=Decimal("10"),
        restrictions=[
            promocode_admin.RestrictionSpec(
                target_type="category",
                target_id=category.pk_category_id,
            )
        ],
    )
    db.commit()
    old = promocode_admin.active_restrictions(db, promo.pk_promocode_id)[0]

    promocode_admin.update_promocode(
        db,
        promocode_id=promo.pk_promocode_id,
        code="summer",
        promocode_kind=PromocodeKind.PERCENTAGE,
        percentage=Decimal("15"),
        restrictions=[
            promocode_admin.RestrictionSpec(
                target_type="product",
                target_id=store["product"].pk_product_id,
                is_exclusion=True,
            )
        ],
    )
    db.commit()

    all_rows = db.scalars(
        select(PromocodeRestriction).where(
            PromocodeRestriction.fk_promocode_id == promo.pk_promocode_id
        )
    ).all()
    active = promocode_admin.active_restrictions(db, promo.pk_promocode_id)
    assert old.scd_active_flag is False
    assert len(all_rows) == 2
    assert len(active) == 1
    assert active[0].fk_product_id == store["product"].pk_product_id
    assert active[0].is_exclusion_flag is True


def test_duplicate_active_promocode_code_is_blocked(db: Session):
    promocode_admin.create_promocode(
        db,
        code="SAVE",
        promocode_kind=PromocodeKind.FIXED_AMOUNT,
        fixed_amount_amt=Decimal("1.000"),
    )
    db.commit()

    with pytest.raises(Conflict):
        promocode_admin.create_promocode(
            db,
            code=" save ",
            promocode_kind=PromocodeKind.FIXED_AMOUNT,
            fixed_amount_amt=Decimal("1.000"),
        )
