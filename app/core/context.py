"""Per-request presentation context.

Language, currency and the live exchange rate are needed by nearly every
template, so they are resolved once per request and hung on ``request.state``
rather than re-fetched by each view. Templates then read them through the
helpers in ``app.core.templating`` and never touch the database themselves.

Resolution order for both language and currency: explicit query parameter (how
the header toggles work), then cookie, then the signed-in user's saved
preference, then the configured default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from fastapi import Request

from app.core.config import settings
from app.core.i18n import direction, normalize_currency, normalize_language

if TYPE_CHECKING:  # pragma: no cover
    from app.models.identity import User

LANGUAGE_COOKIE = "jec_lang"
CURRENCY_COOKIE = "jec_currency"
GUEST_SESSION_COOKIE = "jec_guest_key"
#: A year — the choice is a durable preference, not a session detail.
PREFERENCE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


@dataclass(slots=True)
class RequestContext:
    language: str
    currency: str
    #: The rate live *now*. Deliberately re-read per request: an item in an open
    #: cart reflects the current rate at checkout, not the rate when it was
    #: added (Part I §1.1).
    usd_rate: Decimal
    user: "User | None" = None
    session_key: str | None = None
    #: Set only during impersonation, so the banner and audit trail can name the
    #: real actor (Part I §2.2.2).
    impersonator: "User | None" = None
    cart_count: int = 0
    compare_count: int = 0
    wishlist_count: int = 0
    #: Which products the signed-in shopper has saved. The heart on a product
    #: card and on the product page is a *toggle*, so it has to be able to draw
    #: itself as already-on — without this it rendered "Add to wishlist"
    #: identically whether the item was saved or not, and a second click
    #: silently removed what the shopper thought they were adding.
    wishlisted_ids: set[int] = field(default_factory=set)
    #: Live site-wide bars, above the header on every page (Part I §3.3).
    announcement_bars: list = field(default_factory=list)
    #: Top-level categories for the mega-menu, which renders on every page.
    main_categories: list = field(default_factory=list)
    #: Admin-editable footer copy, social links and toggles, keyed by setting.
    site_settings: dict[str, str] = field(default_factory=dict)

    @property
    def dir(self) -> str:
        return direction(self.language)

    @property
    def is_rtl(self) -> bool:
        return self.dir == "rtl"

    @property
    def other_language(self) -> str:
        return "en" if self.language == "ar" else "ar"

    @property
    def is_authenticated(self) -> bool:
        return self.user is not None

    @property
    def is_impersonating(self) -> bool:
        return self.impersonator is not None


def resolve_language(request: Request, user: "User | None" = None) -> str:
    if requested := request.query_params.get("lang"):
        return normalize_language(requested)
    if cookie := request.cookies.get(LANGUAGE_COOKIE):
        return normalize_language(cookie)
    if user is not None:
        return normalize_language(user.preferred_language)
    return settings.default_language


def resolve_currency(request: Request, user: "User | None" = None) -> str:
    if requested := request.query_params.get("currency"):
        return normalize_currency(requested)
    if cookie := request.cookies.get(CURRENCY_COOKIE):
        return normalize_currency(cookie)
    if user is not None:
        return normalize_currency(user.preferred_currency)
    return settings.default_currency


def get_context(request: Request) -> RequestContext:
    """The context built by ``RequestContextMiddleware`` for this request."""
    context = getattr(request.state, "context", None)
    if context is None:
        # A safe fallback so an early-failing request can still render an error
        # page rather than blowing up inside the error handler itself.
        context = RequestContext(
            language=settings.default_language,
            currency=settings.default_currency,
            usd_rate=Decimal(str(settings.default_usd_rate)),
        )
        request.state.context = context
    return context
