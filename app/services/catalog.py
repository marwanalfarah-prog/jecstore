"""Catalog queries, URLs and availability (Part I §5, §15, §16).

Availability is the subtle part. Part I §5.3 draws a hard line around what the
customer may see: **never** cost, **never** exact quantity, **always** a status,
plus which branches hold the item. And §5.4 adds that a product whose variants
are all at zero shows as unavailable outright, rather than landing the shopper
on a page of dead options.

:func:`availability_for_products` is therefore the only sanctioned way to ask
"can they buy this" — it returns a status and branch names, and structurally
cannot leak a number.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import Category, Product, ProductCategory, ProductVariant
from app.models.enums import StockPoolKind
from app.models.inventory import Branch, StockLevel, StockPool

# ---------------------------------------------------------------------------
# Slugs and URLs
# ---------------------------------------------------------------------------

#: Arabic diacritics (tashkeel) and the tatweel elongation mark. Stripped from
#: slugs so "كِتاب" and "كتاب" produce the same URL.
_ARABIC_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_NON_SLUG = re.compile(r"[^\w؀-ۿ]+", re.UNICODE)


def slugify(value: str) -> str:
    """Slugify Arabic or Latin text.

    Arabic characters are kept rather than transliterated: an Arabic product
    name should produce an Arabic slug, which is what Part I §16 asks for when
    it says each language generates its own.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value.strip())
    text = _ARABIC_DIACRITICS.sub("", text)
    # Normalise the alef and yaa variants so search and slug agree.
    text = text.translate(str.maketrans("أإآىة", "اااية"))
    text = _NON_SLUG.sub("-", text).strip("-").lower()
    return text[:180]


def product_url(product: Product, language: str = "ar") -> str:
    """Canonical product URL.

    The id always leads. That is the decision in Part I §16: when AR and EN
    names collide, or a product is renamed after publishing, the id keeps the
    URL resolvable while the old slug 301s from ``SCD_URL_REDIRECT``. The slug
    is there for readers and for SEO, not for routing.
    """
    slug = (product.slug_ar if language == "ar" else product.slug_en) or ""
    return f"/p/{product.pk_product_id}" + (f"/{slug}" if slug else "")


def category_url(category: Category, language: str = "ar") -> str:
    slug = (category.slug_ar if language == "ar" else category.slug_en) or ""
    return f"/c/{category.pk_category_id}" + (f"/{slug}" if slug else "")


def publisher_url(publisher, language: str = "ar") -> str:  # noqa: ARG001 - slug is shared
    return f"/publisher/{publisher.pk_publisher_id}/{publisher.slug or ''}".rstrip("/")


def tag_url(tag) -> str:
    return f"/tag/{tag.pk_tag_id}/{tag.slug or ''}".rstrip("/")


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


class AvailabilityStatus(StrEnum):
    IN_STOCK = "in_stock"
    #: Held centrally or unassigned to a branch: "Available — pickup/shipping
    #: arranged on order" (Part I §5.3).
    AVAILABLE_ON_ORDER = "available_on_order"
    OUT_OF_STOCK = "out_of_stock"


@dataclass(slots=True)
class AvailabilityView:
    """What a customer may be told about stock — and nothing more.

    There is deliberately no quantity field. Exact quantity is never shown
    publicly (Part I §5.3), and the cleanest way to guarantee that is for the
    view model handed to templates not to carry one.
    """

    status: AvailabilityStatus
    branch_names_ar: list[str] = field(default_factory=list)
    branch_names_en: list[str] = field(default_factory=list)

    @property
    def is_purchasable(self) -> bool:
        return self.status != AvailabilityStatus.OUT_OF_STOCK

    @property
    def translation_key(self) -> str:
        return {
            AvailabilityStatus.IN_STOCK: "product.in_stock",
            AvailabilityStatus.AVAILABLE_ON_ORDER: "product.available_on_order",
            AvailabilityStatus.OUT_OF_STOCK: "product.out_of_stock",
        }[self.status]

    @property
    def badge_class(self) -> str:
        return {
            AvailabilityStatus.IN_STOCK: "badge-success",
            AvailabilityStatus.AVAILABLE_ON_ORDER: "badge-warning",
            AvailabilityStatus.OUT_OF_STOCK: "badge-neutral",
        }[self.status]


def availability_for_products(
    db: Session, product_ids: Sequence[int]
) -> dict[int, AvailabilityView]:
    """Resolve availability for a page of products in one query.

    Sellable stock is ``on_hand - reserved``: units on hold for a placed order
    are not available to the next shopper, even though they are still physically
    present (Part I §8). Consignment-out pools are excluded — that stock is not
    ours to sell from here (Part I §7).
    """
    if not product_ids:
        return {}

    rows = db.execute(
        select(
            ProductVariant.fk_product_id,
            StockPool.pool_kind,
            Branch.name_ar,
            Branch.name_en,
            func.sum(StockLevel.quantity_on_hand - StockLevel.quantity_reserved).label("sellable"),
        )
        .join(StockLevel, StockLevel.fk_product_variant_id == ProductVariant.pk_product_variant_id)
        .join(StockPool, StockPool.pk_stock_pool_id == StockLevel.fk_stock_pool_id)
        .outerjoin(Branch, Branch.pk_branch_id == StockPool.fk_branch_id)
        .where(
            ProductVariant.fk_product_id.in_(product_ids),
            ProductVariant.scd_active_flag.is_(True),
            ProductVariant.is_active_flag.is_(True),
            StockLevel.scd_active_flag.is_(True),
            StockPool.scd_active_flag.is_(True),
            StockPool.is_sellable_flag.is_(True),
        )
        .group_by(
            ProductVariant.fk_product_id, StockPool.pool_kind, Branch.name_ar, Branch.name_en
        )
    ).all()

    result: dict[int, AvailabilityView] = {
        pid: AvailabilityView(status=AvailabilityStatus.OUT_OF_STOCK) for pid in product_ids
    }

    for product_id, pool_kind, branch_ar, branch_en, sellable in rows:
        if not sellable or sellable <= 0:
            continue
        view = result[product_id]
        if pool_kind == StockPoolKind.BRANCH and branch_ar:
            view.status = AvailabilityStatus.IN_STOCK
            view.branch_names_ar.append(branch_ar)
            view.branch_names_en.append(branch_en or branch_ar)
        elif view.status is not AvailabilityStatus.IN_STOCK:
            # Central storage or unassigned — available, arranged on order.
            view.status = AvailabilityStatus.AVAILABLE_ON_ORDER

    return result


# ---------------------------------------------------------------------------
# Listing queries
# ---------------------------------------------------------------------------

#: The named sort options Part I §15 requires — no unspecified placeholder.
SORT_OPTIONS: tuple[str, ...] = (
    "default",
    "name_asc",
    "name_desc",
    "price_asc",
    "price_desc",
    "newest",
)

#: Customer-facing page sizes (Part I §15). "All" is capped, because an
#: uncapped page is a denial-of-service waiting for a big category.
PAGE_SIZES: tuple[int, ...] = (25, 50, 75, 100)
MAX_PAGE_SIZE = 250


def normalize_sort(value: str | None) -> str:
    return value if value in SORT_OPTIONS else "default"


def normalize_page_size(value: str | int | None) -> int:
    if isinstance(value, str) and value.lower() == "all":
        return MAX_PAGE_SIZE
    try:
        size = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return PAGE_SIZES[0]
    return size if size in PAGE_SIZES else PAGE_SIZES[0]


def apply_sort(stmt: Select, sort: str, language: str) -> Select:
    name_column = Product.name_ar if language == "ar" else Product.name_en
    match sort:
        case "name_asc":
            return stmt.order_by(name_column.asc())
        case "name_desc":
            return stmt.order_by(name_column.desc())
        case "price_asc":
            return stmt.order_by(Product.base_price_amt.asc())
        case "price_desc":
            return stmt.order_by(Product.base_price_amt.desc())
        case "newest":
            return stmt.order_by(Product.published_dt.desc().nulls_last())
        case _:
            # "Default" is not arbitrary: in-stock, popular items first, which
            # is what a shopper landing on a category actually wants to see.
            return stmt.order_by(Product.purchase_count.desc(), Product.pk_product_id.desc())


def visible_products_stmt() -> Select:
    """Base statement for anything a customer is allowed to see."""
    return (
        select(Product)
        .where(Product.scd_active_flag.is_(True), Product.is_visible_flag.is_(True))
        .options(selectinload(Product.variants), selectinload(Product.publisher))
    )


def products_in_category(
    db: Session,
    category: Category,
    *,
    language: str = "ar",
    sort: str = "default",
    page: int = 1,
    page_size: int = 25,
    in_stock_only: bool = False,
) -> tuple[list[Product], int]:
    """Products in a category *and all its descendants*, paginated.

    Descendants are matched on ``ancestor_path`` rather than with a recursive
    CTE, which keeps this to one indexed range scan — the reason that column is
    maintained in the first place.
    """
    descendant_ids = select(Category.pk_category_id).where(
        Category.scd_active_flag.is_(True),
        (Category.pk_category_id == category.pk_category_id)
        | (Category.ancestor_path.like(f"{category.ancestor_path}{category.pk_category_id}/%")),
    )

    membership = select(ProductCategory.fk_product_id).where(
        ProductCategory.scd_active_flag.is_(True),
        ProductCategory.fk_category_id.in_(descendant_ids),
    )

    stmt = visible_products_stmt().where(Product.pk_product_id.in_(membership))

    if in_stock_only:
        sellable = (
            select(ProductVariant.fk_product_id)
            .join(
                StockLevel,
                StockLevel.fk_product_variant_id == ProductVariant.pk_product_variant_id,
            )
            .join(StockPool, StockPool.pk_stock_pool_id == StockLevel.fk_stock_pool_id)
            .where(
                ProductVariant.scd_active_flag.is_(True),
                StockLevel.scd_active_flag.is_(True),
                StockPool.is_sellable_flag.is_(True),
                StockLevel.quantity_on_hand > StockLevel.quantity_reserved,
            )
        )
        stmt = stmt.where(Product.pk_product_id.in_(sellable))

    # Count in SQL, never by materialising the rows (Part II §1).
    total = db.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ) or 0

    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    rows = db.scalars(
        apply_sort(stmt, sort, language).offset((page - 1) * page_size).limit(page_size)
    ).unique().all()

    return list(rows), total


def child_categories(db: Session, category: Category | None) -> list[Category]:
    parent_id = category.pk_category_id if category else None
    return list(
        db.scalars(
            select(Category)
            .where(
                Category.scd_active_flag.is_(True),
                Category.is_visible_flag.is_(True),
                Category.fk_parent_category_id.is_(parent_id)
                if parent_id is None
                else Category.fk_parent_category_id == parent_id,
            )
            .order_by(Category.sort_order, Category.pk_category_id)
        ).all()
    )


def breadcrumb_trail(db: Session, category: Category) -> list[Category]:
    """Ancestors, root first — read straight off ``ancestor_path``."""
    ids = [int(part) for part in category.ancestor_path.strip("/").split("/") if part]
    if not ids:
        return []
    ancestors = {
        c.pk_category_id: c
        for c in db.scalars(select(Category).where(Category.pk_category_id.in_(ids))).all()
    }
    return [ancestors[i] for i in ids if i in ancestors]
