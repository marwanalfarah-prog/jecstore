"""Catalog: categories, products, variants, attributes, publishers, discounts.

Implements Part I §5, plus the legacy-parity additions in §5.3, §5.6 and §15.

The load-bearing decision in this module is that **stock lives on the variant,
never on the product** (Part I §5.4). A bracelet in black and blue is one
product differentiated by specification, so the product row carries identity and
copy while each variant carries its own quantity, SKU, barcode and price
override. Every stock check, hold and movement elsewhere in the system keys off
``SCD_PRODUCT_VARIANT``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, MONEY, SCDMixin, TrxBase, UtcDateTime, bilingual, fk, pk
from app.models.enums import (
    AttributeInputType,
    AttributeVisibility,
    DiscountKind,
    DiscountScope,
    OverlapRule,
    ReviewStatus,
)


class Category(Base, SCDMixin):
    """A category at any depth. Unlimited nesting via ``fk_parent_category_id``
    (Part I §5.1)."""

    __tablename__ = "scd_category"
    __grain__ = "One version of one category at any depth."

    pk_category_id: Mapped[int] = pk("category")
    fk_parent_category_id: Mapped[int | None] = mapped_column(
        "fk_parent_category_id",
        Integer,
        ForeignKey("scd_category.pk_category_id"),
        nullable=True,
        index=True,
    )
    name_ar, name_en = bilingual("name", 160)
    slug_ar: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    slug_en: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description_ar: Mapped[str | None] = mapped_column(Text)
    description_en: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(String(500))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_visible_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Denormalised path of ancestor ids ("/1/7/23/") — the one derived value
    #: kept on the row, because breadcrumbs and "everything under this category"
    #: would otherwise need a recursive CTE on every page load. Rebuilt whenever
    #: a parent changes, in app/services/catalog.py.
    ancestor_path: Mapped[str] = mapped_column(String(500), nullable=False, default="/", index=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    parent: Mapped["Category | None"] = relationship(remote_side=[pk_category_id])
    children: Mapped[list["Category"]] = relationship(
        back_populates="parent", overlaps="parent"
    )
    product_links: Mapped[list["ProductCategory"]] = relationship(back_populates="category")

    __table_args__ = (
        Index("ix_scd_category_slug_ar_active", "slug_ar", "scd_active_flag"),
        Index("ix_scd_category_slug_en_active", "slug_en", "scd_active_flag"),
    )


class Publisher(Base, SCDMixin):
    """Publisher / manufacturer, with its own filtered landing page and a
    homepage logo strip (Part I §5.6, §4)."""

    __tablename__ = "scd_publisher"
    __grain__ = "One version of one publisher/manufacturer."

    pk_publisher_id: Mapped[int] = pk("publisher")
    name_ar, name_en = bilingual("name", 160)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    logo_path: Mapped[str | None] = mapped_column(String(500))
    description_ar: Mapped[str | None] = mapped_column(Text)
    description_en: Mapped[str | None] = mapped_column(Text)
    show_on_homepage_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Product(Base, SCDMixin):
    """A sellable item. Identity, copy and shared specifications live here;
    quantity and price-per-combination live on :class:`ProductVariant`."""

    __tablename__ = "scd_product"
    __grain__ = "One version of one product (not one variant)."

    pk_product_id: Mapped[int] = pk("product")
    fk_publisher_id: Mapped[int | None] = fk(
        "publisher", "scd_publisher.pk_publisher_id", nullable=True
    )

    name_ar, name_en = bilingual("name", 300)
    #: Arabic and English names each generate their own slug (Part I §16). On a
    #: collision or a rename the URL falls back to the id form and the old slug
    #: is 301'd from SCD_URL_REDIRECT, so links never break.
    slug_ar: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    slug_en: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    description_ar: Mapped[str | None] = mapped_column(Text)
    description_en: Mapped[str | None] = mapped_column(Text)
    short_description_ar: Mapped[str | None] = mapped_column(String(500))
    short_description_en: Mapped[str | None] = mapped_column(String(500))

    isbn: Mapped[str | None] = mapped_column(String(20), index=True)
    #: Base price in JOD. USD is a display-only conversion (Part I §1.1).
    base_price_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    #: Shared physical specs; a variant may override where it genuinely differs.
    weight_grams: Mapped[int | None] = mapped_column(Integer)
    length_mm: Mapped[int | None] = mapped_column(Integer)
    width_mm: Mapped[int | None] = mapped_column(Integer)
    height_mm: Mapped[int | None] = mapped_column(Integer)

    main_image_path: Mapped[str | None] = mapped_column(String(500))
    #: Manual visibility switch, e.g. tied to stock status (Part I §5.2).
    is_visible_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Precedence when two active discounts both apply — configurable *per
    #: product*, not one global rule (Part I §5.5).
    discount_overlap_rule: Mapped[str] = mapped_column(
        String(30), nullable=False, default=OverlapRule.BEST_FOR_CUSTOMER
    )

    #: Internal metrics that also double as customer-facing social proof when
    #: switched on (Part I §5.3). Null means "follow the storewide default".
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    purchase_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    show_view_count_flag: Mapped[bool | None] = mapped_column(Boolean)
    show_purchase_count_flag: Mapped[bool | None] = mapped_column(Boolean)

    #: Restocking thresholds; the critical-restock dashboard reads these (Part I §11).
    min_stock_level: Mapped[int | None] = mapped_column(Integer)
    optimal_stock_level: Mapped[int | None] = mapped_column(Integer)
    max_stock_level: Mapped[int | None] = mapped_column(Integer)

    meta_title_ar: Mapped[str | None] = mapped_column(String(200))
    meta_title_en: Mapped[str | None] = mapped_column(String(200))
    meta_description_ar: Mapped[str | None] = mapped_column(String(400))
    meta_description_en: Mapped[str | None] = mapped_column(String(400))

    published_dt: Mapped[dt.datetime | None] = mapped_column(
        UtcDateTime, index=True,
        comment="Drives the New Arrivals carousel (Part I §4).",
    )

    #: Normalised, searchable text for this product — a **materialised search
    #: projection**, not stored knowledge. It is derived from the name,
    #: description, ISBN, tags and publisher via
    #: ``services/search_text.index_text()``, and is the second sanctioned
    #: exception to the no-derived-values rule (Part II §1), for the reason
    #: Part II §2 asks to be stated per case: Arabic normalisation cannot be
    #: applied inside a SQL LIKE, so without a precomputed column every search
    #: would have to normalise every row in Python.
    #:
    #: Rebuilt by ``services/search.reindex_product()`` on every catalog write
    #: and by the ``reindex_search`` job.
    search_text_ar: Mapped[str | None] = mapped_column(Text)
    search_text_en: Mapped[str | None] = mapped_column(Text)

    publisher: Mapped[Publisher | None] = relationship()
    variants: Mapped[list["ProductVariant"]] = relationship(back_populates="product")
    images: Mapped[list["ProductImage"]] = relationship(back_populates="product")
    category_links: Mapped[list["ProductCategory"]] = relationship(back_populates="product")
    tag_links: Mapped[list["ProductTag"]] = relationship(back_populates="product")
    attribute_values: Mapped[list["ProductAttributeValue"]] = relationship(
        back_populates="product"
    )

    __table_args__ = (
        Index("ix_scd_product_slug_ar_active", "slug_ar", "scd_active_flag"),
        Index("ix_scd_product_slug_en_active", "slug_en", "scd_active_flag"),
        Index("ix_scd_product_visible", "is_visible_flag", "scd_active_flag"),
        Index("ix_scd_product_purchase_count", "purchase_count"),
        Index("ix_scd_product_view_count", "view_count"),
    )


class ProductCategory(Base, SCDMixin):
    """Product ↔ category. Many-to-many because one item may sit in several
    categories at any level simultaneously (Part I §5.1)."""

    __tablename__ = "lkp_product_category"
    __grain__ = "One version of one product's membership in one category."

    pk_product_category_id: Mapped[int] = pk("product_category")
    fk_product_id: Mapped[int] = fk("product", "scd_product.pk_product_id")
    fk_category_id: Mapped[int] = fk("category", "scd_category.pk_category_id")
    is_primary_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="Drives the canonical URL and breadcrumb when a product is in several categories.",
    )

    product: Mapped[Product] = relationship(back_populates="category_links")
    category: Mapped[Category] = relationship(back_populates="product_links")

    __table_args__ = (
        Index(
            "uq_lkp_product_category_active",
            "fk_product_id",
            "fk_category_id",
            unique=True,
            sqlite_where=text("scd_active_flag = 1"),
            postgresql_where=text("scd_active_flag"),
        ),
    )


class ProductVariant(Base, SCDMixin):
    """One specification combination — the unit stock is actually counted in.

    The combination matrix is generated and validated by the system rather than
    typed in freehand per combination, which is what keeps colour × size ×
    material from exploding unchecked (Part I §5.4).
    """

    __tablename__ = "scd_product_variant"
    __grain__ = "One version of one purchasable specification combination of one product."

    pk_product_variant_id: Mapped[int] = pk("product_variant")
    fk_product_id: Mapped[int] = fk("product", "scd_product.pk_product_id")

    #: What the shopper and the shopkeeper call this combination — "أسود" /
    #: "Black", "كبير" / "Large". Bilingual because it is customer-facing
    #: (Part I §1), and nullable because a product with a single variant has
    #: nothing to distinguish: the label is only meaningful once there is a
    #: choice to make.
    name_ar: Mapped[str | None] = mapped_column(String(120))
    name_en: Mapped[str | None] = mapped_column(String(120))

    #: Generated when staff do not supply one — it exists for stock keeping and
    #: the unique constraint, not as something a shopkeeper must invent.
    sku: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    #: Scanning this in-branch resolves straight to this variant (Part I §5.4).
    barcode: Mapped[str | None] = mapped_column(String(60), index=True)

    #: Overrides the product's base price when this combination costs more.
    price_override_amt: Mapped[Decimal | None] = mapped_column(MONEY)

    #: Optional main photo and gallery *per variant*, on top of the product's
    #: own main photo (Part I §5.4).
    main_image_path: Mapped[str | None] = mapped_column(String(500))

    weight_grams: Mapped[int | None] = mapped_column(Integer)
    is_active_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Which pool a barcode scan or a sale should draw from, when the item could
    #: be consigned or sitting in central storage (Part I §5.4). Null means "ask
    #: at invoice time", per the same decision.
    fk_default_stock_pool_id: Mapped[int | None] = fk(
        "default_stock_pool", "scd_stock_pool.pk_stock_pool_id", nullable=True
    )

    min_stock_level: Mapped[int | None] = mapped_column(Integer)
    optimal_stock_level: Mapped[int | None] = mapped_column(Integer)
    max_stock_level: Mapped[int | None] = mapped_column(Integer)

    product: Mapped[Product] = relationship(back_populates="variants")
    option_values: Mapped[list["VariantOptionValue"]] = relationship(back_populates="variant")
    images: Mapped[list["ProductImage"]] = relationship(back_populates="variant")

    __table_args__ = (
        Index(
            "uq_scd_product_variant_sku_active",
            "sku",
            unique=True,
            sqlite_where=text("scd_active_flag = 1"),
            postgresql_where=text("scd_active_flag"),
        ),
        Index("ix_scd_product_variant_product_active", "fk_product_id", "scd_active_flag"),
    )

    @property
    def unit_price_amt(self) -> Decimal:
        return self.price_override_amt if self.price_override_amt is not None else self.product.base_price_amt


class VariantOption(Base, SCDMixin):
    """A variant axis: Colour, Size, Material (Part I §5.4)."""

    __tablename__ = "lkp_variant_option"
    __grain__ = "One version of one variant axis."

    pk_variant_option_id: Mapped[int] = pk("variant_option")
    option_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name_ar, name_en = bilingual("name", 120)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    values: Mapped[list["VariantOptionChoice"]] = relationship(back_populates="option")


class VariantOptionChoice(Base, SCDMixin):
    """A permitted value on an axis: Black, Blue, XL."""

    __tablename__ = "lkp_variant_option_choice"
    __grain__ = "One version of one permitted value of one variant axis."

    pk_variant_option_choice_id: Mapped[int] = pk("variant_option_choice")
    fk_variant_option_id: Mapped[int] = fk(
        "variant_option", "lkp_variant_option.pk_variant_option_id"
    )
    value_ar, value_en = bilingual("value", 120)
    #: Hex swatch for colour axes, so the picker shows colour rather than words.
    swatch_hex: Mapped[str | None] = mapped_column(String(7))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    option: Mapped[VariantOption] = relationship(back_populates="values")


class VariantOptionValue(Base, SCDMixin):
    """One coordinate of one variant: "this variant is Colour=Black".

    A variant is uniquely identified by its full set of these rows, which is how
    the generator detects and rejects duplicate combinations.
    """

    __tablename__ = "lkp_variant_option_value"
    __grain__ = "One version of one axis value assigned to one variant."

    pk_variant_option_value_id: Mapped[int] = pk("variant_option_value")
    fk_product_variant_id: Mapped[int] = fk(
        "product_variant", "scd_product_variant.pk_product_variant_id"
    )
    fk_variant_option_id: Mapped[int] = fk(
        "variant_option", "lkp_variant_option.pk_variant_option_id"
    )
    fk_variant_option_choice_id: Mapped[int] = fk(
        "variant_option_choice", "lkp_variant_option_choice.pk_variant_option_choice_id"
    )

    variant: Mapped[ProductVariant] = relationship(back_populates="option_values")
    option: Mapped[VariantOption] = relationship()
    choice: Mapped[VariantOptionChoice] = relationship()

    __table_args__ = (
        Index(
            "uq_lkp_variant_option_value_active",
            "fk_product_variant_id",
            "fk_variant_option_id",
            unique=True,
            sqlite_where=text("scd_active_flag = 1"),
            postgresql_where=text("scd_active_flag"),
        ),
    )


class ProductImage(Base, SCDMixin):
    """Gallery image. Attached to the product, or to one variant when the photo
    is specific to that specification (Part I §5.2, §5.4)."""

    __tablename__ = "scd_product_image"
    __grain__ = "One version of one gallery image for one product or variant."

    pk_product_image_id: Mapped[int] = pk("product_image")
    fk_product_id: Mapped[int] = fk("product", "scd_product.pk_product_id")
    fk_product_variant_id: Mapped[int | None] = fk(
        "product_variant", "scd_product_variant.pk_product_variant_id", nullable=True
    )
    image_path: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Alt text is bilingual: it is read aloud and indexed, so it cannot be one
    #: language for both audiences (Part I §1, §17.7).
    alt_text_ar: Mapped[str | None] = mapped_column(String(300))
    alt_text_en: Mapped[str | None] = mapped_column(String(300))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    product: Mapped[Product] = relationship(back_populates="images")
    variant: Mapped[ProductVariant | None] = relationship(back_populates="images")


class Tag(Base, SCDMixin):
    """A keyword. Clickable, with its own results page (Part I §15)."""

    __tablename__ = "lkp_tag"
    __grain__ = "One version of one tag."

    pk_tag_id: Mapped[int] = pk("tag")
    name_ar, name_en = bilingual("name", 120)
    slug: Mapped[str] = mapped_column(String(160), nullable=False, index=True)


class ProductTag(Base, SCDMixin):
    __tablename__ = "lkp_product_tag"
    __grain__ = "One version of one tag applied to one product."

    pk_product_tag_id: Mapped[int] = pk("product_tag")
    fk_product_id: Mapped[int] = fk("product", "scd_product.pk_product_id")
    fk_tag_id: Mapped[int] = fk("tag", "lkp_tag.pk_tag_id")

    product: Mapped[Product] = relationship(back_populates="tag_links")
    tag: Mapped[Tag] = relationship()


class ProductAttribute(Base, SCDMixin):
    """An Admin-defined custom attribute, e.g. "Shelf Number" (Part I §5.2).

    ``visibility`` is what makes one attribute a customer-facing spec and
    another an internal note, without a second table.
    """

    __tablename__ = "lkp_product_attribute"
    __grain__ = "One version of one custom attribute definition."

    pk_product_attribute_id: Mapped[int] = pk("product_attribute")
    attribute_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    name_ar, name_en = bilingual("name", 160)
    input_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AttributeInputType.TEXT
    )
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AttributeVisibility.PUBLIC
    )
    #: Usable as a listing-page filter (Part I §15).
    is_filterable_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Shown on the side-by-side compare page (Part I §14).
    is_comparable_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    choices: Mapped[list["ProductAttributeChoice"]] = relationship(back_populates="attribute")


class ProductAttributeChoice(Base, SCDMixin):
    """A dropdown option. Editable after creation, per Part I §5.2."""

    __tablename__ = "lkp_product_attribute_choice"
    __grain__ = "One version of one dropdown option of one custom attribute."

    pk_product_attribute_choice_id: Mapped[int] = pk("product_attribute_choice")
    fk_product_attribute_id: Mapped[int] = fk(
        "product_attribute", "lkp_product_attribute.pk_product_attribute_id"
    )
    value_ar, value_en = bilingual("value", 200)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    attribute: Mapped[ProductAttribute] = relationship(back_populates="choices")


class ProductAttributeValue(Base, SCDMixin):
    """One custom detail's value, on a product or on one of its variants.

    The row's *existence* is what says the detail applies here: an attribute is
    defined once in ``lkp_product_attribute`` and reused across the catalog, and
    a product carries only the details somebody actually gave it a value for.
    That is why there is no separate "which attributes does this product use"
    table — it would be derivable from this one, which Part II §1 rules out.

    Scoped exactly the way a gallery image is (§5.4): ``fk_product_variant_id``
    null means the value is the product's and every variant inherits it, and set
    means it belongs to that one variant. A bracelet's *material* is the
    product's; its *dimensions* differ per size.
    """

    __tablename__ = "scd_product_attribute_value"
    __grain__ = (
        "One version of one custom attribute's value for one product or variant."
    )

    pk_product_attribute_value_id: Mapped[int] = pk("product_attribute_value")
    fk_product_id: Mapped[int] = fk("product", "scd_product.pk_product_id")
    #: Null for a product-wide value; set when this variant overrides it.
    fk_product_variant_id: Mapped[int | None] = fk(
        "product_variant", "scd_product_variant.pk_product_variant_id", nullable=True
    )
    fk_product_attribute_id: Mapped[int] = fk(
        "product_attribute", "lkp_product_attribute.pk_product_attribute_id"
    )
    fk_product_attribute_choice_id: Mapped[int | None] = fk(
        "product_attribute_choice",
        "lkp_product_attribute_choice.pk_product_attribute_choice_id",
        nullable=True,
    )
    #: Free-text attributes store the text here, in both languages. A dropdown
    #: attribute stores the choice id instead and leaves these null, so renaming
    #: an option updates every product that picked it rather than stranding
    #: copies of the old wording (Part I §5.2: "editable after creation").
    value_ar: Mapped[str | None] = mapped_column(String(500))
    value_en: Mapped[str | None] = mapped_column(String(500))

    product: Mapped[Product] = relationship(back_populates="attribute_values")
    variant: Mapped["ProductVariant | None"] = relationship()
    attribute: Mapped[ProductAttribute] = relationship()
    choice: Mapped["ProductAttributeChoice | None"] = relationship()

    __table_args__ = (
        # "What does this product/variant carry?" is the read on every product
        # page and every admin edit, so it is the one that gets an index.
        Index(
            "ix_scd_product_attribute_value_scope",
            "fk_product_id",
            "fk_product_variant_id",
            "scd_active_flag",
        ),
    )


class Discount(Base, SCDMixin):
    """A time-boxed discount on one product or a whole category (Part I §5.5).

    Category discounts apply to descendants too, so "20% off all Bibles" does
    not need re-entering for every sub-category.
    """

    __tablename__ = "scd_discount"
    __grain__ = "One version of one discount rule."

    pk_discount_id: Mapped[int] = pk("discount")
    name_ar, name_en = bilingual("name", 200)

    discount_scope: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    fk_product_id: Mapped[int | None] = fk(
        "product", "scd_product.pk_product_id", nullable=True
    )
    fk_category_id: Mapped[int | None] = fk(
        "category", "scd_category.pk_category_id", nullable=True
    )
    include_subcategories_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    discount_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Set for PERCENTAGE (e.g. 20.00 = 20%).
    percentage: Mapped[Decimal | None] = mapped_column(MONEY)
    #: Set for FIXED_PRICE — the resulting price in JOD, not the amount off.
    fixed_price_amt: Mapped[Decimal | None] = mapped_column(MONEY)

    starts_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    ends_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    #: Precedence when several discounts collide and the product's rule is
    #: FIRST_MATCH (Part I §5.5).
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    product: Mapped[Product | None] = relationship()
    category: Mapped[Category | None] = relationship()

    __table_args__ = (
        Index("ix_scd_discount_window", "starts_dt", "ends_dt", "scd_active_flag"),
    )

    def is_live(self, now: dt.datetime) -> bool:
        if not self.scd_active_flag:
            return False
        if self.starts_dt and self.starts_dt > now:
            return False
        if self.ends_dt and self.ends_dt <= now:
            return False
        return True


class ProductReview(Base, SCDMixin):
    """A rating and comment, held back until a moderator publishes it
    (Part I §14)."""

    __tablename__ = "scd_product_review"
    __grain__ = "One version of one customer review of one product."

    pk_product_review_id: Mapped[int] = pk("product_review")
    fk_product_id: Mapped[int] = fk("product", "scd_product.pk_product_id")
    fk_user_id: Mapped[int] = fk("user", "scd_user.pk_user_id")
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    #: When the customer wrote it. Its own column rather than reusing
    #: scd_active_from, which moves whenever a moderator edits the row —
    #: the displayed date must not shift because of a moderation action.
    submitted_dt: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ReviewStatus.PENDING, index=True
    )
    moderated_by_user_id: Mapped[int | None] = mapped_column(Integer)
    moderated_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    moderation_note: Mapped[str | None] = mapped_column(Text)
    #: Set when the reviewer actually bought the item — worth showing, and worth
    #: weighting differently in moderation.
    verified_purchase_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_scd_product_review_product_status", "fk_product_id", "status"),
    )


class UrlRedirect(Base, SCDMixin):
    """301 from a retired slug to its current URL (Part I §16).

    Written whenever a name changes post-publish or a slug collides, so an old
    link — including one already shared over WhatsApp — never dies.
    """

    __tablename__ = "scd_url_redirect"
    __grain__ = "One version of one redirect from an old path to a current path."

    pk_url_redirect_id: Mapped[int] = pk("url_redirect")
    old_path: Mapped[str] = mapped_column(String(600), nullable=False, index=True)
    new_path: Mapped[str] = mapped_column(String(600), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("old_path", name="redirect_old_path"),
    )


class ProductViewEvent(TrxBase):
    """One product view. Insert-only.

    ``SCD_PRODUCT.view_count`` is a running counter for cheap sorting; this table
    is the auditable detail behind it and the source for the most-viewed
    carousel and the browsing-behaviour dashboards (Part I §2.8, §4).
    """

    __tablename__ = "trx_product_view"
    __grain__ = "One view of one product by one session."

    pk_product_view_id: Mapped[int] = pk("product_view")
    fk_product_id: Mapped[int] = fk("product", "scd_product.pk_product_id")
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    session_key: Mapped[str | None] = mapped_column(String(64), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45))

    __table_args__ = (
        Index("ix_trx_product_view_product_time", "fk_product_id", "created_dt"),
    )
