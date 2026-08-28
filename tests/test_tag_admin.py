"""Tags, end to end (Part I §15).

§15 gives every tag its own results page and folds tag words into the product
search index. Both of those had always been built — and both had always been
dead, because nothing in the panel could create a tag or put one on a product.
`lkp_tag` shipped empty, so `/tag/{id}` had nothing to show and the tag terms
`search.build_index_text()` folds into every product's projection were always
the empty set.

So these tests are about the join, not the CRUD: creating a tag and applying it
must actually change what the storefront and the search index see.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound
from app.models.catalog import ProductTag, Tag
from app.services import catalog_admin, search
from tests.test_checkout import db, store  # noqa: F401 - fixtures


@pytest.fixture
def tag(db: Session) -> Tag:
    created = catalog_admin.create_tag(db, name_ar="ترانيم", name_en="Hymns")
    db.commit()
    return created


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


def test_a_tag_gets_a_slug_from_its_english_name(db: Session, tag: Tag):
    assert tag.slug == "hymns"
    assert tag.scd_active_flag is True


def test_two_tags_cannot_share_a_slug(db: Session, tag: Tag):
    """The slug is the tag's public URL, so a duplicate would make one of the
    two pages unreachable."""
    with pytest.raises(Conflict):
        catalog_admin.create_tag(db, name_ar="ترانيم أخرى", name_en="Hymns")


def test_a_tag_needs_a_name_in_both_languages(db: Session):
    from app.core.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        catalog_admin.create_tag(db, name_ar="", name_en="Hymns Only")


# ---------------------------------------------------------------------------
# Applying — the part that was missing entirely
# ---------------------------------------------------------------------------


def test_applying_a_tag_puts_its_words_into_the_search_index(
    db: Session, store: dict, tag: Tag
):
    """The reason §15's tag terms were never searchable: no tag was ever
    applied, so the index folded in nothing."""
    product = store["product"]
    before = product.search_text_en or ""
    assert "hymns" not in before.lower()

    catalog_admin.set_product_tags(
        db, product_id=product.pk_product_id, tag_ids=[tag.pk_tag_id]
    )
    db.commit()

    assert "hymns" in (product.search_text_en or "").lower()


def test_setting_tags_is_a_diff_not_a_rewrite(db: Session, store: dict, tag: Tag):
    """An unchanged link must not be closed and reopened: Part II §1's SCD rows
    are meant to record real changes, not every save."""
    other = catalog_admin.create_tag(db, name_ar="أيقونات", name_en="Icons")
    product = store["product"]
    db.commit()

    catalog_admin.set_product_tags(
        db, product_id=product.pk_product_id, tag_ids=[tag.pk_tag_id]
    )
    db.commit()
    first = db.scalars(
        select(ProductTag).where(
            ProductTag.fk_product_id == product.pk_product_id,
            ProductTag.fk_tag_id == tag.pk_tag_id,
        )
    ).one()

    # Re-save with the first tag still selected and a second one added.
    catalog_admin.set_product_tags(
        db,
        product_id=product.pk_product_id,
        tag_ids=[tag.pk_tag_id, other.pk_tag_id],
    )
    db.commit()

    unchanged = db.scalars(
        select(ProductTag).where(
            ProductTag.fk_product_id == product.pk_product_id,
            ProductTag.fk_tag_id == tag.pk_tag_id,
        )
    ).all()
    assert len(unchanged) == 1, "the untouched link was rewritten"
    assert unchanged[0].pk_product_tag_id == first.pk_product_tag_id
    assert unchanged[0].scd_active_flag is True


def test_removing_a_tag_closes_its_link_and_reindexes(
    db: Session, store: dict, tag: Tag
):
    product = store["product"]
    catalog_admin.set_product_tags(
        db, product_id=product.pk_product_id, tag_ids=[tag.pk_tag_id]
    )
    db.commit()

    catalog_admin.set_product_tags(db, product_id=product.pk_product_id, tag_ids=[])
    db.commit()

    assert catalog_admin.product_tag_ids(db, product.pk_product_id) == set()
    assert "hymns" not in (product.search_text_en or "").lower()


def test_a_tag_that_does_not_exist_is_refused(db: Session, store: dict):
    with pytest.raises(NotFound):
        catalog_admin.set_product_tags(
            db, product_id=store["product"].pk_product_id, tag_ids=[9999]
        )


# ---------------------------------------------------------------------------
# Retiring
# ---------------------------------------------------------------------------


def test_retiring_a_tag_detaches_it_from_every_product(
    db: Session, store: dict, tag: Tag
):
    """Closing the tag alone would leave live links pointing at a dead tag:
    `/tag/{id}` would 404 while the product still listed it."""
    product = store["product"]
    catalog_admin.set_product_tags(
        db, product_id=product.pk_product_id, tag_ids=[tag.pk_tag_id]
    )
    db.commit()

    catalog_admin.close_tag(db, tag_id=tag.pk_tag_id)
    db.commit()

    assert tag.scd_active_flag is False
    assert catalog_admin.product_tag_ids(db, product.pk_product_id) == set()
    assert "hymns" not in (product.search_text_en or "").lower()


def test_the_admin_list_counts_products_per_tag(db: Session, store: dict, tag: Tag):
    """A tag on nothing is a typo or an unfinished job, and its page is an
    empty shop window — the screen has to be able to tell them apart."""
    assert [row.product_count for row in catalog_admin.active_tags(db)] == [0]

    catalog_admin.set_product_tags(
        db, product_id=store["product"].pk_product_id, tag_ids=[tag.pk_tag_id]
    )
    db.commit()

    rows = catalog_admin.active_tags(db)
    assert [row.product_count for row in rows] == [1]
    assert rows[0].tag.pk_tag_id == tag.pk_tag_id


def test_renaming_a_tag_reindexes_the_products_carrying_it(
    db: Session, store: dict, tag: Tag
):
    product = store["product"]
    catalog_admin.set_product_tags(
        db, product_id=product.pk_product_id, tag_ids=[tag.pk_tag_id]
    )
    db.commit()

    catalog_admin.update_tag(
        db, tag_id=tag.pk_tag_id, name_ar="تسابيح", name_en="Canticles"
    )
    db.commit()

    text = (product.search_text_en or "").lower()
    assert "canticles" in text
    assert "hymns" not in text


def test_the_index_still_matches_a_rebuild(db: Session, store: dict, tag: Tag):
    """The projection is a derived column (Part II §1's sanctioned exception),
    so the incremental updates above must agree with a full rebuild."""
    product = store["product"]
    catalog_admin.set_product_tags(
        db, product_id=product.pk_product_id, tag_ids=[tag.pk_tag_id]
    )
    db.commit()

    incremental = (product.search_text_ar, product.search_text_en)
    assert search.build_index_text(db, product) == incremental
