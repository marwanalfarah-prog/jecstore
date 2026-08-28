"""Site chrome: the pieces that render on every page.

The announcement bar, mega-menu categories, footer settings and the cart badge
appear in the layout, so they are loaded once per request into the context
rather than fetched by whichever view happens to need them. Kept to a small,
fixed number of queries — this cost is paid on *every* page, including error
pages, so it stays cheap.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.catalog import Category, Product
from app.models.marketing import AnnouncementBar, SiteSetting
from app.models.orders import Cart, CartLine, CompareEntry, Wishlist

log = get_logger(__name__)

#: How many top-level categories the mega-menu shows before "All categories".
MEGA_MENU_LIMIT = 8


def load_chrome(db: Session, context: RequestContext) -> None:
    """Populate the layout-wide parts of the context.

    Failures are logged and swallowed: a missing footer setting must never turn
    a working product page into a 500.
    """
    try:
        context.announcement_bars = _live_announcement_bars(db)
        context.main_categories = _main_categories(db)
        context.site_settings = _site_settings(db, context.language)
        _load_counts(db, context)
    except Exception:  # noqa: BLE001 - chrome is decoration, not the page
        log.exception("chrome_load_failed")


def _live_announcement_bars(db: Session) -> list[AnnouncementBar]:
    """Zero, one or several may be live at once (Part I §3.3)."""
    now = utcnow()
    bars = db.scalars(
        select(AnnouncementBar)
        .where(
            AnnouncementBar.scd_active_flag.is_(True),
            AnnouncementBar.is_enabled_flag.is_(True),
        )
        .order_by(AnnouncementBar.priority.desc(), AnnouncementBar.pk_announcement_bar_id)
    ).all()
    return [bar for bar in bars if bar.is_live(now)]


def _main_categories(db: Session) -> list[Category]:
    return list(
        db.scalars(
            select(Category)
            .where(
                Category.scd_active_flag.is_(True),
                Category.is_visible_flag.is_(True),
                Category.fk_parent_category_id.is_(None),
            )
            .order_by(Category.sort_order, Category.pk_category_id)
            .limit(MEGA_MENU_LIMIT)
        ).all()
    )


def _site_settings(db: Session, language: str) -> dict[str, str]:
    """Admin-editable settings, flattened to the caller's language.

    Templates read ``ctx.site_settings["footer_about"]`` and get the right
    language without knowing there are two columns behind it.
    """
    rows = db.scalars(
        select(SiteSetting).where(SiteSetting.scd_active_flag.is_(True))
    ).all()

    settings_map: dict[str, str] = {}
    for row in rows:
        primary = row.value_text_ar if language == "ar" else row.value_text_en
        fallback = row.value_text_en if language == "ar" else row.value_text_ar
        value = primary or fallback
        if value is None and row.value_number is not None:
            value = str(row.value_number)
        if value is None and row.value_flag is not None:
            value = "1" if row.value_flag else ""
        if value is not None:
            settings_map[row.setting_key] = value
    return settings_map


def _load_counts(db: Session, context: RequestContext) -> None:
    """Header badge counts: cart, compare, wishlist.

    All three key on the user when signed in and on the session otherwise —
    guest browsing and guest comparing are both allowed (Part I §14).
    """
    user_id = context.user.pk_user_id if context.user else None
    session_key = context.session_key

    if user_id is None and session_key is None:
        return

    cart_filter = (
        Cart.fk_user_id == user_id if user_id is not None else Cart.session_key == session_key
    )
    context.cart_count = db.scalar(
        select(func.coalesce(func.sum(CartLine.quantity), 0))
        .select_from(CartLine)
        .join(Cart, Cart.pk_cart_id == CartLine.fk_cart_id)
        .where(
            cart_filter,
            Cart.scd_active_flag.is_(True),
            Cart.converted_order_id.is_(None),
            CartLine.scd_active_flag.is_(True),
        )
    ) or 0

    compare_filter = (
        CompareEntry.fk_user_id == user_id
        if user_id is not None
        else CompareEntry.session_key == session_key
    )
    context.compare_count = db.scalar(
        select(func.count())
        .select_from(CompareEntry)
        .where(compare_filter, CompareEntry.scd_active_flag.is_(True))
    ) or 0

    if user_id is not None:
        # Ids rather than a count, for two reasons. The heart on every product
        # card needs to know whether *this* product is saved, and the badge has
        # to agree with what /account/wishlist actually lists — that page joins
        # Product and filters on active + visible, while the badge used to
        # count wishlist rows alone. Hide a product and the badge said 1 over an
        # empty page, permanently. Same query, same filters, so they cannot
        # disagree again.
        context.wishlisted_ids = set(
            db.scalars(
                select(Wishlist.fk_product_id)
                .join(Product, Product.pk_product_id == Wishlist.fk_product_id)
                .where(
                    Wishlist.fk_user_id == user_id,
                    Wishlist.scd_active_flag.is_(True),
                    Product.scd_active_flag.is_(True),
                    Product.is_visible_flag.is_(True),
                )
            ).all()
        )
        context.wishlist_count = len(context.wishlisted_ids)
