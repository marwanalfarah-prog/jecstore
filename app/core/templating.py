"""Jinja environment and template helpers.

Templates get a small, deliberate vocabulary. Everything language- or
currency-dependent goes through a helper here — ``t``, ``money``, ``num``,
``localized`` — so no template ever hardcodes a string, formats a price by hand,
or reaches for ``name_ar`` directly. That is what keeps the bilingual and RTL
requirements (Part I §1, §17.2) from decaying one template at a time.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.core.config import settings
from app.core.context import get_context
from app.core import i18n


def _localized(obj: Any, field: str, language: str) -> str:
    """Read the AR/EN pair on a model as one value.

    Falls back to the other language when a translation has not been filled in
    yet, so a half-translated catalog degrades to "shows something" rather than
    "shows nothing".
    """
    if obj is None:
        return ""
    primary = getattr(obj, f"{field}_{language}", None)
    if primary:
        return primary
    other = "en" if language == "ar" else "ar"
    return getattr(obj, f"{field}_{other}", None) or ""


def _template_globals(request: Request) -> dict[str, Any]:
    ctx = get_context(request)
    language = ctx.language

    def t(key: str, **params: Any) -> str:
        return i18n.translate(key, language, **params)

    def money(amount_jod: Decimal | int | float | None) -> str:
        if amount_jod is None:
            return ""
        return i18n.format_money(amount_jod, language, ctx.currency, ctx.usd_rate)

    def num(value: Decimal | int | float | None, decimals: int = 0) -> str:
        if value is None:
            return ""
        return i18n.format_number(value, language, decimals=decimals)

    def localized(obj: Any, field: str = "name") -> str:
        return _localized(obj, field, language)

    def date(value: dt.date | dt.datetime | None) -> str:
        return i18n.format_date(value, language) if value else ""

    def datetime_(value: dt.datetime | None) -> str:
        return i18n.format_datetime(value, language) if value else ""

    def percentage(value: Decimal | int | float | None) -> str:
        return i18n.format_percentage(value, language) if value is not None else ""

    def url_with(**params: Any) -> str:
        """Current URL with query parameters merged in.

        Used by the sort/filter/pagination controls so each one changes only its
        own parameter instead of dropping everyone else's.
        """
        merged = dict(request.query_params)
        for key, value in params.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = str(value)
        query = urlencode(merged)
        return f"{request.url.path}?{query}" if query else request.url.path

    def static(path: str) -> str:
        return f"/static/{path.lstrip('/')}"

    # URLs are built here, not in templates, so the id-first slug scheme from
    # Part I §16 is applied in exactly one place.
    def product_url(product: Any) -> str:
        from app.services.catalog import product_url as build

        return build(product, language)

    def category_url(category: Any) -> str:
        from app.services.catalog import category_url as build

        return build(category, language)

    def publisher_url(publisher: Any) -> str:
        from app.services.catalog import publisher_url as build

        return build(publisher, language)

    def tag_url(tag: Any) -> str:
        from app.services.catalog import tag_url as build

        return build(tag)

    def media(path: str | None) -> str | None:
        if not path:
            return None
        if path.startswith(("http://", "https://", "/")):
            return path
        return f"{settings.media_url.rstrip('/')}/{path.lstrip('/')}"

    return {
        "ctx": ctx,
        "lang": language,
        "dir": ctx.dir,
        "is_rtl": ctx.is_rtl,
        "currency": ctx.currency,
        "t": t,
        "money": money,
        "num": num,
        "localized": localized,
        "date": date,
        "datetime": datetime_,
        "percentage": percentage,
        "url_with": url_with,
        "static": static,
        "media": media,
        "product_url": product_url,
        "category_url": category_url,
        "publisher_url": publisher_url,
        "tag_url": tag_url,
        "settings": settings,
        "now": i18n.to_local(dt.datetime.now(dt.timezone.utc)),
    }


class StoreTemplates(Jinja2Templates):
    """Jinja2Templates that injects the per-request helpers automatically."""

    def TemplateResponse(  # noqa: N802 - matching Starlette's API
        self,
        request: Request,
        name: str,
        context: dict[str, Any] | None = None,
        *args: Any,
        **kwargs: Any,
    ):
        merged = _template_globals(request)
        merged.update(context or {})
        return super().TemplateResponse(request, name, merged, *args, **kwargs)


def _build_templates() -> StoreTemplates:
    instance = StoreTemplates(directory=str(settings.templates_dir))
    env = instance.env
    env.trim_blocks = True
    env.lstrip_blocks = True
    # Autoescape is on by default for .html; make it explicit so nobody
    # "helpfully" turns it off later.
    env.autoescape = True
    env.globals["Markup"] = Markup
    return instance


templates = _build_templates()
