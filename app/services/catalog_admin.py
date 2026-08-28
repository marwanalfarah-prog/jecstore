"""Admin catalog operations (Part I sections 5, 13, and 14).

The storefront-facing catalog service is deliberately read-heavy. This module is
the write side used by the admin panel and by maker-checker replay handlers.
It keeps entity ids stable for products and categories because those ids are
embedded in URLs and foreign keys; closing is used for retirement/deletion-style
actions, not for ordinary field edits.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import CategoryNotEmpty, Conflict, NotFound, ValidationFailed
from app.db.base import utcnow
from app.models.catalog import (
    Category,
    Discount,
    Product,
    ProductAttribute,
    ProductAttributeChoice,
    ProductAttributeValue,
    ProductCategory,
    ProductImage,
    ProductReview,
    ProductTag,
    ProductVariant,
    Publisher,
    Tag,
    VariantOptionValue,
)
from app.models.enums import (
    AttributeInputType,
    AttributeVisibility,
    DiscountKind,
    DiscountScope,
    OverlapRule,
    ReviewStatus,
)
from app.services.catalog import slugify
from app.services.pricing import q


@dataclass(slots=True)
class ProductAdminRow:
    product: Product
    variant_count: int
    primary_category: Category | None


@dataclass(slots=True)
class ProductAdminPage:
    """One page of :func:`active_products`, and how many rows it was cut from."""

    rows: list[ProductAdminRow]
    total: int
    page: int
    per_page: int

    @property
    def total_pages(self) -> int:
        return max(1, -(-self.total // self.per_page))


def active_categories(db: Session) -> list[Category]:
    return list(
        db.scalars(
            select(Category)
            .where(Category.scd_active_flag.is_(True))
            .order_by(Category.depth, Category.sort_order, Category.name_en)
        ).all()
    )


def category_product_counts(db: Session) -> dict[int, int]:
    return {
        category_id: int(count)
        for category_id, count in db.execute(
            select(ProductCategory.fk_category_id, func.count())
            .where(ProductCategory.scd_active_flag.is_(True))
            .group_by(ProductCategory.fk_category_id)
        ).all()
    }


def active_publishers(db: Session) -> list[Publisher]:
    return list(
        db.scalars(
            select(Publisher)
            .where(Publisher.scd_active_flag.is_(True))
            .order_by(Publisher.sort_order, Publisher.name_en)
        ).all()
    )


def active_products(
    db: Session,
    *,
    search: str | None = None,
    category_id: int | None = None,
    visible: bool | None = None,
    page: int = 1,
    per_page: int = 25,
) -> ProductAdminPage:
    """One page of the catalog, plus the unfiltered total behind it.

    Paged rather than "all of it": this read used to return every active
    product, so the admin list grew a row per book forever and the screen got
    slower with every shipment. Part II §2 rules that out, and staff cannot
    scan four thousand rows anyway. The count comes back alongside so the
    screen can say how many matched rather than how many fitted.
    """
    filters = [Product.scd_active_flag.is_(True)]

    stmt = (
        select(Product)
        .options(selectinload(Product.publisher), selectinload(Product.category_links))
        .order_by(Product.pk_product_id.desc())
    )
    if search:
        like = f"%{search.strip().lower()}%"
        raw_like = f"%{search.strip()}%"
        filters.append(
            or_(
                func.lower(Product.name_en).like(like),
                Product.name_ar.like(raw_like),
                func.lower(Product.isbn).like(like),
                func.lower(Product.slug_en).like(like),
            )
        )
    if visible is not None:
        filters.append(Product.is_visible_flag.is_(visible))

    count_stmt = select(func.count()).select_from(Product)
    if category_id is not None:
        join = (
            ProductCategory,
            ProductCategory.fk_product_id == Product.pk_product_id,
        )
        stmt = stmt.join(*join)
        count_stmt = count_stmt.join(*join)
        filters.extend(
            [
                ProductCategory.fk_category_id == category_id,
                ProductCategory.scd_active_flag.is_(True),
            ]
        )

    total = db.scalar(count_stmt.where(*filters)) or 0
    page = max(page, 1)
    products = list(
        db.scalars(
            stmt.where(*filters).offset((page - 1) * per_page).limit(per_page)
        ).all()
    )
    if not products:
        return ProductAdminPage(rows=[], total=total, page=page, per_page=per_page)

    product_ids = [product.pk_product_id for product in products]
    variant_counts = {
        product_id: int(count)
        for product_id, count in db.execute(
            select(ProductVariant.fk_product_id, func.count())
            .where(
                ProductVariant.fk_product_id.in_(product_ids),
                ProductVariant.scd_active_flag.is_(True),
            )
            .group_by(ProductVariant.fk_product_id)
        ).all()
    }

    primary_links = {
        link.fk_product_id: link.fk_category_id
        for link in db.scalars(
            select(ProductCategory).where(
                ProductCategory.fk_product_id.in_(product_ids),
                ProductCategory.is_primary_flag.is_(True),
                ProductCategory.scd_active_flag.is_(True),
            )
        ).all()
    }
    categories = {
        category.pk_category_id: category
        for category in db.scalars(
            select(Category).where(Category.scd_active_flag.is_(True))
        ).all()
    }

    return ProductAdminPage(
        rows=[
            ProductAdminRow(
                product=product,
                variant_count=variant_counts.get(product.pk_product_id, 0),
                primary_category=categories.get(primary_links.get(product.pk_product_id)),
            )
            for product in products
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


def product_options(db: Session, *, limit: int = 500) -> list[Product]:
    """Products for a ``<select>``, newest first and bounded.

    Separate from :func:`active_products` on purpose. That one pages, because a
    list screen should; a picker cannot page, so it takes a hard cap instead and
    the screens that use it also offer a typed id (§17.4 replaces the old
    seventy-item unstyled dropdown with something searchable). Without the
    split, paging the list screen would have silently cut every picker in the
    panel down to one page of options.
    """
    return list(
        db.scalars(
            select(Product)
            .where(Product.scd_active_flag.is_(True))
            .order_by(Product.pk_product_id.desc())
            .limit(limit)
        ).all()
    )


def product_detail(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None or not product.scd_active_flag:
        raise NotFound("That product does not exist.")
    return product


def product_variants(db: Session, product_id: int) -> list[ProductVariant]:
    return list(
        db.scalars(
            select(ProductVariant)
            .where(
                ProductVariant.fk_product_id == product_id,
                ProductVariant.scd_active_flag.is_(True),
            )
            .order_by(ProductVariant.sort_order, ProductVariant.pk_product_variant_id)
        ).all()
    )


def product_images(db: Session, product_id: int) -> list[ProductImage]:
    """The product's gallery, in display order (Part I §5.2).

    Variant-scoped images are included: §5.4 hangs an optional gallery off each
    variant on top of the product's own, and the admin screen shows both so
    staff can see everything attached to the product in one place.
    """
    return list(
        db.scalars(
            select(ProductImage)
            .where(
                ProductImage.fk_product_id == product_id,
                ProductImage.scd_active_flag.is_(True),
            )
            .order_by(ProductImage.sort_order, ProductImage.pk_product_image_id)
        ).all()
    )


def product_discounts(db: Session, product_id: int) -> list[Discount]:
    return list(
        db.scalars(
            select(Discount)
            .where(
                Discount.fk_product_id == product_id,
                Discount.scd_active_flag.is_(True),
            )
            .order_by(Discount.starts_dt.desc().nulls_last(), Discount.pk_discount_id.desc())
        ).all()
    )


@dataclass(slots=True)
class ReviewAdminRow:
    """A review with the two things a moderator needs beside it.

    The screen listed `#{fk_product_id}` and nothing about the reviewer, so
    judging a review meant opening the product in another tab to see what was
    being reviewed, and there was no way at all to see whether the person had
    actually bought it — which §14 already records.
    """

    review: ProductReview
    product: Product | None
    reviewer: "object | None"


def active_reviews(
    db: Session, *, status: str | None = None, limit: int = 100
) -> list[ReviewAdminRow]:
    from app.models.identity import User

    stmt = (
        select(ProductReview)
        .where(ProductReview.scd_active_flag.is_(True))
        .order_by(ProductReview.submitted_dt.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(ProductReview.status == status)
    reviews = list(db.scalars(stmt).all())
    if not reviews:
        return []

    products = {
        product.pk_product_id: product
        for product in db.scalars(
            select(Product).where(
                Product.pk_product_id.in_({r.fk_product_id for r in reviews})
            )
        ).all()
    }
    reviewers = {
        user.pk_user_id: user
        for user in db.scalars(
            select(User).where(User.pk_user_id.in_({r.fk_user_id for r in reviews}))
        ).all()
    }
    return [
        ReviewAdminRow(
            review=review,
            product=products.get(review.fk_product_id),
            reviewer=reviewers.get(review.fk_user_id),
        )
        for review in reviews
    ]


def create_category(
    db: Session,
    *,
    name_ar: str,
    name_en: str,
    parent_category_id: int | None = None,
    slug_ar: str | None = None,
    slug_en: str | None = None,
    description_ar: str | None = None,
    description_en: str | None = None,
    image_path: str | None = None,
    sort_order: int = 0,
    is_visible: bool = True,
    actor_user_id: int | None = None,
) -> Category:
    if not name_ar.strip() or not name_en.strip():
        raise ValidationFailed("Category names are required in both languages.")
    parent = _active_category(db, parent_category_id) if parent_category_id else None
    now = utcnow()
    category = Category(
        fk_parent_category_id=parent.pk_category_id if parent else None,
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        slug_ar=_slug(slug_ar, name_ar),
        slug_en=_slug(slug_en, name_en),
        description_ar=_blank(description_ar),
        description_en=_blank(description_en),
        image_path=_blank(image_path),
        sort_order=sort_order,
        is_visible_flag=is_visible,
        scd_active_from=now,
        scd_changed_by=actor_user_id,
    )
    _set_category_path(category, parent)
    db.add(category)
    db.flush()
    return category


def update_category(
    db: Session,
    *,
    category_id: int,
    name_ar: str,
    name_en: str,
    parent_category_id: int | None = None,
    slug_ar: str | None = None,
    slug_en: str | None = None,
    description_ar: str | None = None,
    description_en: str | None = None,
    image_path: str | None = None,
    sort_order: int = 0,
    is_visible: bool = True,
    actor_user_id: int | None = None,
) -> Category:
    category = _active_category(db, category_id)
    parent = _active_category(db, parent_category_id) if parent_category_id else None
    if parent and (
        parent.pk_category_id == category.pk_category_id
        or f"/{category.pk_category_id}/" in parent.ancestor_path
    ):
        raise ValidationFailed("A category cannot be nested under itself.")
    if not name_ar.strip() or not name_en.strip():
        raise ValidationFailed("Category names are required in both languages.")

    category.fk_parent_category_id = parent.pk_category_id if parent else None
    category.name_ar = name_ar.strip()
    category.name_en = name_en.strip()
    category.slug_ar = _slug(slug_ar, name_ar)
    category.slug_en = _slug(slug_en, name_en)
    category.description_ar = _blank(description_ar)
    category.description_en = _blank(description_en)
    category.image_path = _blank(image_path)
    category.sort_order = sort_order
    category.is_visible_flag = is_visible
    category.scd_changed_by = actor_user_id
    _set_category_path(category, parent)
    db.flush()
    _rebuild_child_paths(db, category, actor_user_id)
    db.flush()
    return category


def close_category(
    db: Session,
    *,
    category_id: int,
    actor_user_id: int | None = None,
) -> Category:
    category = _active_category(db, category_id)
    child_count = db.scalar(
        select(func.count())
        .select_from(Category)
        .where(
            Category.fk_parent_category_id == category_id,
            Category.scd_active_flag.is_(True),
        )
    ) or 0
    product_count = db.scalar(
        select(func.count())
        .select_from(ProductCategory)
        .where(
            ProductCategory.fk_category_id == category_id,
            ProductCategory.scd_active_flag.is_(True),
        )
    ) or 0
    if child_count or product_count:
        raise CategoryNotEmpty()
    category.close(changed_by=actor_user_id)
    db.flush()
    return category


def create_publisher(
    db: Session,
    *,
    name_ar: str,
    name_en: str,
    slug: str | None = None,
    logo_path: str | None = None,
    description_ar: str | None = None,
    description_en: str | None = None,
    show_on_homepage: bool = True,
    sort_order: int = 0,
    actor_user_id: int | None = None,
) -> Publisher:
    if not name_ar.strip() or not name_en.strip():
        raise ValidationFailed("Publisher names are required in both languages.")
    normalized_slug = _slug(slug, name_en)
    _assert_unique_publisher_slug(db, normalized_slug)
    publisher = Publisher(
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        slug=normalized_slug,
        logo_path=_blank(logo_path),
        description_ar=_blank(description_ar),
        description_en=_blank(description_en),
        show_on_homepage_flag=show_on_homepage,
        sort_order=sort_order,
        scd_active_from=utcnow(),
        scd_changed_by=actor_user_id,
    )
    db.add(publisher)
    db.flush()
    return publisher


def update_publisher(
    db: Session,
    *,
    publisher_id: int,
    name_ar: str,
    name_en: str,
    slug: str | None = None,
    logo_path: str | None = None,
    description_ar: str | None = None,
    description_en: str | None = None,
    show_on_homepage: bool = True,
    sort_order: int = 0,
    actor_user_id: int | None = None,
) -> Publisher:
    publisher = _active_publisher(db, publisher_id)
    normalized_slug = _slug(slug, name_en)
    _assert_unique_publisher_slug(db, normalized_slug, exclude_id=publisher_id)
    publisher.name_ar = name_ar.strip()
    publisher.name_en = name_en.strip()
    publisher.slug = normalized_slug
    publisher.logo_path = _blank(logo_path)
    publisher.description_ar = _blank(description_ar)
    publisher.description_en = _blank(description_en)
    publisher.show_on_homepage_flag = show_on_homepage
    publisher.sort_order = sort_order
    publisher.scd_changed_by = actor_user_id
    db.flush()
    return publisher


# ---------------------------------------------------------------------------
# Tags (Part I §15)
# ---------------------------------------------------------------------------
#
# The data model, the /tag/{id} landing page and the search index all read tags
# and always have. Nothing *wrote* them: `lkp_tag` shipped empty, so §15's tag
# pages existed and were unreachable, and the tag terms that
# `search.build_index_text()` folds into every product's search projection were
# always the empty set. These four functions and the screen over them are what
# make the rest of it real.


@dataclass(slots=True)
class TagAdminRow:
    tag: Tag
    product_count: int


def active_tags(db: Session) -> list[TagAdminRow]:
    """Every live tag, with how many products carry it.

    The count is the whole point of the list: a tag on nothing is either a typo
    or a job somebody left half-done, and either way staff need to see which.
    """
    tags = list(
        db.scalars(
            select(Tag).where(Tag.scd_active_flag.is_(True)).order_by(Tag.name_en)
        ).all()
    )
    if not tags:
        return []

    counts = {
        tag_id: int(count)
        for tag_id, count in db.execute(
            select(ProductTag.fk_tag_id, func.count())
            .where(
                ProductTag.fk_tag_id.in_([tag.pk_tag_id for tag in tags]),
                ProductTag.scd_active_flag.is_(True),
            )
            .group_by(ProductTag.fk_tag_id)
        ).all()
    }
    return [
        TagAdminRow(tag=tag, product_count=counts.get(tag.pk_tag_id, 0))
        for tag in tags
    ]


def create_tag(
    db: Session,
    *,
    name_ar: str,
    name_en: str,
    slug: str | None = None,
    actor_user_id: int | None = None,
) -> Tag:
    if not name_ar.strip() or not name_en.strip():
        raise ValidationFailed("Tag names are required in both languages.")
    normalized = _slug(slug, name_en)
    _assert_unique_tag_slug(db, normalized)
    tag = Tag(
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        slug=normalized,
        scd_active_from=utcnow(),
        scd_changed_by=actor_user_id,
    )
    db.add(tag)
    db.flush()
    return tag


def update_tag(
    db: Session,
    *,
    tag_id: int,
    name_ar: str,
    name_en: str,
    slug: str | None = None,
    actor_user_id: int | None = None,
) -> Tag:
    """Rename in place.

    The slug is part of a public URL, so changing it retires the old address.
    §16 answers that for products and categories with a 301; a tag page is not
    in the sitemap and carries no inbound links of its own, so a rename simply
    moves it.
    """
    tag = _active_tag(db, tag_id)
    normalized = _slug(slug, name_en)
    _assert_unique_tag_slug(db, normalized, exclude_id=tag_id)
    tag.name_ar = name_ar.strip()
    tag.name_en = name_en.strip()
    tag.slug = normalized
    tag.scd_changed_by = actor_user_id
    db.flush()
    _reindex_tagged(db, tag_id)
    return tag


def close_tag(db: Session, *, tag_id: int, actor_user_id: int | None = None) -> Tag:
    """Retire a tag and detach it from every product.

    Closing the tag alone would leave live `lkp_product_tag` rows pointing at a
    dead tag: the join in `search.build_index_text()` filters on both flags, so
    the search index would quietly recover, but `/tag/{id}` would 404 while the
    product page still listed the tag. Both sides close together.
    """
    tag = _active_tag(db, tag_id)
    links = db.scalars(
        select(ProductTag).where(
            ProductTag.fk_tag_id == tag_id,
            ProductTag.scd_active_flag.is_(True),
        )
    ).all()
    for link in links:
        link.close(changed_by=actor_user_id)
    tag.close(changed_by=actor_user_id)
    db.flush()
    _reindex_tagged(db, tag_id)
    return tag


def product_tag_ids(db: Session, product_id: int) -> set[int]:
    return set(
        db.scalars(
            select(ProductTag.fk_tag_id).where(
                ProductTag.fk_product_id == product_id,
                ProductTag.scd_active_flag.is_(True),
            )
        ).all()
    )


def set_product_tags(
    db: Session,
    *,
    product_id: int,
    tag_ids: list[int],
    actor_user_id: int | None = None,
) -> list[int]:
    """Replace a product's tags with exactly ``tag_ids``.

    Diffed rather than cleared-and-rewritten: closing and reopening an unchanged
    link would churn history for nothing, and Part II §1's SCD rows are meant to
    record real changes.
    """
    wanted = {int(tag_id) for tag_id in tag_ids}
    if wanted:
        live = set(
            db.scalars(
                select(Tag.pk_tag_id).where(
                    Tag.pk_tag_id.in_(wanted), Tag.scd_active_flag.is_(True)
                )
            ).all()
        )
        missing = wanted - live
        if missing:
            raise NotFound("One of those tags does not exist.")

    existing = {
        link.fk_tag_id: link
        for link in db.scalars(
            select(ProductTag).where(
                ProductTag.fk_product_id == product_id,
                ProductTag.scd_active_flag.is_(True),
            )
        ).all()
    }

    now = utcnow()
    for tag_id, link in existing.items():
        if tag_id not in wanted:
            link.close(changed_by=actor_user_id, at=now)
    for tag_id in wanted - set(existing):
        db.add(
            ProductTag(
                fk_product_id=product_id,
                fk_tag_id=tag_id,
                scd_active_from=now,
                scd_changed_by=actor_user_id,
            )
        )
    db.flush()

    # Tags feed the search projection, so a product whose tags changed has a
    # stale index until it is rebuilt (Part II §1's sanctioned exception).
    from app.services import search

    product = db.get(Product, product_id)
    if product is not None:
        search.reindex_product(db, product)
        db.flush()
    return sorted(wanted)


def _reindex_tagged(db: Session, tag_id: int) -> None:
    """Rebuild the search projection of every product that carried a tag."""
    from app.services import search

    product_ids = db.scalars(
        select(ProductTag.fk_product_id).where(ProductTag.fk_tag_id == tag_id)
    ).all()
    if not product_ids:
        return
    for product in db.scalars(
        select(Product).where(Product.pk_product_id.in_(set(product_ids)))
    ).all():
        search.reindex_product(db, product)
    db.flush()


def _active_tag(db: Session, tag_id: int) -> Tag:
    tag = db.get(Tag, tag_id)
    if tag is None or not tag.scd_active_flag:
        raise NotFound("That tag does not exist.")
    return tag


def _assert_unique_tag_slug(
    db: Session, slug: str, *, exclude_id: int | None = None
) -> None:
    stmt = select(Tag).where(Tag.slug == slug, Tag.scd_active_flag.is_(True))
    if exclude_id is not None:
        stmt = stmt.where(Tag.pk_tag_id != exclude_id)
    if db.scalars(stmt).first() is not None:
        raise Conflict("That tag slug is already in use.")


# ---------------------------------------------------------------------------
# Custom product details (Part I section 5.2)
# ---------------------------------------------------------------------------
#
# Definitions live once in lkp_product_attribute. The value row is the only
# "this detail applies here" marker, which keeps products from storing a
# second, derivable list of selected details.


@dataclass(slots=True)
class AttributeAdminRow:
    attribute: ProductAttribute
    choices: list[ProductAttributeChoice]
    usage_count: int


def active_attributes(db: Session) -> list[AttributeAdminRow]:
    """The reusable detail library, with dropdown choices and live usage."""
    attributes = list(
        db.scalars(
            select(ProductAttribute)
            .where(ProductAttribute.scd_active_flag.is_(True))
            .order_by(ProductAttribute.sort_order, ProductAttribute.name_en)
        ).all()
    )
    if not attributes:
        return []

    attribute_ids = [item.pk_product_attribute_id for item in attributes]
    choices: dict[int, list[ProductAttributeChoice]] = {}
    for choice in db.scalars(
        select(ProductAttributeChoice)
        .where(
            ProductAttributeChoice.fk_product_attribute_id.in_(attribute_ids),
            ProductAttributeChoice.scd_active_flag.is_(True),
        )
        .order_by(
            ProductAttributeChoice.fk_product_attribute_id,
            ProductAttributeChoice.sort_order,
            ProductAttributeChoice.pk_product_attribute_choice_id,
        )
    ).all():
        choices.setdefault(choice.fk_product_attribute_id, []).append(choice)

    counts = {
        attribute_id: int(count)
        for attribute_id, count in db.execute(
            select(ProductAttributeValue.fk_product_attribute_id, func.count())
            .where(
                ProductAttributeValue.fk_product_attribute_id.in_(attribute_ids),
                ProductAttributeValue.scd_active_flag.is_(True),
            )
            .group_by(ProductAttributeValue.fk_product_attribute_id)
        ).all()
    }

    return [
        AttributeAdminRow(
            attribute=attribute,
            choices=choices.get(attribute.pk_product_attribute_id, []),
            usage_count=counts.get(attribute.pk_product_attribute_id, 0),
        )
        for attribute in attributes
    ]


def create_attribute(
    db: Session,
    *,
    name_ar: str,
    name_en: str,
    input_type: str = AttributeInputType.TEXT,
    visibility: str = AttributeVisibility.PUBLIC,
    attribute_code: str | None = None,
    is_filterable: bool = False,
    is_comparable: bool = True,
    sort_order: int = 0,
    choices: list[tuple[str, str]] | None = None,
    actor_user_id: int | None = None,
) -> ProductAttribute:
    """Define a reusable detail such as Dimensions or Shelf number."""
    input_type = str(input_type)
    visibility = str(visibility)
    if not name_ar.strip() or not name_en.strip():
        raise ValidationFailed("Detail names are required in both languages.")
    if input_type not in {item.value for item in AttributeInputType}:
        raise ValidationFailed("Choose a valid detail input type.")
    if visibility not in {item.value for item in AttributeVisibility}:
        raise ValidationFailed("Choose a valid detail visibility.")

    code = _slug(attribute_code, name_en).replace("-", "_")
    _assert_unique_attribute_code(db, code)

    attribute = ProductAttribute(
        attribute_code=code,
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        input_type=input_type,
        visibility=visibility,
        is_filterable_flag=is_filterable,
        is_comparable_flag=is_comparable,
        sort_order=sort_order,
        scd_active_from=utcnow(),
        scd_changed_by=actor_user_id,
    )
    db.add(attribute)
    db.flush()

    if input_type == AttributeInputType.DROPDOWN.value:
        set_attribute_choices(
            db,
            attribute_id=attribute.pk_product_attribute_id,
            choices=choices or [],
            actor_user_id=actor_user_id,
        )
    return attribute


def update_attribute(
    db: Session,
    *,
    attribute_id: int,
    name_ar: str,
    name_en: str,
    visibility: str,
    is_filterable: bool = False,
    is_comparable: bool = True,
    sort_order: int = 0,
    choices: list[tuple[str, str]] | None = None,
    actor_user_id: int | None = None,
) -> ProductAttribute:
    """Rename or re-scope a detail without changing its stored value shape."""
    visibility = str(visibility)
    attribute = _active_attribute(db, attribute_id)
    if not name_ar.strip() or not name_en.strip():
        raise ValidationFailed("Detail names are required in both languages.")
    if visibility not in {item.value for item in AttributeVisibility}:
        raise ValidationFailed("Choose a valid detail visibility.")

    attribute.name_ar = name_ar.strip()
    attribute.name_en = name_en.strip()
    attribute.visibility = visibility
    attribute.is_filterable_flag = is_filterable
    attribute.is_comparable_flag = is_comparable
    attribute.sort_order = sort_order
    attribute.scd_changed_by = actor_user_id

    if attribute.input_type == AttributeInputType.DROPDOWN.value:
        set_attribute_choices(
            db,
            attribute_id=attribute_id,
            choices=choices or [],
            actor_user_id=actor_user_id,
        )
    db.flush()
    return attribute


def set_attribute_choices(
    db: Session,
    *,
    attribute_id: int,
    choices: list[tuple[str, str]],
    actor_user_id: int | None = None,
) -> list[ProductAttributeChoice]:
    """Replace dropdown choices by diffing, not by rewriting unchanged rows."""
    attribute = _active_attribute(db, attribute_id)
    if attribute.input_type != AttributeInputType.DROPDOWN.value:
        raise ValidationFailed("Only a dropdown detail has choices.")

    existing = {
        choice.value_en: choice
        for choice in db.scalars(
            select(ProductAttributeChoice).where(
                ProductAttributeChoice.fk_product_attribute_id == attribute_id,
                ProductAttributeChoice.scd_active_flag.is_(True),
            )
        ).all()
    }
    wanted = [
        (value_ar.strip(), value_en.strip())
        for value_ar, value_en in choices
        if value_ar.strip() and value_en.strip()
    ]
    if not wanted:
        raise ValidationFailed("A dropdown detail needs at least one choice.")

    now = utcnow()
    kept: list[ProductAttributeChoice] = []
    for sort_order, (value_ar, value_en) in enumerate(wanted):
        choice = existing.pop(value_en, None)
        if choice is None:
            choice = ProductAttributeChoice(
                fk_product_attribute_id=attribute_id,
                value_ar=value_ar,
                value_en=value_en,
                sort_order=sort_order,
                scd_active_from=now,
                scd_changed_by=actor_user_id,
            )
            db.add(choice)
        else:
            choice.value_ar = value_ar
            choice.sort_order = sort_order
            choice.scd_changed_by = actor_user_id
        kept.append(choice)

    for removed in existing.values():
        removed.close(changed_by=actor_user_id, at=now)
    db.flush()
    return kept


def close_attribute(
    db: Session,
    *,
    attribute_id: int,
    actor_user_id: int | None = None,
) -> ProductAttribute:
    """Retire a detail and every live value/choice hanging from it."""
    attribute = _active_attribute(db, attribute_id)
    now = utcnow()

    for value in db.scalars(
        select(ProductAttributeValue).where(
            ProductAttributeValue.fk_product_attribute_id == attribute_id,
            ProductAttributeValue.scd_active_flag.is_(True),
        )
    ).all():
        value.close(changed_by=actor_user_id, at=now)
    for choice in db.scalars(
        select(ProductAttributeChoice).where(
            ProductAttributeChoice.fk_product_attribute_id == attribute_id,
            ProductAttributeChoice.scd_active_flag.is_(True),
        )
    ).all():
        choice.close(changed_by=actor_user_id, at=now)

    attribute.close(changed_by=actor_user_id, at=now)
    db.flush()
    return attribute


def attribute_values(db: Session, product_id: int) -> list[ProductAttributeValue]:
    """Live custom detail values for a product, including variant-scoped ones."""
    product_detail(db, product_id)
    return list(
        db.scalars(
            select(ProductAttributeValue)
            .where(
                ProductAttributeValue.fk_product_id == product_id,
                ProductAttributeValue.scd_active_flag.is_(True),
            )
            .options(
                selectinload(ProductAttributeValue.attribute),
                selectinload(ProductAttributeValue.choice),
            )
            .order_by(
                ProductAttributeValue.fk_product_variant_id,
                ProductAttributeValue.fk_product_attribute_id,
                ProductAttributeValue.pk_product_attribute_value_id,
            )
        ).all()
    )


def attribute_value_index(
    db: Session, product_id: int
) -> dict[str, dict[int, ProductAttributeValue]]:
    """Index values for templates: "product" or "variant:<id>" -> attr id."""
    indexed: dict[str, dict[int, ProductAttributeValue]] = {"product": {}}
    for value in attribute_values(db, product_id):
        scope = (
            "product"
            if value.fk_product_variant_id is None
            else f"variant:{value.fk_product_variant_id}"
        )
        indexed.setdefault(scope, {})[value.fk_product_attribute_id] = value
    return indexed


def set_attribute_values(
    db: Session,
    *,
    product_id: int,
    values: list[dict[str, object]],
    actor_user_id: int | None = None,
) -> list[ProductAttributeValue]:
    """Apply a product detail form in one SCD-aware batch."""
    product_detail(db, product_id)
    active_rows: list[ProductAttributeValue] = []
    for value in values:
        raw_value_ar = value.get("value_ar")
        raw_value_en = value.get("value_en")
        row = set_attribute_value(
            db,
            product_id=product_id,
            attribute_id=int(value["attribute_id"]),
            variant_id=(
                int(value["variant_id"])
                if value.get("variant_id") is not None
                else None
            ),
            value_ar=str(raw_value_ar) if raw_value_ar is not None else None,
            value_en=str(raw_value_en) if raw_value_en is not None else None,
            choice_id=(
                int(value["choice_id"])
                if value.get("choice_id") is not None
                else None
            ),
            actor_user_id=actor_user_id,
        )
        if row is not None:
            active_rows.append(row)
    return active_rows


def set_attribute_value(
    db: Session,
    *,
    product_id: int,
    attribute_id: int,
    variant_id: int | None = None,
    value_ar: str | None = None,
    value_en: str | None = None,
    choice_id: int | None = None,
    actor_user_id: int | None = None,
) -> ProductAttributeValue | None:
    """Give one detail a value here, or clear it by passing a blank value."""
    product_detail(db, product_id)
    attribute = _active_attribute(db, attribute_id)
    if variant_id is not None:
        variant = db.get(ProductVariant, variant_id)
        if (
            variant is None
            or not variant.scd_active_flag
            or variant.fk_product_id != product_id
        ):
            raise NotFound("That variant does not belong to this product.")

    if attribute.input_type == AttributeInputType.DROPDOWN.value:
        value_ar = value_en = None
        if choice_id is not None:
            choice = db.get(ProductAttributeChoice, choice_id)
            if (
                choice is None
                or not choice.scd_active_flag
                or choice.fk_product_attribute_id != attribute_id
            ):
                raise NotFound("That choice does not belong to this detail.")
        empty = choice_id is None
    else:
        choice_id = None
        value_ar = _blank(value_ar)
        value_en = _blank(value_en)
        empty = value_ar is None and value_en is None

    conditions = [
        ProductAttributeValue.fk_product_id == product_id,
        ProductAttributeValue.fk_product_attribute_id == attribute_id,
        ProductAttributeValue.scd_active_flag.is_(True),
    ]
    if variant_id is None:
        conditions.append(ProductAttributeValue.fk_product_variant_id.is_(None))
    else:
        conditions.append(ProductAttributeValue.fk_product_variant_id == variant_id)

    current = db.scalars(select(ProductAttributeValue).where(*conditions)).first()
    now = utcnow()

    if empty:
        if current is not None:
            current.close(changed_by=actor_user_id, at=now)
            db.flush()
        return None

    if current is not None:
        unchanged = (
            current.value_ar == value_ar
            and current.value_en == value_en
            and current.fk_product_attribute_choice_id == choice_id
        )
        if unchanged:
            return current
        current.close(changed_by=actor_user_id, at=now)

    row = ProductAttributeValue(
        fk_product_id=product_id,
        fk_product_variant_id=variant_id,
        fk_product_attribute_id=attribute_id,
        fk_product_attribute_choice_id=choice_id,
        value_ar=value_ar,
        value_en=value_en,
        scd_active_from=now,
        scd_changed_by=actor_user_id,
    )
    db.add(row)
    db.flush()
    return row


def _active_attribute(db: Session, attribute_id: int) -> ProductAttribute:
    attribute = db.get(ProductAttribute, attribute_id)
    if attribute is None or not attribute.scd_active_flag:
        raise NotFound("That detail does not exist.")
    return attribute


def _assert_unique_attribute_code(
    db: Session, code: str, *, exclude_id: int | None = None
) -> None:
    stmt = select(ProductAttribute).where(
        ProductAttribute.attribute_code == code,
        ProductAttribute.scd_active_flag.is_(True),
    )
    if exclude_id is not None:
        stmt = stmt.where(ProductAttribute.pk_product_attribute_id != exclude_id)
    if db.scalars(stmt).first() is not None:
        raise Conflict("A detail with that code already exists.")


def create_product(
    db: Session,
    *,
    name_ar: str,
    name_en: str,
    base_price_amt: Decimal,
    category_id: int | None = None,
    publisher_id: int | None = None,
    sku: str | None = None,
    barcode: str | None = None,
    slug_ar: str | None = None,
    slug_en: str | None = None,
    description_ar: str | None = None,
    description_en: str | None = None,
    short_description_ar: str | None = None,
    short_description_en: str | None = None,
    isbn: str | None = None,
    main_image_path: str | None = None,
    is_visible: bool = True,
    published: bool = False,
    discount_overlap_rule: str = OverlapRule.BEST_FOR_CUSTOMER,
    actor_user_id: int | None = None,
) -> Product:
    if not name_ar.strip() or not name_en.strip():
        raise ValidationFailed("Product names are required in both languages.")
    if Decimal(base_price_amt) < 0:
        raise ValidationFailed("Price cannot be negative.")
    if publisher_id is not None:
        _active_publisher(db, publisher_id)
    if category_id is not None:
        _active_category(db, category_id)
    if discount_overlap_rule not in {item.value for item in OverlapRule}:
        raise ValidationFailed("Choose a valid discount overlap rule.")

    now = utcnow()
    product = Product(
        fk_publisher_id=publisher_id,
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        slug_ar=_slug(slug_ar, name_ar),
        slug_en=_slug(slug_en, name_en),
        description_ar=_blank(description_ar),
        description_en=_blank(description_en),
        short_description_ar=_blank(short_description_ar),
        short_description_en=_blank(short_description_en),
        isbn=_blank(isbn),
        base_price_amt=q(Decimal(base_price_amt)),
        main_image_path=_blank(main_image_path),
        is_visible_flag=is_visible,
        discount_overlap_rule=discount_overlap_rule,
        published_dt=now if published else None,
        scd_active_from=now,
        scd_changed_by=actor_user_id,
    )
    db.add(product)
    db.flush()

    if category_id is not None:
        db.add(
            ProductCategory(
                fk_product_id=product.pk_product_id,
                fk_category_id=category_id,
                is_primary_flag=True,
                scd_active_from=now,
                scd_changed_by=actor_user_id,
            )
        )

    variant_sku = (sku or "").strip() or f"JEC-{product.pk_product_id:06d}"
    _assert_unique_sku(db, variant_sku)
    db.add(
        ProductVariant(
            fk_product_id=product.pk_product_id,
            sku=variant_sku,
            barcode=_blank(barcode),
            is_active_flag=True,
            scd_active_from=now,
            scd_changed_by=actor_user_id,
        )
    )
    db.flush()

    # Keep the search projection in step with the catalog on every write
    # (Part I §15) — an unindexed product is invisible to search.
    _reindex(db, product)
    return product


#: Sentinel for "this field was not submitted, keep what is stored".
#: Distinct from ``None``, which means "the user cleared it".
KEEP = object()


def update_product(
    db: Session,
    *,
    product_id: int,
    name_ar: str,
    name_en: str,
    category_id: int | None = None,
    publisher_id: int | None = None,
    slug_ar: str | None = None,
    slug_en: str | None = None,
    description_ar: str | None = None,
    description_en: str | None = None,
    short_description_ar: str | None = None,
    short_description_en: str | None = None,
    isbn: str | None = None,
    main_image_path: str | None | object = KEEP,
    is_visible: bool = True,
    published: bool = False,
    discount_overlap_rule: str = OverlapRule.BEST_FOR_CUSTOMER,
    min_stock_level: int | None = None,
    optimal_stock_level: int | None = None,
    max_stock_level: int | None = None,
    actor_user_id: int | None = None,
) -> Product:
    product = product_detail(db, product_id)
    if not name_ar.strip() or not name_en.strip():
        raise ValidationFailed("Product names are required in both languages.")
    if publisher_id is not None:
        _active_publisher(db, publisher_id)
    if category_id is not None:
        _active_category(db, category_id)
    if discount_overlap_rule not in {item.value for item in OverlapRule}:
        raise ValidationFailed("Choose a valid discount overlap rule.")

    product.fk_publisher_id = publisher_id
    product.name_ar = name_ar.strip()
    product.name_en = name_en.strip()
    product.slug_ar = _slug(slug_ar, name_ar)
    product.slug_en = _slug(slug_en, name_en)
    product.description_ar = _blank(description_ar)
    product.description_en = _blank(description_en)
    product.short_description_ar = _blank(short_description_ar)
    product.short_description_en = _blank(short_description_en)
    product.isbn = _blank(isbn)
    # The image is owned by the upload widget, not by this form. Saving the
    # details must never silently wipe a photo the form never carried.
    if main_image_path is not KEEP:
        product.main_image_path = _blank(main_image_path)
    product.is_visible_flag = is_visible
    product.discount_overlap_rule = discount_overlap_rule
    if published and product.published_dt is None:
        product.published_dt = utcnow()
    if not published:
        product.published_dt = None
    product.min_stock_level = min_stock_level
    product.optimal_stock_level = optimal_stock_level
    product.max_stock_level = max_stock_level
    product.scd_changed_by = actor_user_id
    _sync_primary_category(db, product.pk_product_id, category_id, actor_user_id)
    db.flush()

    _reindex(db, product)
    return product


def change_price(
    db: Session,
    *,
    product_id: int,
    base_price_amt: Decimal,
    actor_user_id: int | None = None,
) -> Product:
    product = product_detail(db, product_id)
    if Decimal(base_price_amt) < 0:
        raise ValidationFailed("Price cannot be negative.")
    product.base_price_amt = q(Decimal(base_price_amt))
    product.scd_changed_by = actor_user_id
    db.flush()
    return product


def create_variant(
    db: Session,
    *,
    product_id: int,
    sku: str | None = None,
    name_ar: str | None = None,
    name_en: str | None = None,
    barcode: str | None = None,
    price_override_amt: Decimal | None = None,
    main_image_path: str | None = None,
    weight_grams: int | None = None,
    is_active: bool = True,
    sort_order: int = 0,
    actor_user_id: int | None = None,
) -> ProductVariant:
    """Add one specification combination (Part I §5.4).

    Only the product is mandatory. A shopkeeper adding "أسود / Black" should not
    have to invent a stock-keeping code first, so a blank SKU is generated
    rather than refused — the code exists for the unique constraint and the
    barcode scanner, not as a question the user must answer.
    """
    product_detail(db, product_id)
    sku = (sku or "").strip() or _next_variant_sku(db, product_id)
    _assert_unique_sku(db, sku)
    variant = ProductVariant(
        fk_product_id=product_id,
        sku=sku,
        name_ar=_blank(name_ar),
        name_en=_blank(name_en),
        barcode=_blank(barcode),
        price_override_amt=q(Decimal(price_override_amt)) if price_override_amt is not None else None,
        main_image_path=_blank(main_image_path),
        weight_grams=weight_grams,
        is_active_flag=is_active,
        sort_order=sort_order,
        scd_active_from=utcnow(),
        scd_changed_by=actor_user_id,
    )
    db.add(variant)
    db.flush()
    return variant


def update_variant(
    db: Session,
    *,
    variant_id: int,
    sku: str | None = None,
    name_ar: str | None = None,
    name_en: str | None = None,
    barcode: str | None = None,
    price_override_amt: Decimal | None = None,
    main_image_path: str | None = None,
    weight_grams: int | None = None,
    is_active: bool = True,
    sort_order: int = 0,
    actor_user_id: int | None = None,
) -> ProductVariant:
    """Edit one combination in place. A blank SKU keeps the existing one."""
    variant = db.get(ProductVariant, variant_id)
    if variant is None or not variant.scd_active_flag:
        raise NotFound("That variant does not exist.")
    sku = (sku or "").strip() or variant.sku
    _assert_unique_sku(db, sku, exclude_id=variant_id)
    variant.sku = sku
    variant.name_ar = _blank(name_ar)
    variant.name_en = _blank(name_en)
    variant.barcode = _blank(barcode)
    variant.price_override_amt = (
        q(Decimal(price_override_amt)) if price_override_amt is not None else None
    )
    variant.main_image_path = _blank(main_image_path)
    variant.weight_grams = weight_grams
    variant.is_active_flag = is_active
    variant.sort_order = sort_order
    variant.scd_changed_by = actor_user_id
    db.flush()
    return variant


def close_variant(
    db: Session,
    *,
    variant_id: int,
    actor_user_id: int | None = None,
) -> ProductVariant:
    variant = db.get(ProductVariant, variant_id)
    if variant is None or not variant.scd_active_flag:
        raise NotFound("That variant does not exist.")
    for value in db.scalars(
        select(VariantOptionValue).where(
            VariantOptionValue.fk_product_variant_id == variant_id,
            VariantOptionValue.scd_active_flag.is_(True),
        )
    ).all():
        value.close(changed_by=actor_user_id)
    for value in db.scalars(
        select(ProductAttributeValue).where(
            ProductAttributeValue.fk_product_variant_id == variant_id,
            ProductAttributeValue.scd_active_flag.is_(True),
        )
    ).all():
        value.close(changed_by=actor_user_id)
    variant.close(changed_by=actor_user_id)
    db.flush()
    return variant


def create_discount(
    db: Session,
    *,
    name_ar: str,
    name_en: str,
    discount_scope: str,
    product_id: int | None = None,
    category_id: int | None = None,
    include_subcategories: bool = True,
    discount_kind: str = DiscountKind.PERCENTAGE,
    percentage: Decimal | None = None,
    fixed_price_amt: Decimal | None = None,
    starts_dt: dt.datetime | None = None,
    ends_dt: dt.datetime | None = None,
    priority: int = 0,
    actor_user_id: int | None = None,
) -> Discount:
    if not name_ar.strip() or not name_en.strip():
        raise ValidationFailed("Discount names are required in both languages.")
    if discount_scope not in {item.value for item in DiscountScope}:
        raise ValidationFailed("Choose a discount scope.")
    if discount_kind not in {item.value for item in DiscountKind}:
        raise ValidationFailed("Choose a discount kind.")
    if discount_scope == DiscountScope.PRODUCT.value:
        if product_id is None:
            raise ValidationFailed("Choose a product for this discount.")
        product_detail(db, product_id)
        category_id = None
    else:
        if category_id is None:
            raise ValidationFailed("Choose a category for this discount.")
        _active_category(db, category_id)
        product_id = None

    if discount_kind == DiscountKind.PERCENTAGE.value:
        if percentage is None or Decimal(percentage) <= 0 or Decimal(percentage) > 100:
            raise ValidationFailed("Percentage discounts must be between 0 and 100.")
        fixed_price_amt = None
    else:
        if fixed_price_amt is None or Decimal(fixed_price_amt) < 0:
            raise ValidationFailed("Fixed price discounts need a non-negative price.")
        percentage = None

    if starts_dt and ends_dt and ends_dt <= starts_dt:
        raise ValidationFailed("The discount end must be after the start.")

    discount = Discount(
        name_ar=name_ar.strip(),
        name_en=name_en.strip(),
        discount_scope=discount_scope,
        fk_product_id=product_id,
        fk_category_id=category_id,
        include_subcategories_flag=include_subcategories,
        discount_kind=discount_kind,
        percentage=q(Decimal(percentage)) if percentage is not None else None,
        fixed_price_amt=q(Decimal(fixed_price_amt)) if fixed_price_amt is not None else None,
        starts_dt=starts_dt,
        ends_dt=ends_dt,
        priority=priority,
        scd_active_from=utcnow(),
        scd_changed_by=actor_user_id,
    )
    db.add(discount)
    db.flush()
    return discount


def close_discount(
    db: Session,
    *,
    discount_id: int,
    actor_user_id: int | None = None,
) -> Discount:
    discount = db.get(Discount, discount_id)
    if discount is None or not discount.scd_active_flag:
        raise NotFound("That discount does not exist.")
    discount.close(changed_by=actor_user_id)
    db.flush()
    return discount


def moderate_review(
    db: Session,
    *,
    review_id: int,
    status: str,
    note: str | None = None,
    actor_user_id: int | None = None,
) -> ProductReview:
    if status not in {item.value for item in ReviewStatus}:
        raise ValidationFailed("Choose a valid review status.")
    review = db.get(ProductReview, review_id)
    if review is None or not review.scd_active_flag:
        raise NotFound("That review does not exist.")
    review.status = status
    review.moderation_note = _blank(note)
    review.moderated_by_user_id = actor_user_id
    review.moderated_dt = utcnow()
    review.scd_changed_by = actor_user_id
    db.flush()
    return review


def primary_category_id(db: Session, product_id: int) -> int | None:
    return db.scalar(
        select(ProductCategory.fk_category_id).where(
            ProductCategory.fk_product_id == product_id,
            ProductCategory.is_primary_flag.is_(True),
            ProductCategory.scd_active_flag.is_(True),
        )
    )


def _reindex(db: Session, product) -> None:
    """Refresh a product's search projection after a catalog write (§15)."""
    from app.services.search import reindex_product

    reindex_product(db, product)


def _active_category(db: Session, category_id: int) -> Category:
    category = db.get(Category, category_id)
    if category is None or not category.scd_active_flag:
        raise NotFound("That category does not exist.")
    return category


def _active_publisher(db: Session, publisher_id: int) -> Publisher:
    publisher = db.get(Publisher, publisher_id)
    if publisher is None or not publisher.scd_active_flag:
        raise NotFound("That publisher does not exist.")
    return publisher


def _slug(raw: str | None, fallback: str) -> str:
    value = slugify(raw or fallback)
    if not value:
        raise ValidationFailed("A slug could not be generated.")
    return value


def _blank(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _set_category_path(category: Category, parent: Category | None) -> None:
    if parent is None:
        category.ancestor_path = "/"
        category.depth = 0
        return
    category.ancestor_path = f"{parent.ancestor_path}{parent.pk_category_id}/"
    category.depth = parent.depth + 1


def _rebuild_child_paths(
    db: Session,
    parent: Category,
    actor_user_id: int | None,
) -> None:
    children = db.scalars(
        select(Category).where(
            Category.fk_parent_category_id == parent.pk_category_id,
            Category.scd_active_flag.is_(True),
        )
    ).all()
    for child in children:
        _set_category_path(child, parent)
        child.scd_changed_by = actor_user_id
        _rebuild_child_paths(db, child, actor_user_id)


def _sync_primary_category(
    db: Session,
    product_id: int,
    category_id: int | None,
    actor_user_id: int | None,
) -> None:
    active_links = db.scalars(
        select(ProductCategory).where(
            ProductCategory.fk_product_id == product_id,
            ProductCategory.scd_active_flag.is_(True),
        )
    ).all()
    kept = False
    for link in active_links:
        if category_id is not None and link.fk_category_id == category_id:
            link.is_primary_flag = True
            link.scd_changed_by = actor_user_id
            kept = True
        else:
            link.close(changed_by=actor_user_id)
    if category_id is not None and not kept:
        db.add(
            ProductCategory(
                fk_product_id=product_id,
                fk_category_id=category_id,
                is_primary_flag=True,
                scd_active_from=utcnow(),
                scd_changed_by=actor_user_id,
            )
        )


def _next_variant_sku(db: Session, product_id: int) -> str:
    """A unique, readable stand-in: ``JEC-000012-03``.

    Counts every variant the product has *ever* had, live or retired, so a
    retired code is never handed out a second time — SKUs end up on printed
    labels and past shipment lines, where reuse would be a real mix-up.
    """
    used = db.scalar(
        select(func.count()).select_from(ProductVariant).where(
            ProductVariant.fk_product_id == product_id
        )
    ) or 0
    while True:
        used += 1
        candidate = f"JEC-{product_id:06d}-{used:02d}"
        exists = db.scalars(
            select(ProductVariant).where(ProductVariant.sku == candidate)
        ).first()
        if exists is None:
            return candidate


def _assert_unique_sku(
    db: Session,
    sku: str,
    *,
    exclude_id: int | None = None,
) -> None:
    stmt = select(ProductVariant).where(
        ProductVariant.sku == sku,
        ProductVariant.scd_active_flag.is_(True),
    )
    if exclude_id is not None:
        stmt = stmt.where(ProductVariant.pk_product_variant_id != exclude_id)
    if db.scalars(stmt).first() is not None:
        raise Conflict("That SKU is already in use.")


def _assert_unique_publisher_slug(
    db: Session,
    slug: str,
    *,
    exclude_id: int | None = None,
) -> None:
    stmt = select(Publisher).where(
        Publisher.slug == slug,
        Publisher.scd_active_flag.is_(True),
    )
    if exclude_id is not None:
        stmt = stmt.where(Publisher.pk_publisher_id != exclude_id)
    if db.scalars(stmt).first() is not None:
        raise Conflict("That publisher slug is already in use.")
