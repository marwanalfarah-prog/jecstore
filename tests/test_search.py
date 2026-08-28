"""Search normalisation and matching (Part I §3.1, §15).

§3.1 requires the search bar to account for Arabic/English normalisation, and
§15 asks for typo tolerance. Arabic is where this earns its keep: the same word
is routinely typed several ways, and without folding, the most natural spelling
returns nothing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, utcnow
from app.models.catalog import Product, Publisher
from app.services import search, search_text


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("الكِتاب المقدَّس", "الكتاب المقدس"),   # tashkeel stripped
        ("مسبحـــة", "مسبحه"),                    # tatweel removed, ة→ه
        ("أيقونة", "ايقونه"),                     # أ→ا, ة→ه
        ("إيمان", "ايمان"),                       # إ→ا
        ("آمين", "امين"),                         # آ→ا
        ("عيسى", "عيسي"),                         # ى→ي
        ("مسؤول", "مسوول"),                       # ؤ→و
        ("٢٠٢٦", "2026"),                          # Arabic-Indic digits
        ("  Holy   BIBLE  ", "holy bible"),        # case + whitespace
        ("Saint-Joseph!", "saint joseph"),         # punctuation
    ],
)
def test_normalisation_folds_equivalent_spellings(raw: str, expected: str):
    assert search_text.normalize(raw) == expected


def test_differently_spelled_words_normalise_identically():
    """The point of the exercise: two natural spellings, one comparable form."""
    assert search_text.normalize("أيقونة") == search_text.normalize("ايقونه")
    assert search_text.normalize("الكِتاب") == search_text.normalize("الكتاب")


def test_index_text_keeps_both_article_forms():
    """"كتاب" must find "الكتاب" without the query guessing which was indexed."""
    indexed = search_text.index_text("الكتاب المقدس")
    assert "الكتاب" in indexed
    assert "كتاب" in indexed


def test_tokens_drop_stopwords_and_single_letters():
    assert search_text.tokens("the Holy Bible of a") == ["holy", "bible"]


def test_normalising_empty_input_is_safe():
    assert search_text.normalize(None) == ""
    assert search_text.index_text(None, "") == ""


# ---------------------------------------------------------------------------
# Typo budget
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("term", "budget"),
    [("abc", 0), ("book", 1), ("bibles", 1), ("rosaries", 2)],
)
def test_typo_budget_scales_with_length(term: str, budget: int):
    """Two edits on a three-letter word would match almost anything."""
    assert search_text.max_edits(term) == budget


def test_short_terms_get_no_typo_allowance():
    assert search_text.is_fuzzy_match("icn", "icon") is False


def test_prefixes_always_match():
    """Someone typing half a word is still typing, not making a mistake."""
    assert search_text.is_fuzzy_match("stat", "statue") is True


def test_close_misspellings_match():
    assert search_text.is_fuzzy_match("bibel", "bible") is True
    assert search_text.is_fuzzy_match("rosarry", "rosary") is True


def test_unrelated_words_do_not_match():
    assert search_text.is_fuzzy_match("bible", "rosary") is False


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_exact_phrase_outranks_a_fuzzy_hit():
    """An exact phrase must always beat a lucky typo match."""
    exact = search_text.score("holy bible", "holy bible arabic translation")
    fuzzy = search_text.score("holy bibel", "holy bible arabic translation")
    assert exact > fuzzy > 0


def test_score_is_zero_for_unrelated_text():
    assert search_text.score("bible", "olive wood rosary") == 0.0


def test_score_is_zero_for_an_empty_query():
    assert search_text.score("", "anything") == 0.0


# ---------------------------------------------------------------------------
# End-to-end against the SQL backend
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = maker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def catalog(db: Session) -> dict:
    now = utcnow()
    publisher = Publisher(
        name_ar="دار المشرق", name_en="Dar El-Machreq", slug="dar",
        scd_active_from=now,
    )
    db.add(publisher)
    db.flush()

    products = [
        ("الكتاب المقدس — الترجمة العربية", "Holy Bible — Arabic Translation", "9781234567890"),
        ("أيقونة السيدة العذراء", "Icon of the Virgin Mary", None),
        ("مسبحة خشب الزيتون", "Olive Wood Rosary", None),
        ("تمثال القديس يوسف", "Statue of Saint Joseph", None),
    ]
    for index, (name_ar, name_en, isbn) in enumerate(products):
        product = Product(
            fk_publisher_id=publisher.pk_publisher_id,
            name_ar=name_ar, name_en=name_en,
            slug_ar=f"p{index}-ar", slug_en=f"p{index}-en",
            isbn=isbn,
            base_price_amt=Decimal("10.000"),
            purchase_count=10 - index,
            scd_active_from=now,
        )
        db.add(product)
    db.commit()

    search.reindex_all(db)
    db.commit()
    search.reset_backend(search.SqlBackend())
    return {"publisher": publisher}


def _titles(result) -> list[str]:
    return [p.name_en for p in result.products]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("الكتاب المقدس", "Holy Bible — Arabic Translation"),   # exact
        ("الكِتاب المقدَّس", "Holy Bible — Arabic Translation"),  # with tashkeel
        ("كتاب", "Holy Bible — Arabic Translation"),             # no article
        ("ايقونه", "Icon of the Virgin Mary"),                    # folded forms
        ("أيقونة", "Icon of the Virgin Mary"),                    # canonical
        ("مسبحه", "Olive Wood Rosary"),                           # ة→ه
    ],
)
def test_arabic_search_finds_the_right_product(
    db: Session, catalog: dict, query: str, expected: str
):
    result = search.search_products(db, q=query, language="ar")
    assert result.products, f"{query!r} returned nothing"
    assert result.products[0].name_en == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Bible", "Holy Bible — Arabic Translation"),
        ("bibel", "Holy Bible — Arabic Translation"),    # transposed
        ("rosarry", "Olive Wood Rosary"),                # doubled letter
        ("statu", "Statue of Saint Joseph"),             # prefix
    ],
)
def test_english_search_tolerates_typos(
    db: Session, catalog: dict, query: str, expected: str
):
    result = search.search_products(db, q=query, language="en")
    assert result.products, f"{query!r} returned nothing"
    assert result.products[0].name_en == expected


def test_nonsense_returns_nothing(db: Session, catalog: dict):
    """Fuzzy matching must not turn every query into a hit."""
    assert search.search_products(db, q="zzzzqqqq", language="en").total == 0


def test_search_matches_across_languages(db: Session, catalog: dict):
    """A shopper browsing in Arabic still types "Bible" sometimes."""
    assert search.search_products(db, q="Bible", language="ar").products


def test_isbn_is_searchable(db: Session, catalog: dict):
    result = search.search_products(db, q="9781234567890", language="en")
    assert result.products[0].name_en == "Holy Bible — Arabic Translation"


def test_publisher_name_is_searchable(db: Session, catalog: dict):
    assert search.search_products(db, q="Machreq", language="en").products


def test_empty_query_lists_everything(db: Session, catalog: dict):
    assert search.search_products(db, q="", language="en").total == 4


def test_suggestions_need_two_characters(db: Session, catalog: dict):
    assert search.suggestions(db, q="k", language="ar") == []
    assert search.suggestions(db, q="كتا", language="ar")


def test_results_paginate(db: Session, catalog: dict):
    first = search.search_products(db, q="", language="en", page=1, per_page=25)
    assert first.page_size == 25
    assert len(first.products) == 4


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


def test_reindex_populates_both_languages(db: Session, catalog: dict):
    from sqlalchemy import select

    for product in db.scalars(select(Product)).all():
        assert product.search_text_ar
        assert product.search_text_en


def test_index_includes_the_publisher(db: Session, catalog: dict):
    from sqlalchemy import select

    product = db.scalars(select(Product)).first()
    assert "machreq" in product.search_text_en


def test_reindex_is_idempotent(db: Session, catalog: dict):
    from sqlalchemy import select

    before = {p.pk_product_id: p.search_text_ar for p in db.scalars(select(Product)).all()}
    search.reindex_all(db)
    db.commit()
    after = {p.pk_product_id: p.search_text_ar for p in db.scalars(select(Product)).all()}
    assert before == after
