"""Promocodes, homepage builder, announcements, newsletter, email templates.

Covers Part I §13 (promocodes), §4 (homepage), §3.2/§3.3 (footer and the
site-wide announcement bar), §2.7 (transactional emails) and §2.6 (newsletter
preferences).

Everything here is admin-managed content, which is why almost every text column
comes in an AR/EN pair: Part I §1 requires separate Arabic and English inputs
wherever a value is entered in the admin panel.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, MONEY, SCDMixin, TrxBase, UtcDateTime, bilingual, fk, pk
from app.models.enums import HomepageSectionKind, PromocodeKind


class Promocode(Base, SCDMixin):
    """A discount code (Part I §13)."""

    __tablename__ = "scd_promocode"
    __grain__ = "One version of one promocode."

    pk_promocode_id: Mapped[int] = pk("promocode")
    #: Compared case-insensitively; stored upper-cased.
    code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name_ar, name_en = bilingual("name", 200, nullable=True)

    promocode_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    #: PERCENTAGE and PERCENTAGE_CAPPED.
    percentage: Mapped[Decimal | None] = mapped_column(MONEY)
    #: The JD ceiling for PERCENTAGE_CAPPED.
    max_discount_amt: Mapped[Decimal | None] = mapped_column(MONEY)
    #: FIXED_AMOUNT.
    fixed_amount_amt: Mapped[Decimal | None] = mapped_column(MONEY)

    minimum_order_amt: Mapped[Decimal | None] = mapped_column(MONEY)
    starts_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    expires_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)

    #: Three independent limits, per the decision in Part I §13:
    #: single-use-globally, reusable-until-a-total-cap, and a per-customer cap.
    single_use_globally_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    max_total_uses: Mapped[int | None] = mapped_column(Integer)
    max_uses_per_customer: Mapped[int | None] = mapped_column(Integer)

    #: Whether it stacks on top of an item's existing discount (Part I §13).
    stacks_with_item_discount_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    #: Whether consigned items qualify. Only meaningful alongside the
    #: arrangement's own eligibility switch (Part I §7) — both must allow it.
    applies_to_consigned_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    #: Null on both restriction tables means "everything".
    note: Mapped[str | None] = mapped_column(Text)

    restrictions: Mapped[list["PromocodeRestriction"]] = relationship(back_populates="promocode")

    __table_args__ = (
        Index(
            "uq_scd_promocode_code_active",
            "code",
            unique=True,
            sqlite_where=text("scd_active_flag = 1"),
            postgresql_where=text("scd_active_flag"),
        ),
    )


class PromocodeRestriction(Base, SCDMixin):
    """Restricts a code to specific categories or items (Part I §13).

    Rows rather than a list column, so "which codes touch this category" is a
    join instead of a scan.
    """

    __tablename__ = "lkp_promocode_restriction"
    __grain__ = "One version of one category or product restriction on one promocode."

    pk_promocode_restriction_id: Mapped[int] = pk("promocode_restriction")
    fk_promocode_id: Mapped[int] = fk("promocode", "scd_promocode.pk_promocode_id")
    fk_category_id: Mapped[int | None] = fk(
        "category", "scd_category.pk_category_id", nullable=True
    )
    fk_product_id: Mapped[int | None] = fk(
        "product", "scd_product.pk_product_id", nullable=True
    )
    #: True excludes rather than includes, so "everything except Bibles" does
    #: not require listing every other category.
    is_exclusion_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    promocode: Mapped[Promocode] = relationship(back_populates="restrictions")


class PromocodeRedemption(TrxBase):
    """One use of a code — insert-only, and the source of every usage count.

    Counts are derived from these rows rather than stored on the promocode, so
    the limit check and the redemption history can never disagree (Part II §1).
    """

    __tablename__ = "trx_promocode_redemption"
    __grain__ = "One redemption of one promocode on one order."

    pk_promocode_redemption_id: Mapped[int] = pk("promocode_redemption")
    fk_promocode_id: Mapped[int] = fk("promocode", "scd_promocode.pk_promocode_id")
    fk_order_id: Mapped[int] = fk("order", "scd_order.pk_order_id")
    fk_user_id: Mapped[int] = fk("user", "scd_user.pk_user_id")
    discount_amt: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    #: Negative-signed reversal when the order is cancelled, so the use is
    #: returned to the customer's allowance without deleting history.
    reverses_redemption_id: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_trx_promocode_redemption_code_user", "fk_promocode_id", "fk_user_id"),
    )


class HomepageSection(Base, SCDMixin):
    """One block on the homepage (Part I §4).

    ``sort_order`` is what the drag-and-drop builder writes, and the scheduling
    columns are what let a Christmas promo activate and expire on its own.
    Auto-populating carousel types (New Arrivals, Best Sellers, Discounted, Most
    Viewed) need no curation — optionally scoped to one category, which is how
    the old site's per-category carousel tabs are reproduced.
    """

    __tablename__ = "scd_homepage_section"
    __grain__ = "One version of one homepage section."

    pk_homepage_section_id: Mapped[int] = pk("homepage_section")
    section_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    title_ar, title_en = bilingual("title", 200, nullable=True)
    subtitle_ar: Mapped[str | None] = mapped_column(String(400))
    subtitle_en: Mapped[str | None] = mapped_column(String(400))
    body_ar: Mapped[str | None] = mapped_column(Text)
    body_en: Mapped[str | None] = mapped_column(Text)

    image_path_ar: Mapped[str | None] = mapped_column(
        String(500), comment="Banners often carry baked-in text, so artwork is per language."
    )
    image_path_en: Mapped[str | None] = mapped_column(String(500))
    video_url: Mapped[str | None] = mapped_column(String(500))
    link_url: Mapped[str | None] = mapped_column(String(500))

    #: Scopes an auto-populating carousel to one category, e.g. "Best Sellers —
    #: Statues" (Part I §4).
    fk_category_id: Mapped[int | None] = fk(
        "category", "scd_category.pk_category_id", nullable=True
    )
    item_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=12)

    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    is_enabled_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Scheduling — auto-activate and auto-expire without a developer (Part I §4).
    starts_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    ends_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    items: Mapped[list["HomepageSectionItem"]] = relationship(back_populates="section")

    def is_live(self, now: dt.datetime) -> bool:
        if not (self.is_enabled_flag and self.scd_active_flag):
            return False
        if self.starts_dt and self.starts_dt > now:
            return False
        if self.ends_dt and self.ends_dt <= now:
            return False
        return True


class HomepageSectionItem(Base, SCDMixin):
    """A hand-picked product in a curated section ("our picks for you")."""

    __tablename__ = "scd_homepage_section_item"
    __grain__ = "One version of one curated product within one homepage section."

    pk_homepage_section_item_id: Mapped[int] = pk("homepage_section_item")
    fk_homepage_section_id: Mapped[int] = fk(
        "homepage_section", "scd_homepage_section.pk_homepage_section_id"
    )
    fk_product_id: Mapped[int] = fk("product", "scd_product.pk_product_id")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    section: Mapped[HomepageSection] = relationship(back_populates="items")


class AnnouncementBar(Base, SCDMixin):
    """The persistent bar above the header on *every* page (Part I §3.3).

    Distinct from a homepage announcement section: this is the storewide
    mechanism for a notice like "delivery temporarily suspended". Zero, one or
    several may be active at once; ``priority`` decides the stacking order.
    """

    __tablename__ = "scd_announcement_bar"
    __grain__ = "One version of one site-wide announcement bar."

    pk_announcement_bar_id: Mapped[int] = pk("announcement_bar")
    message_ar, message_en = bilingual("message", 500)
    link_url: Mapped[str | None] = mapped_column(String(500))
    background_hex: Mapped[str | None] = mapped_column(String(7))
    text_hex: Mapped[str | None] = mapped_column(String(7))

    #: The on/off toggle for immediate use without scheduling (Part I §3.3).
    is_enabled_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    starts_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    ends_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_dismissible_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def is_live(self, now: dt.datetime) -> bool:
        if not (self.is_enabled_flag and self.scd_active_flag):
            return False
        if self.starts_dt and self.starts_dt > now:
            return False
        if self.ends_dt and self.ends_dt <= now:
            return False
        return True


class SiteSetting(Base, SCDMixin):
    """Admin-editable site-wide values: footer copy, social links, the storewide
    view/purchase-count defaults, session timeout (Part I §2.3, §3.2, §5.3).

    Typed value columns rather than one stringly-typed column, so a number is a
    number and a report can aggregate it without casting.
    """

    __tablename__ = "scd_site_setting"
    __grain__ = "One version of one site-wide setting."

    pk_site_setting_id: Mapped[int] = pk("site_setting")
    setting_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    value_text_ar: Mapped[str | None] = mapped_column(Text)
    value_text_en: Mapped[str | None] = mapped_column(Text)
    value_number: Mapped[Decimal | None] = mapped_column(MONEY)
    value_flag: Mapped[bool | None] = mapped_column(Boolean)
    description: Mapped[str | None] = mapped_column(Text)


class EmailTemplate(Base, SCDMixin):
    """One admin-controlled email template (Part I §2.7).

    The system owns the *required* technical fields — a reset link and its
    expiry, for instance — and Admin owns the wording and branding around them.
    ``required_placeholders`` records which tokens the body must contain so a
    save that would break a password reset is rejected rather than discovered
    by a locked-out customer.
    """

    __tablename__ = "scd_email_template"
    __grain__ = "One version of one transactional email template."

    pk_email_template_id: Mapped[int] = pk("email_template")
    template_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    subject_ar, subject_en = bilingual("subject", 300)
    body_ar: Mapped[str] = mapped_column(Text, nullable=False)
    body_en: Mapped[str] = mapped_column(Text, nullable=False)
    required_placeholders: Mapped[str | None] = mapped_column(
        String(500), comment="Comma-separated tokens that must appear in the body."
    )
    is_enabled_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class NewsletterSubscription(Base, SCDMixin):
    """Newsletter status, manageable from a dedicated profile page as well as
    the footer box and the registration checkbox (Part I §2.6)."""

    __tablename__ = "scd_newsletter_subscription"
    __grain__ = "One version of one email address's newsletter subscription."

    pk_newsletter_subscription_id: Mapped[int] = pk("newsletter_subscription")
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    #: Null for a footer signup by someone without an account.
    fk_user_id: Mapped[int | None] = fk("user", "scd_user.pk_user_id", nullable=True)
    is_subscribed_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    subscribed_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    unsubscribed_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    source: Mapped[str | None] = mapped_column(
        String(40), comment="registration | footer | profile | import"
    )
    #: Double opt-in token, so a typo'd address cannot subscribe someone else.
    confirm_token: Mapped[str | None] = mapped_column(String(64))
    confirmed_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)


class EmailOutbox(Base, SCDMixin):
    """The send queue: one row per email the system intends to deliver.

    SCD rather than TRX because delivery state genuinely changes — queued, sent,
    failed, retried. ``idempotency_key`` is what makes a retried job safe: the
    unique constraint means the same key can only ever produce one row, so a
    redelivered job cannot email a customer twice (Part II §5).
    """

    __tablename__ = "scd_email_outbox"
    __grain__ = "One version of one queued or delivered email to one recipient."

    pk_email_outbox_id: Mapped[int] = pk("email_outbox")
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    template_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    recipient_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(2), nullable=False, default="ar")
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    queued_dt: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, nullable=False, index=True
    )
    sent_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    #: Null while queued; set on failure. Never silent (Part II §5).
    error_detail: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
