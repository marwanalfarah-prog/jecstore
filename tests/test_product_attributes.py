"""Custom product details, product-wide and per variant (Part I section 5.2)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.models.catalog import ProductAttributeChoice, ProductAttributeValue
from app.models.enums import AttributeInputType, AttributeVisibility
from app.services import catalog_actions, catalog_admin
from tests.test_checkout import db, store  # noqa: F401 - fixtures


def test_a_product_can_carry_a_reusable_custom_detail(
    db: Session, store: dict
) -> None:
    attribute = catalog_admin.create_attribute(
        db, name_ar="الأبعاد", name_en="Dimensions"
    )
    value = catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        attribute_id=attribute.pk_product_attribute_id,
        value_ar="10 سم",
        value_en="10 cm",
    )
    db.commit()

    assert value is not None
    assert value.fk_product_variant_id is None
    indexed = catalog_admin.attribute_value_index(db, store["product"].pk_product_id)
    assert indexed["product"][attribute.pk_product_attribute_id].value_en == "10 cm"


def test_unchanged_values_are_not_rewritten(db: Session, store: dict) -> None:
    attribute = catalog_admin.create_attribute(db, name_ar="الخامة", name_en="Material")
    first = catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        attribute_id=attribute.pk_product_attribute_id,
        value_ar="خشب",
        value_en="Wood",
    )
    db.commit()

    second = catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        attribute_id=attribute.pk_product_attribute_id,
        value_ar="خشب",
        value_en="Wood",
    )
    db.commit()

    rows = db.scalars(select(ProductAttributeValue)).all()
    assert second.pk_product_attribute_value_id == first.pk_product_attribute_value_id
    assert len(rows) == 1
    assert rows[0].scd_active_flag is True


def test_changing_a_value_closes_the_old_version(db: Session, store: dict) -> None:
    attribute = catalog_admin.create_attribute(db, name_ar="الخامة", name_en="Material")
    first = catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        attribute_id=attribute.pk_product_attribute_id,
        value_ar="خشب",
        value_en="Wood",
    )
    db.commit()

    replacement = catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        attribute_id=attribute.pk_product_attribute_id,
        value_ar="معدن",
        value_en="Metal",
    )
    db.commit()

    rows = db.scalars(
        select(ProductAttributeValue).where(
            ProductAttributeValue.fk_product_attribute_id
            == attribute.pk_product_attribute_id
        )
    ).all()
    assert first.scd_active_flag is False
    assert replacement is not None
    assert replacement.scd_active_flag is True
    assert len(rows) == 2


def test_blank_value_clears_the_detail(db: Session, store: dict) -> None:
    attribute = catalog_admin.create_attribute(db, name_ar="الخامة", name_en="Material")
    catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        attribute_id=attribute.pk_product_attribute_id,
        value_ar="خشب",
        value_en="Wood",
    )
    db.commit()

    cleared = catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        attribute_id=attribute.pk_product_attribute_id,
        value_ar="",
        value_en="",
    )
    db.commit()

    active = db.scalars(
        select(ProductAttributeValue).where(
            ProductAttributeValue.fk_product_attribute_id
            == attribute.pk_product_attribute_id,
            ProductAttributeValue.scd_active_flag.is_(True),
        )
    ).all()
    assert cleared is None
    assert active == []


def test_variant_specific_details_are_scoped_to_the_variant(
    db: Session, store: dict
) -> None:
    attribute = catalog_admin.create_attribute(
        db, name_ar="الأبعاد", name_en="Dimensions"
    )
    variant = catalog_admin.create_variant(
        db, product_id=store["product"].pk_product_id, name_en="Large"
    )
    value = catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        variant_id=variant.pk_product_variant_id,
        attribute_id=attribute.pk_product_attribute_id,
        value_ar="20 سم",
        value_en="20 cm",
    )
    db.commit()

    assert value is not None
    assert value.fk_product_variant_id == variant.pk_product_variant_id
    indexed = catalog_admin.attribute_value_index(db, store["product"].pk_product_id)
    assert (
        indexed[f"variant:{variant.pk_product_variant_id}"][
            attribute.pk_product_attribute_id
        ].value_en
        == "20 cm"
    )


def test_variant_detail_must_belong_to_the_product(
    db: Session, store: dict
) -> None:
    other = catalog_admin.create_product(
        db, name_ar="آخر", name_en="Other", base_price_amt="1.000"
    )
    other_variant = catalog_admin.product_variants(db, other.pk_product_id)[0]
    attribute = catalog_admin.create_attribute(
        db, name_ar="الأبعاد", name_en="Dimensions"
    )
    db.commit()

    with pytest.raises(NotFound):
        catalog_admin.set_attribute_value(
            db,
            product_id=store["product"].pk_product_id,
            variant_id=other_variant.pk_product_variant_id,
            attribute_id=attribute.pk_product_attribute_id,
            value_en="Wrong product",
        )


def test_public_product_details_appear_on_the_storefront(
    db: Session, store: dict
) -> None:
    from app.web.storefront import _specifications

    attribute = catalog_admin.create_attribute(
        db, name_ar="الأبعاد", name_en="Dimensions"
    )
    catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        attribute_id=attribute.pk_product_attribute_id,
        value_ar="10 سم",
        value_en="10 cm",
    )
    db.commit()

    specs = _specifications(db, store["product"], "en")
    assert ("Dimensions", "10 cm") in [(spec.label, spec.value) for spec in specs]


def test_admin_only_details_never_appear_on_the_storefront(
    db: Session, store: dict
) -> None:
    from app.web.storefront import _specifications

    attribute = catalog_admin.create_attribute(
        db,
        name_ar="مكان الرف",
        name_en="Shelf number",
        visibility=AttributeVisibility.ADMIN_ONLY.value,
    )
    catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        attribute_id=attribute.pk_product_attribute_id,
        value_ar="أ-1",
        value_en="A-1",
    )
    db.commit()

    specs = _specifications(db, store["product"], "en")
    assert "Shelf number" not in [spec.label for spec in specs]
    assert "A-1" not in [spec.value for spec in specs]


def test_variant_scoped_details_do_not_pollute_general_specs(
    db: Session, store: dict
) -> None:
    from app.web.storefront import _specifications

    attribute = catalog_admin.create_attribute(
        db, name_ar="الأبعاد", name_en="Dimensions"
    )
    catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        variant_id=store["variant"].pk_product_variant_id,
        attribute_id=attribute.pk_product_attribute_id,
        value_ar="20 سم",
        value_en="20 cm",
    )
    db.commit()

    specs = _specifications(db, store["product"], "en")
    assert "20 cm" not in [spec.value for spec in specs]


def test_dropdown_details_store_the_choice_id_and_render_the_choice(
    db: Session, store: dict
) -> None:
    from app.web.storefront import _specifications

    attribute = catalog_admin.create_attribute(
        db,
        name_ar="اللون",
        name_en="Color",
        input_type=AttributeInputType.DROPDOWN.value,
        choices=[("أحمر", "Red"), ("أزرق", "Blue")],
    )
    choice = db.scalars(
        select(ProductAttributeChoice).where(ProductAttributeChoice.value_en == "Blue")
    ).one()
    value = catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        attribute_id=attribute.pk_product_attribute_id,
        choice_id=choice.pk_product_attribute_choice_id,
    )
    db.commit()

    assert value is not None
    assert value.value_en is None
    assert value.fk_product_attribute_choice_id == choice.pk_product_attribute_choice_id
    specs = _specifications(db, store["product"], "en")
    assert ("Color", "Blue") in [(spec.label, spec.value) for spec in specs]


def test_retiring_a_detail_closes_its_values_and_choices(
    db: Session, store: dict
) -> None:
    attribute = catalog_admin.create_attribute(
        db,
        name_ar="اللون",
        name_en="Color",
        input_type=AttributeInputType.DROPDOWN.value,
        choices=[("أحمر", "Red")],
    )
    choice = db.scalars(select(ProductAttributeChoice)).one()
    value = catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        attribute_id=attribute.pk_product_attribute_id,
        choice_id=choice.pk_product_attribute_choice_id,
    )
    db.commit()

    catalog_admin.close_attribute(db, attribute_id=attribute.pk_product_attribute_id)
    db.commit()

    assert attribute.scd_active_flag is False
    assert choice.scd_active_flag is False
    assert value is not None
    assert value.scd_active_flag is False


def test_retiring_a_variant_closes_its_custom_detail_values(
    db: Session, store: dict
) -> None:
    attribute = catalog_admin.create_attribute(
        db, name_ar="الأبعاد", name_en="Dimensions"
    )
    value = catalog_admin.set_attribute_value(
        db,
        product_id=store["product"].pk_product_id,
        variant_id=store["variant"].pk_product_variant_id,
        attribute_id=attribute.pk_product_attribute_id,
        value_ar="20 سم",
        value_en="20 cm",
    )
    db.commit()

    catalog_admin.close_variant(db, variant_id=store["variant"].pk_product_variant_id)
    db.commit()

    assert value is not None
    assert value.scd_active_flag is False


def test_duplicate_detail_codes_are_refused(db: Session) -> None:
    catalog_admin.create_attribute(db, name_ar="الأبعاد", name_en="Dimensions")
    db.commit()

    with pytest.raises(Conflict):
        catalog_admin.create_attribute(
            db, name_ar="أبعاد أخرى", name_en="Dimensions"
        )


def test_dropdown_details_need_choices(db: Session) -> None:
    with pytest.raises(ValidationFailed):
        catalog_admin.create_attribute(
            db,
            name_ar="اللون",
            name_en="Color",
            input_type=AttributeInputType.DROPDOWN.value,
        )


def test_attribute_values_survive_action_replay(db: Session, store: dict) -> None:
    attribute = catalog_actions.edit_product(
        db,
        {
            "operation": "create_attribute",
            "name_ar": "الأبعاد",
            "name_en": "Dimensions",
            "input_type": AttributeInputType.TEXT.value,
            "visibility": AttributeVisibility.PUBLIC.value,
            "choices": json.dumps([]),
        },
        None,
    )
    db.commit()

    catalog_actions.edit_product(
        db,
        {
            "operation": "set_attribute_values",
            "product_id": store["product"].pk_product_id,
            "values": json.dumps(
                [
                    {
                        "attribute_id": attribute.pk_product_attribute_id,
                        "variant_id": None,
                        "value_ar": "10 سم",
                        "value_en": "10 cm",
                    }
                ]
            ),
        },
        None,
    )
    db.commit()

    indexed = catalog_admin.attribute_value_index(db, store["product"].pk_product_id)
    assert indexed["product"][attribute.pk_product_attribute_id].value_en == "10 cm"
