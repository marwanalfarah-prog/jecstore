"""Product editing: inline image upload and free-form variants (§5.2, §5.4).

Three behaviours are pinned here, all of them things a shopkeeper does daily:

* an image is attached by picking a file, never by typing a storage path, and
  saving the details form afterwards does not wipe it;
* a variant needs nothing but a name — the SKU is stock-keeping plumbing and
  generates itself;
* each variant saves on its own, and a product whose variants are plain labels
  still gives the shopper something to choose between.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict
from app.models.catalog import Product, ProductVariant
from app.services import catalog_admin
from tests.test_checkout import db, store  # noqa: F401 - fixtures


# ---------------------------------------------------------------------------
# Variants: only the product is mandatory
# ---------------------------------------------------------------------------


def test_a_variant_needs_nothing_but_a_name(db: Session, store: dict) -> None:
    variant = catalog_admin.create_variant(
        db,
        product_id=store["product"].pk_product_id,
        name_ar="أسود",
        name_en="Black",
    )
    db.commit()

    assert variant.name_en == "Black"
    assert variant.name_ar == "أسود"
    assert variant.sku, "a SKU must be generated rather than demanded"
    assert variant.price_override_amt is None
    assert variant.barcode is None


def test_a_variant_can_be_created_with_no_fields_at_all(
    db: Session, store: dict
) -> None:
    """The blankest possible case still has to produce a usable row."""
    variant = catalog_admin.create_variant(
        db, product_id=store["product"].pk_product_id
    )
    db.commit()

    assert variant.pk_product_variant_id is not None
    assert variant.sku
    assert variant.is_active_flag is True


def test_generated_skus_do_not_collide(db: Session, store: dict) -> None:
    product_id = store["product"].pk_product_id
    made = [
        catalog_admin.create_variant(db, product_id=product_id) for _ in range(5)
    ]
    db.commit()

    skus = [v.sku for v in made]
    assert len(set(skus)) == len(skus), skus


def test_a_generated_sku_is_not_reused_after_a_variant_is_retired(
    db: Session, store: dict
) -> None:
    """SKUs reach printed labels and shipment lines, so reuse is a real mix-up."""
    product_id = store["product"].pk_product_id
    first = catalog_admin.create_variant(db, product_id=product_id)
    db.commit()
    retired_sku = first.sku

    catalog_admin.close_variant(db, variant_id=first.pk_product_variant_id)
    db.commit()

    second = catalog_admin.create_variant(db, product_id=product_id)
    db.commit()

    assert second.sku != retired_sku


def test_an_explicit_sku_is_still_honoured(db: Session, store: dict) -> None:
    variant = catalog_admin.create_variant(
        db, product_id=store["product"].pk_product_id, sku="MY-OWN-CODE"
    )
    db.commit()
    assert variant.sku == "MY-OWN-CODE"


def test_a_duplicate_sku_is_still_refused(db: Session, store: dict) -> None:
    """Optional does not mean unchecked."""
    product_id = store["product"].pk_product_id
    catalog_admin.create_variant(db, product_id=product_id, sku="TAKEN")
    db.commit()

    with pytest.raises(Conflict):
        catalog_admin.create_variant(db, product_id=product_id, sku="TAKEN")


def test_editing_a_variant_with_a_blank_sku_keeps_the_existing_one(
    db: Session, store: dict
) -> None:
    variant = catalog_admin.create_variant(
        db, product_id=store["product"].pk_product_id, sku="KEEP-ME"
    )
    db.commit()

    catalog_admin.update_variant(
        db, variant_id=variant.pk_product_variant_id, name_en="Blue", sku=""
    )
    db.commit()

    assert variant.sku == "KEEP-ME"
    assert variant.name_en == "Blue"


def test_editing_a_variant_updates_only_that_variant(
    db: Session, store: dict
) -> None:
    """Per-row saving is the point: one row must not rewrite its neighbours."""
    product_id = store["product"].pk_product_id
    first = catalog_admin.create_variant(db, product_id=product_id, name_en="Black")
    second = catalog_admin.create_variant(db, product_id=product_id, name_en="Blue")
    db.commit()

    catalog_admin.update_variant(
        db,
        variant_id=first.pk_product_variant_id,
        name_en="Charcoal",
        price_override_amt=Decimal("12.500"),
    )
    db.commit()

    assert first.name_en == "Charcoal"
    assert first.price_override_amt == Decimal("12.500")
    assert second.name_en == "Blue"
    assert second.price_override_amt is None


# ---------------------------------------------------------------------------
# Images: uploaded, not typed — and not destroyed by the next save
# ---------------------------------------------------------------------------


def test_saving_details_does_not_wipe_an_uploaded_image(
    db: Session, store: dict
) -> None:
    """The regression that would otherwise bite on every single edit.

    The details form no longer carries the image, so `update_product` must read
    an absent key as "leave it alone" rather than as "clear it".
    """
    product: Product = store["product"]
    product.main_image_path = "products/abc123.jpg"
    db.commit()

    catalog_admin.update_product(
        db,
        product_id=product.pk_product_id,
        name_ar=product.name_ar,
        name_en=product.name_en,
    )
    db.commit()

    assert product.main_image_path == "products/abc123.jpg"


def test_an_image_can_still_be_cleared_deliberately(
    db: Session, store: dict
) -> None:
    """"Not submitted" and "cleared" must stay distinguishable."""
    product: Product = store["product"]
    product.main_image_path = "products/abc123.jpg"
    db.commit()

    catalog_admin.update_product(
        db,
        product_id=product.pk_product_id,
        name_ar=product.name_ar,
        name_en=product.name_en,
        main_image_path=None,
    )
    db.commit()

    assert product.main_image_path is None


# ---------------------------------------------------------------------------
# The rendered screens
# ---------------------------------------------------------------------------


def test_the_product_page_offers_a_file_picker_not_a_path_box(
    db: Session, store: dict
) -> None:
    """Read the template directly: the point is what the screen asks for."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "app/templates/admin/products/detail.html"
    ).read_text(encoding="utf-8")

    assert 'name="main_image_path"' not in source, (
        "the details form must not ask anyone to type a storage path"
    )
    assert 'type="file"' in source
    assert "/admin/uploads/products/" in source
    assert 'enctype="multipart/form-data"' in source


def test_each_variant_row_posts_to_its_own_endpoint(db: Session, store: dict) -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "app/templates/admin/products/detail.html"
    ).read_text(encoding="utf-8")

    assert "/variants/{{ variant.pk_product_variant_id }}\"" in source, (
        "each row needs its own save target so rows save independently"
    )


# ---------------------------------------------------------------------------
# The shopper's side of the same change
# ---------------------------------------------------------------------------


def test_named_variants_are_offered_to_the_shopper(db: Session, store: dict) -> None:
    """Several variants and no option matrix must still yield a picker.

    Otherwise the shopper silently receives the first variant and the labels
    staff typed are invisible.
    """
    from app.web.storefront import _selectable_variants

    product: Product = store["product"]
    catalog_admin.create_variant(db, product_id=product.pk_product_id, name_en="Black")
    db.commit()
    db.refresh(product)

    offered = _selectable_variants(db, product)
    assert len(offered) == 2, [v.sku for v in offered]


def test_a_single_variant_is_not_presented_as_a_choice(
    db: Session, store: dict
) -> None:
    """A choice of one is not a choice."""
    from app.web.storefront import _selectable_variants

    product: Product = store["product"]
    db.refresh(product)
    assert _selectable_variants(db, product) == []


def test_a_retired_variant_is_not_offered(db: Session, store: dict) -> None:
    from app.web.storefront import _selectable_variants

    product: Product = store["product"]
    extra = catalog_admin.create_variant(
        db, product_id=product.pk_product_id, name_en="Black"
    )
    db.commit()

    catalog_admin.close_variant(db, variant_id=extra.pk_product_variant_id)
    db.commit()
    db.refresh(product)

    assert _selectable_variants(db, product) == []


def test_variant_rows_survive_a_round_trip_through_the_action_replay(
    db: Session, store: dict
) -> None:
    """Maker-Checker replays from stored scalars, so the new fields must survive."""
    from app.services import catalog_actions

    created = catalog_actions.edit_product(
        db,
        {
            "operation": "create_variant",
            "product_id": store["product"].pk_product_id,
            "name_ar": "كبير",
            "name_en": "Large",
        },
        None,
    )
    db.commit()

    assert created.name_en == "Large"
    assert created.name_ar == "كبير"
    assert created.sku

    catalog_actions.edit_product(
        db,
        {
            "operation": "update_variant",
            "variant_id": created.pk_product_variant_id,
            "name_en": "Extra large",
        },
        None,
    )
    db.commit()

    stored = db.scalars(
        select(ProductVariant).where(
            ProductVariant.pk_product_variant_id == created.pk_product_variant_id
        )
    ).first()
    assert stored.name_en == "Extra large"


# ---------------------------------------------------------------------------
# Retired variants must leave the storefront
# ---------------------------------------------------------------------------
#
# These pin a bug that predates the free-form-variant work: the storefront
# filtered variants on `is_active_flag` alone and never on `scd_active_flag`.
# Retiring a variant closes the SCD row but leaves `is_active_flag` true, so a
# retired variant stayed pickable — and `default_variant`, which took
# `product.variants[0]` unfiltered, could hand add-to-cart a retired row
# outright.


def test_a_retired_variant_is_not_the_default(db: Session, store: dict) -> None:
    """The worst case: retired stock silently sold as the default choice."""
    from app.web.storefront import _live_variants

    product: Product = store["product"]
    original = product.variants[0]
    replacement = catalog_admin.create_variant(
        db, product_id=product.pk_product_id, name_en="Replacement"
    )
    db.commit()

    catalog_admin.close_variant(db, variant_id=original.pk_product_variant_id)
    db.commit()
    db.refresh(product)

    live = _live_variants(product)
    assert original not in live
    assert live == [replacement]


def test_a_variant_switched_off_for_sale_is_not_offered(
    db: Session, store: dict
) -> None:
    """The other flag: still a live row, but the shopkeeper turned it off."""
    from app.web.storefront import _live_variants

    product: Product = store["product"]
    extra = catalog_admin.create_variant(
        db, product_id=product.pk_product_id, name_en="Black", is_active=False
    )
    db.commit()
    db.refresh(product)

    assert extra not in _live_variants(product)


def test_the_option_matrix_also_drops_retired_variants(
    db: Session, store: dict
) -> None:
    """`_variant_axes` shared the same blind spot and needs the same guard."""
    from app.web.storefront import _variant_axes

    product: Product = store["product"]
    for variant in list(product.variants):
        catalog_admin.close_variant(db, variant_id=variant.pk_product_variant_id)
    db.commit()
    db.refresh(product)

    assert _variant_axes(db, product) == []
