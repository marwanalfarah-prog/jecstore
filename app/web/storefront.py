"""Storefront pages (Part I §4, §5, §15, §16).

Server-rendered HTML, per Part II §7.2: the bilingual SEO requirements in
Part I §16 are met by default this way, without an SSR layer bolted onto a
client-rendered app.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.context import get_context
from app.core.errors import NotFound
from app.core.templating import templates
from app.db.session import get_db
from app.models.catalog import (
    Category,
    Product,
    ProductAttributeValue,
    ProductCategory,
    ProductImage,
    ProductTag,
    ProductVariant,
    Publisher,
    Tag,
    UrlRedirect,
    VariantOptionValue,
)
from app.models.enums import AttributeVisibility
from app.services import catalog as catalog_service
from app.services import reviews as reviews_service
from app.services.catalog import (
    PAGE_SIZES,
    SORT_OPTIONS,
    availability_for_products,
    breadcrumb_trail,
    child_categories,
    normalize_page_size,
    normalize_sort,
    products_in_category,
    visible_products_stmt,
)
from app.services.homepage import build_homepage
from app.services.pricing import price_products, resolve_price
from app.services.pricing import live_discounts_for_products

router = APIRouter(tags=["storefront"])


# ---------------------------------------------------------------------------
# Homepage
# ---------------------------------------------------------------------------


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)) -> Response:
    return templates.TemplateResponse(
        request, "storefront/home.html", {"sections": build_homepage(db)}
    )


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@router.get("/categories")
def all_categories(request: Request, db: Session = Depends(get_db)) -> Response:
    return templates.TemplateResponse(
        request,
        "storefront/categories.html",
        {"categories": child_categories(db, None)},
    )


@router.get("/c/{category_id}")
@router.get("/c/{category_id}/{slug}")
def category_page(
    request: Request,
    category_id: int,
    slug: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    """A category listing.

    The id resolves the row and the slug is cosmetic (Part I §16), so a renamed
    category keeps working. A stale slug is 301'd to the current one rather than
    served at two URLs, which keeps the canonical clean for search engines.
    """
    ctx = get_context(request)
    category = db.scalars(
        select(Category).where(
            Category.pk_category_id == category_id,
            Category.scd_active_flag.is_(True),
            Category.is_visible_flag.is_(True),
        )
    ).first()
    if category is None:
        raise NotFound()

    canonical = catalog_service.category_url(category, ctx.language)
    if slug is not None and request.url.path != canonical:
        return RedirectResponse(canonical, status_code=301)

    sort = normalize_sort(request.query_params.get("sort"))
    page_size = normalize_page_size(request.query_params.get("per_page"))
    page = max(int(request.query_params.get("page", 1) or 1), 1)
    in_stock_only = request.query_params.get("in_stock") == "1"

    products, total = products_in_category(
        db,
        category,
        language=ctx.language,
        sort=sort,
        page=page,
        page_size=page_size,
        in_stock_only=in_stock_only,
    )

    total_pages = max((total + page_size - 1) // page_size, 1)

    return templates.TemplateResponse(
        request,
        "storefront/category.html",
        {
            "category": category,
            "trail": breadcrumb_trail(db, category),
            "children": child_categories(db, category),
            "products": products,
            "prices": price_products(db, products),
            "availability": availability_for_products(
                db, [p.pk_product_id for p in products]
            ),
            "ratings": reviews_service.rating_summaries(
                db, [p.pk_product_id for p in products]
            ),
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "page_window": _page_window(page, total_pages),
            "sort": sort,
            "sort_options": SORT_OPTIONS,
            "page_sizes": PAGE_SIZES,
            "in_stock_only": in_stock_only,
        },
    )


# ---------------------------------------------------------------------------
# Product detail
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VariantAxis:
    """One picker on the product page: an axis and the values in stock for it."""

    option: Any
    choices: list[Any]


@dataclass(slots=True)
class Specification:
    label: str
    value: str


@router.get("/p/{product_id}")
@router.get("/p/{product_id}/{slug}")
def product_page(
    request: Request,
    product_id: int,
    slug: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    ctx = get_context(request)

    product = db.scalars(
        select(Product)
        .where(
            Product.pk_product_id == product_id,
            Product.scd_active_flag.is_(True),
            Product.is_visible_flag.is_(True),
        )
        .options(
            selectinload(Product.publisher),
            selectinload(Product.variants).selectinload(ProductVariant.option_values),
            selectinload(Product.images),
        )
    ).first()
    if product is None:
        raise NotFound()

    canonical = catalog_service.product_url(product, ctx.language)
    if slug is not None and request.url.path != canonical:
        return RedirectResponse(canonical, status_code=301)

    discounts = live_discounts_for_products(db, [product.pk_product_id])
    price = resolve_price(
        product.base_price_amt,
        discounts.get(product.pk_product_id, []),
        product.discount_overlap_rule,
    )
    availability = availability_for_products(db, [product.pk_product_id])[product.pk_product_id]

    _record_product_view(db, request, product)

    related = _related_products(db, product)
    review_rows = reviews_service.approved_reviews(db, product.pk_product_id)

    return templates.TemplateResponse(
        request,
        "storefront/product.html",
        {
            "product": product,
            "price": price,
            "availability": availability,
            "trail": _product_trail(db, product),
            "gallery_urls": _gallery_urls(request, product),
            "variant_axes": _variant_axes(db, product),
            "selectable_variants": _selectable_variants(db, product),
            "default_variant": next(iter(_live_variants(product)), None),
            "specifications": _specifications(db, product, ctx.language),
            "tags": _tags(db, product),
            "reviews": review_rows,
            "reviewer_names": reviews_service.reviewer_names(db, review_rows),
            "rating": reviews_service.rating_summary(db, product.pk_product_id),
            "can_review": reviews_service.can_review(
                db, product_id=product.pk_product_id, user=ctx.user
            ),
            "my_review": (
                reviews_service.existing_review(
                    db, product_id=product.pk_product_id, user_id=ctx.user.pk_user_id
                )
                if ctx.user
                else None
            ),
            "review_flash": _review_flash(request),
            "rating_scale": range(reviews_service.MAX_RATING, 0, -1),
            "show_view_count": _counter_visible(ctx, product, "view"),
            "show_purchase_count": _counter_visible(ctx, product, "purchase"),
            "related": related,
            "related_prices": price_products(db, related),
            "related_availability": availability_for_products(
                db, [p.pk_product_id for p in related]
            ),
            "related_ratings": reviews_service.rating_summaries(
                db, [p.pk_product_id for p in related]
            ),
        },
    )


# ---------------------------------------------------------------------------
# Publisher and tag landing pages (Part I §5.6, §15)
# ---------------------------------------------------------------------------


@router.get("/publisher/{publisher_id}")
@router.get("/publisher/{publisher_id}/{slug}")
def publisher_page(
    request: Request,
    publisher_id: int,
    slug: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    publisher = db.scalars(
        select(Publisher).where(
            Publisher.pk_publisher_id == publisher_id,
            Publisher.scd_active_flag.is_(True),
        )
    ).first()
    if publisher is None:
        raise NotFound()

    # A retired slug 301s to the current one rather than serving the page under
    # both (Part I §16): the id resolves the page, and one canonical URL keeps
    # a search engine from reading two paths as duplicate content.
    canonical = catalog_service.publisher_url(publisher)
    if slug is not None and request.url.path != canonical:
        return RedirectResponse(canonical, status_code=301)

    ctx = get_context(request)
    sort = normalize_sort(request.query_params.get("sort"))
    stmt = catalog_service.apply_sort(
        visible_products_stmt().where(Product.fk_publisher_id == publisher_id),
        sort,
        ctx.language,
    )
    products = list(db.scalars(stmt.limit(100)).unique().all())

    return templates.TemplateResponse(
        request,
        "storefront/publisher.html",
        {
            "publisher": publisher,
            "products": products,
            "prices": price_products(db, products),
            "availability": availability_for_products(
                db, [p.pk_product_id for p in products]
            ),
            "ratings": reviews_service.rating_summaries(
                db, [p.pk_product_id for p in products]
            ),
            "sort": sort,
            "sort_options": SORT_OPTIONS,
        },
    )


@router.get("/tag/{tag_id}")
@router.get("/tag/{tag_id}/{slug}")
def tag_page(
    request: Request, tag_id: int, slug: str | None = None, db: Session = Depends(get_db)
) -> Response:
    tag = db.scalars(
        select(Tag).where(Tag.pk_tag_id == tag_id, Tag.scd_active_flag.is_(True))
    ).first()
    if tag is None:
        raise NotFound()

    canonical = catalog_service.tag_url(tag)
    if slug is not None and request.url.path != canonical:
        return RedirectResponse(canonical, status_code=301)

    tagged = select(ProductTag.fk_product_id).where(
        ProductTag.fk_tag_id == tag_id, ProductTag.scd_active_flag.is_(True)
    )
    products = list(
        db.scalars(visible_products_stmt().where(Product.pk_product_id.in_(tagged)).limit(100))
        .unique()
        .all()
    )

    return templates.TemplateResponse(
        request,
        "storefront/tag.html",
        {
            "tag": tag,
            "products": products,
            "prices": price_products(db, products),
            "availability": availability_for_products(
                db, [p.pk_product_id for p in products]
            ),
            "ratings": reviews_service.rating_summaries(
                db, [p.pk_product_id for p in products]
            ),
        },
    )


# ---------------------------------------------------------------------------
# Legacy URL redirects (Part I §16)
# ---------------------------------------------------------------------------


#: Its own router because it must be included *after* every other route —
#: a catch-all registered early would swallow /auth/login and friends.
legacy_router = APIRouter(include_in_schema=False)


@legacy_router.get("/{legacy_path:path}")
def legacy_redirect(
    request: Request, legacy_path: str, db: Session = Depends(get_db)
) -> Response:
    """Last-resort 301 for a retired slug, so an old shared link never dies.

    Included last in ``app/main.py``, so it only sees paths nothing else claimed.
    """
    redirect = db.scalars(
        select(UrlRedirect).where(
            UrlRedirect.old_path == f"/{legacy_path}",
            UrlRedirect.scd_active_flag.is_(True),
        )
    ).first()
    if redirect is None:
        raise NotFound()

    redirect.hit_count += 1
    db.commit()
    return RedirectResponse(redirect.new_path, status_code=301)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _page_window(page: int, total_pages: int, span: int = 2) -> list[int]:
    """Page numbers around the current one — never the full range, which on a
    large category would render hundreds of links."""
    start = max(1, page - span)
    end = min(total_pages, page + span)
    return list(range(start, end + 1))


def _product_trail(db: Session, product: Product) -> list[Category]:
    """Breadcrumb via the product's primary category, falling back to any."""
    link = db.scalars(
        select(ProductCategory)
        .where(
            ProductCategory.fk_product_id == product.pk_product_id,
            ProductCategory.scd_active_flag.is_(True),
        )
        .order_by(ProductCategory.is_primary_flag.desc())
    ).first()
    if link is None:
        return []
    category = db.get(Category, link.fk_category_id)
    if category is None:
        return []
    return [*breadcrumb_trail(db, category), category]


def _gallery_urls(request: Request, product: Product) -> list[str]:
    from app.core.config import settings

    def to_url(path: str) -> str:
        if path.startswith(("http://", "https://", "/")):
            return path
        return f"{settings.media_url.rstrip('/')}/{path.lstrip('/')}"

    urls: list[str] = []
    if product.main_image_path:
        urls.append(to_url(product.main_image_path))
    for image in sorted(product.images, key=lambda i: i.sort_order):
        if image.scd_active_flag and image.image_path:
            url = to_url(image.image_path)
            if url not in urls:
                urls.append(url)
    return urls


def _live_variants(product: Product) -> list[ProductVariant]:
    """Variants a shopper may actually be offered.

    Two different flags have to agree, and missing either one leaks stock that
    should be gone: ``scd_active_flag`` is the SCD row state — false once staff
    retire the variant (nothing is deleted, Part II §6) — while
    ``is_active_flag`` is the shopkeeper's own "not for sale right now" switch.
    """
    return [
        variant
        for variant in product.variants
        if variant.scd_active_flag and variant.is_active_flag
    ]


def _selectable_variants(db: Session, product: Product) -> list[ProductVariant]:
    """The plain list of variants to offer when there is no option matrix.

    Most of this shop's products vary on one informal axis — a colour, a cover,
    a size — entered as a free-text label rather than modelled as a structured
    colour × size matrix (Part I §5.4). Without this, such a product renders no
    picker at all and every shopper silently receives the first variant.

    Returns nothing when the axes already cover it, or when there is only one
    variant: a choice of one is not a choice.
    """
    if _variant_axes(db, product):
        return []
    active = _live_variants(product)
    return active if len(active) > 1 else []


def _variant_axes(db: Session, product: Product) -> list[VariantAxis]:
    """Build one picker per axis this product actually varies on.

    Derived from the variants that exist rather than from the full option
    catalog, so a product that only comes in one colour shows no colour picker.
    """
    variant_ids = [v.pk_product_variant_id for v in _live_variants(product)]
    if not variant_ids:
        return []

    values = db.scalars(
        select(VariantOptionValue)
        .where(
            VariantOptionValue.fk_product_variant_id.in_(variant_ids),
            VariantOptionValue.scd_active_flag.is_(True),
        )
        .options(
            selectinload(VariantOptionValue.option),
            selectinload(VariantOptionValue.choice),
        )
    ).all()

    axes: dict[int, VariantAxis] = {}
    seen_choices: dict[int, set[int]] = {}
    for value in values:
        axis = axes.setdefault(
            value.fk_variant_option_id, VariantAxis(option=value.option, choices=[])
        )
        seen = seen_choices.setdefault(value.fk_variant_option_id, set())
        if value.fk_variant_option_choice_id not in seen:
            seen.add(value.fk_variant_option_choice_id)
            axis.choices.append(value.choice)

    for axis in axes.values():
        axis.choices.sort(key=lambda c: (c.sort_order, c.pk_variant_option_choice_id))

    return [axis for axis in axes.values() if len(axis.choices) > 1]


def _specifications(db: Session, product: Product, language: str) -> list[Specification]:
    """Public specs only.

    Admin-only attributes — "Shelf Number" and the like — are filtered out here
    rather than in the template, so a template change can never leak one
    (Part I §5.2, §5.3).
    """
    rows = db.scalars(
        select(ProductAttributeValue)
        .where(
            ProductAttributeValue.fk_product_id == product.pk_product_id,
            ProductAttributeValue.fk_product_variant_id.is_(None),
            ProductAttributeValue.scd_active_flag.is_(True),
        )
        .options(
            selectinload(ProductAttributeValue.attribute),
            selectinload(ProductAttributeValue.choice),
        )
    ).all()

    specs: list[Specification] = []
    for row in rows:
        attribute = row.attribute
        if attribute is None or attribute.visibility != AttributeVisibility.PUBLIC.value:
            continue
        label = attribute.name_ar if language == "ar" else attribute.name_en
        if row.choice is not None:
            value = row.choice.value_ar if language == "ar" else row.choice.value_en
        else:
            value = row.value_ar if language == "ar" else row.value_en
        if value:
            specs.append(Specification(label=label or "", value=value))

    # Built-in fields the customer may see. Cost and quantity are never here.
    if product.isbn:
        specs.append(Specification(label="ISBN", value=product.isbn))
    if product.weight_grams:
        specs.append(Specification(label="Weight (g)", value=str(product.weight_grams)))

    return specs


def _tags(db: Session, product: Product) -> list[Tag]:
    return list(
        db.scalars(
            select(Tag)
            .join(ProductTag, ProductTag.fk_tag_id == Tag.pk_tag_id)
            .where(
                ProductTag.fk_product_id == product.pk_product_id,
                ProductTag.scd_active_flag.is_(True),
                Tag.scd_active_flag.is_(True),
            )
        ).all()
    )


def _review_flash(request: Request) -> str | None:
    """The outcome of a just-submitted review, from the redirect.

    Validated against a closed set: the value comes off the query string and is
    turned into a message shown to the customer.
    """
    from app.web.commerce import REVIEW_FLASHES

    value = request.query_params.get("review")
    return value if value in REVIEW_FLASHES else None


def _counter_visible(ctx: Any, product: Product, which: str) -> bool:
    """Storewide default with a per-product override (Part I §5.3)."""
    override = (
        product.show_view_count_flag if which == "view" else product.show_purchase_count_flag
    )
    if override is not None:
        return override
    key = "show_view_count" if which == "view" else "show_purchase_count"
    return bool(ctx.site_settings.get(key))


def _related_products(db: Session, product: Product, limit: int = 6) -> list[Product]:
    """Same categories, most purchased first — a useful default without a
    recommendation engine."""
    category_ids = select(ProductCategory.fk_category_id).where(
        ProductCategory.fk_product_id == product.pk_product_id,
        ProductCategory.scd_active_flag.is_(True),
    )
    siblings = select(ProductCategory.fk_product_id).where(
        ProductCategory.fk_category_id.in_(category_ids),
        ProductCategory.scd_active_flag.is_(True),
    )
    return list(
        db.scalars(
            visible_products_stmt()
            .where(
                Product.pk_product_id.in_(siblings),
                Product.pk_product_id != product.pk_product_id,
            )
            .order_by(Product.purchase_count.desc())
            .limit(limit)
        )
        .unique()
        .all()
    )


def _record_product_view(db: Session, request: Request, product: Product) -> None:
    """Bump the counter and append the auditable detail row (Part I §5.2, §2.8)."""
    from app.models.catalog import ProductViewEvent
    from app.services.activity import record_event
    from app.models.enums import ActivityEvent

    ctx = get_context(request)
    product.view_count += 1
    db.add(
        ProductViewEvent(
            fk_product_id=product.pk_product_id,
            user_id=ctx.user.pk_user_id if ctx.user else None,
            session_key=ctx.session_key,
            ip_address=request.client.host if request.client else None,
        )
    )
    record_event(
        db,
        ActivityEvent.PRODUCT_VIEW,
        request=request,
        context=ctx,
        target_table="scd_product",
        target_row_id=product.pk_product_id,
    )
    db.commit()
