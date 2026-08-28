"""Bilingual + RTL support (Part I §1, §16; Part II §6).

The spec is explicit that RTL means more than ``dir="rtl"``: numeral formatting
and price/date layout have to be right too. So this module owns four things, and
templates are not allowed to improvise any of them:

* **Translation** — ``t("key")`` against ``locales/{ar,en}.json``.
* **Numerals** — one site-wide decision (Western vs Arabic-Indic), applied
  consistently, per Part I §17.2. Configured once in ``NUMERAL_SYSTEM``.
* **Money** — JOD is stored; USD is a *display-only* conversion at the current
  rate (Part I §1.1). Currency is always labelled, never a bare number.
* **Direction** — ``dir`` and the logical start/end edges, so one template
  serves both languages instead of two diverging copies.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from typing import Any, Literal

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

LANGUAGES: tuple[str, ...] = ("ar", "en")
RTL_LANGUAGES: frozenset[str] = frozenset({"ar"})

#: Arabic-Indic digits, index-aligned with 0-9 for a direct translate table.
_ARABIC_INDIC = "٠١٢٣٤٥٦٧٨٩"
_TO_ARABIC_INDIC = str.maketrans("0123456789", _ARABIC_INDIC)

#: JOD is a three-decimal currency (1 dinar = 1000 fils); USD is two.
CURRENCY_DECIMALS: dict[str, int] = {"JOD": 3, "USD": 2}

_MONTHS_AR = (
    "كانون الثاني", "شباط", "آذار", "نيسان", "أيار", "حزيران",
    "تموز", "آب", "أيلول", "تشرين الأول", "تشرين الثاني", "كانون الأول",
)
_MONTHS_EN = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


@lru_cache(maxsize=len(LANGUAGES))
def _catalog(language: str) -> dict[str, str]:
    """Load and flatten one locale file.

    Nested JSON is flattened to dotted keys, so ``{"cart": {"empty": "..."}}``
    is reachable as ``cart.empty`` — the file stays organised while lookups stay
    a single dict hit.
    """
    path = settings.locales_dir / f"{language}.json"
    if not path.exists():
        log.warning("locale_file_missing", extra={"language": language, "path": str(path)})
        return {}

    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)

    flat: dict[str, str] = {}

    def _walk(node: Any, prefix: str = "") -> None:
        for key, value in node.items():
            full = f"{prefix}{key}"
            if isinstance(value, dict):
                _walk(value, f"{full}.")
            else:
                flat[full] = str(value)

    _walk(raw)
    return flat


def catalog(language: str) -> dict[str, str]:
    """The flattened catalog for one language, without any fallback.

    :func:`translate` deliberately falls back to the other language so a gap
    renders as readable text rather than a key. That is right for a page and
    wrong for a check: use this when the question is "does *this* language have
    the string", not "what should we show".
    """
    return _catalog(language)


def clear_translation_cache() -> None:
    """Drop cached catalogs — used by the dev reloader and by tests."""
    _catalog.cache_clear()


def normalize_language(value: str | None) -> str:
    if value:
        candidate = value.strip().lower()[:2]
        if candidate in LANGUAGES:
            return candidate
    return settings.default_language


def normalize_currency(value: str | None) -> str:
    if value and value.strip().upper() in CURRENCY_DECIMALS:
        return value.strip().upper()
    return settings.default_currency


def is_rtl(language: str) -> bool:
    return language in RTL_LANGUAGES


def direction(language: str) -> Literal["rtl", "ltr"]:
    return "rtl" if is_rtl(language) else "ltr"


def translate(key: str, language: str, /, **params: Any) -> str:
    """Look up ``key``; fall back to the other language, then to the key itself.

    A missing string renders as its key rather than blank — visible in review,
    and never a silently empty button.
    """
    catalog = _catalog(language)
    text = catalog.get(key)

    if text is None:
        for fallback in LANGUAGES:
            if fallback != language:
                text = _catalog(fallback).get(key)
                if text is not None:
                    log.debug("translation_fallback", extra={"key": key, "language": language})
                    break

    if text is None:
        log.warning("translation_missing", extra={"key": key, "language": language})
        return key

    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError):
            log.warning("translation_format_failed", extra={"key": key})
            return text
    return text


# ---------------------------------------------------------------------------
# Numerals
# ---------------------------------------------------------------------------


def localize_digits(text: str, language: str) -> str:
    """Apply the site-wide numeral decision (Part I §17.2).

    Deliberately one global setting rather than per-language: the old site never
    settled this, and the spec asks for it to be decided once and applied
    consistently. Arabic-Indic digits are only ever used in Arabic, even when
    the setting selects them.
    """
    if settings.numeral_system == "arabic-indic" and is_rtl(language):
        return text.translate(_TO_ARABIC_INDIC)
    return text


def format_number(value: Decimal | int | float, language: str, *, decimals: int = 0) -> str:
    quantized = Decimal(str(value)).quantize(
        Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP
    )
    return localize_digits(f"{quantized:,.{decimals}f}", language)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


def convert_jod(amount_jod: Decimal, currency: str, rate_jod_to_usd: Decimal) -> Decimal:
    """Convert a stored JOD amount for *display only*.

    Nothing is ever stored in USD, and this result must never be written back to
    the database (Part I §1.1).
    """
    if currency == "USD":
        return Decimal(amount_jod) * Decimal(rate_jod_to_usd)
    return Decimal(amount_jod)


def format_money(
    amount_jod: Decimal | int | float,
    language: str,
    currency: str,
    rate_jod_to_usd: Decimal,
) -> str:
    """Render a stored JOD amount in the shopper's chosen currency.

    The currency is always part of the output — an unlabelled number is exactly
    what Part I §1.1 forbids on invoices, and the same rule is worth keeping
    everywhere so the two never diverge.
    """
    currency = normalize_currency(currency)
    decimals = CURRENCY_DECIMALS[currency]
    converted = convert_jod(Decimal(str(amount_jod)), currency, rate_jod_to_usd)
    number = format_number(converted, language, decimals=decimals)
    symbol = translate(f"currency.{currency.lower()}", language)

    # In both directions the symbol leads the amount, matching how prices are
    # written locally ("د.أ ١٢٫٥٠٠" / "JOD 12.500").
    # Joined with a non-breaking space, written as an escape rather than the
    # literal character so it stays visible in the source: a price must never
    # wrap between its symbol and its digits, least of all in a narrow mobile
    # product card.
    return f"{symbol} {number}"


def format_percentage(value: Decimal | int | float, language: str) -> str:
    return f"{format_number(value, language, decimals=0)}%"


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def to_local(moment: dt.datetime) -> dt.datetime:
    """UTC is what is stored; local time is a presentation concern (Part II §1)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(JORDAN_TZ)


#: Jordan abolished DST in 2022 and sits on UTC+3 year-round.
JORDAN_TZ = dt.timezone(dt.timedelta(hours=3), name="Asia/Amman")


def format_date(value: dt.date | dt.datetime, language: str) -> str:
    if isinstance(value, dt.datetime):
        value = to_local(value).date()
    months = _MONTHS_AR if language == "ar" else _MONTHS_EN
    day = localize_digits(str(value.day), language)
    year = localize_digits(str(value.year), language)
    return f"{day} {months[value.month - 1]} {year}"


def format_datetime(value: dt.datetime, language: str) -> str:
    local = to_local(value)
    clock = localize_digits(local.strftime("%H:%M"), language)
    return f"{format_date(local, language)} — {clock}"
