"""Admin-managed content (Part I §4, §3.2, §3.3, §2.7, §6, §2.2).

The behaviours worth pinning are the ones the spec is specific about: the
homepage reorders without a developer, announcements stack rather than replace
each other, an email template cannot lose a token the system needs, a branch
holding stock cannot vanish, and a shipping rule can say "we will contact you"
instead of a price.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.db.base import Base, utcnow
from app.models.enums import EmailTemplateCode, HomepageSectionKind, StockPoolKind
from app.models.identity import Country, Province
from app.models.inventory import Branch, StockLevel, StockPool
from app.models.marketing import (
    AnnouncementBar,
    EmailTemplate,
    HomepageSection,
    SiteSetting,
)
from app.models.orders import ShippingRule
from app.services import content_admin
from app.services.email import MissingPlaceholderError


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


# ---------------------------------------------------------------------------
# Homepage builder (Part I §4)
# ---------------------------------------------------------------------------


def test_sections_append_in_order(db: Session):
    first = content_admin.create_section(db, section_kind=HomepageSectionKind.BANNER)
    second = content_admin.create_section(
        db, section_kind=HomepageSectionKind.NEW_ARRIVALS
    )
    db.commit()

    assert (first.sort_order, second.sort_order) == (0, 1)


def test_reorder_applies_the_new_order(db: Session):
    """The drag-and-drop builder writes the full order (§4)."""
    a = content_admin.create_section(db, section_kind=HomepageSectionKind.BANNER)
    b = content_admin.create_section(db, section_kind=HomepageSectionKind.BEST_SELLERS)
    c = content_admin.create_section(db, section_kind=HomepageSectionKind.DISCOUNTED)
    db.commit()

    content_admin.reorder_sections(
        db,
        [c.pk_homepage_section_id, a.pk_homepage_section_id, b.pk_homepage_section_id],
    )
    db.commit()

    ordered = [s.pk_homepage_section_id for s in content_admin.homepage_sections(db)]
    assert ordered == [
        c.pk_homepage_section_id,
        a.pk_homepage_section_id,
        b.pk_homepage_section_id,
    ]


def test_reorder_rejects_an_unknown_section(db: Session):
    """A bad id must not leave the page half-reordered."""
    content_admin.create_section(db, section_kind=HomepageSectionKind.BANNER)
    db.commit()
    with pytest.raises(ValidationFailed):
        content_admin.reorder_sections(db, [999])


def test_removing_a_section_closes_it(db: Session):
    """Closed, never deleted (Part II §6)."""
    section = content_admin.create_section(db, section_kind=HomepageSectionKind.BANNER)
    db.commit()

    content_admin.remove_section(db, section.pk_homepage_section_id)
    db.commit()

    assert section.scd_active_flag is False
    assert section.scd_active_to is not None
    assert content_admin.homepage_sections(db) == []
    # The row survives for history.
    assert db.scalars(select(HomepageSection)).all()


def test_auto_populating_sections_reject_manual_picks(db: Session):
    """Best Sellers curates itself; hand-picking it would be a lie (§4)."""
    section = content_admin.create_section(
        db, section_kind=HomepageSectionKind.BEST_SELLERS
    )
    db.commit()
    with pytest.raises(Conflict):
        content_admin.set_curated_items(db, section.pk_homepage_section_id, [1, 2])


def test_scheduling_controls_whether_a_section_is_live(db: Session):
    """Auto-activate and auto-expire on set dates, without a developer (§4)."""
    now = utcnow()
    section = content_admin.create_section(
        db,
        section_kind=HomepageSectionKind.BANNER,
        starts_dt=now + dt.timedelta(days=1),
    )
    db.commit()

    assert section.is_live(now) is False
    assert section.is_live(now + dt.timedelta(days=2)) is True


# ---------------------------------------------------------------------------
# Announcement bars (Part I §3.3)
# ---------------------------------------------------------------------------


def test_announcement_requires_both_languages(db: Session):
    """§1: separate Arabic and English inputs wherever a value is entered."""
    with pytest.raises(ValidationFailed):
        content_admin.create_announcement(
            db, message_ar="تنبيه", message_en=""
        )


def test_several_announcements_can_be_live_at_once(db: Session):
    """§3.3 allows zero, one, or more than one — creating never deactivates."""
    content_admin.create_announcement(
        db, message_ar="أ", message_en="A", priority=1
    )
    content_admin.create_announcement(
        db, message_ar="ب", message_en="B", priority=5
    )
    db.commit()

    bars = content_admin.announcement_bars(db)
    now = utcnow()
    assert len(bars) == 2
    assert all(bar.is_live(now) for bar in bars)
    # Higher priority stacks first.
    assert bars[0].priority == 5


def test_announcement_toggle_is_immediate(db: Session):
    """The on/off switch for use without scheduling (§3.3)."""
    bar = content_admin.create_announcement(db, message_ar="أ", message_en="A")
    db.commit()

    content_admin.toggle_announcement(db, bar.pk_announcement_bar_id, enabled=False)
    db.commit()

    assert bar.is_enabled_flag is False
    assert bar.is_live(utcnow()) is False


# ---------------------------------------------------------------------------
# Site settings (Part I §3.2)
# ---------------------------------------------------------------------------


def test_saving_a_setting_closes_the_previous_version(db: Session):
    """A true SCD write, so "who changed the footer and when" is answerable."""
    content_admin.save_setting(
        db, "footer_about", value_ar="قديم", value_en="Old", actor_user_id=1
    )
    db.commit()
    content_admin.save_setting(
        db, "footer_about", value_ar="جديد", value_en="New", actor_user_id=2
    )
    db.commit()

    rows = db.scalars(
        select(SiteSetting).where(SiteSetting.setting_key == "footer_about")
    ).all()
    assert len(rows) == 2, "the old version is kept, not overwritten"

    live = content_admin.site_settings(db)["footer_about"]
    assert live.value_text_en == "New"
    assert live.scd_changed_by == 2


def test_flag_settings_round_trip(db: Session):
    content_admin.save_setting(db, "show_view_count", value_flag=True)
    db.commit()
    assert content_admin.site_settings(db)["show_view_count"].value_flag is True


# ---------------------------------------------------------------------------
# Email templates (Part I §2.7)
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_template(db: Session) -> EmailTemplate:
    template = EmailTemplate(
        template_code=EmailTemplateCode.FORGOT_PASSWORD,
        subject_ar="إعادة تعيين", subject_en="Reset your password",
        body_ar="اضغط {reset_url} — ينتهي خلال {expiry_hours} ساعة",
        body_en="Click {reset_url} — expires in {expiry_hours} hours",
        required_placeholders="reset_url,expiry_hours",
        scd_active_from=utcnow(),
    )
    db.add(template)
    db.commit()
    return template


def test_editing_wording_is_allowed(db: Session, reset_template: EmailTemplate):
    """Admin owns the copy; the system owns the tokens (§2.7)."""
    saved = content_admin.save_email_template(
        db, EmailTemplateCode.FORGOT_PASSWORD,
        subject_ar="كلمة المرور", subject_en="Your password",
        body_ar="مرحباً، اضغط {reset_url} خلال {expiry_hours} ساعة",
        body_en="Hello, use {reset_url} within {expiry_hours} hours",
    )
    db.commit()

    assert saved.subject_en == "Your password"
    assert reset_template.scd_active_flag is False, "the old version is closed"


def test_dropping_a_required_token_is_rejected(
    db: Session, reset_template: EmailTemplate
):
    """A reset email without its link is worse than no email at all (§2.7)."""
    with pytest.raises(MissingPlaceholderError):
        content_admin.save_email_template(
            db, EmailTemplateCode.FORGOT_PASSWORD,
            subject_ar="كلمة المرور", subject_en="Your password",
            body_ar="مرحباً", body_en="Hello, no link here",
        )

    db.rollback()
    # The live template is untouched.
    live = content_admin.email_templates(db)
    assert any("{reset_url}" in t.body_en for t in live)


def test_missing_template_codes_are_reported(db: Session, reset_template: EmailTemplate):
    missing = content_admin.missing_template_codes(db)
    assert EmailTemplateCode.WELCOME in missing
    assert EmailTemplateCode.FORGOT_PASSWORD not in missing


# ---------------------------------------------------------------------------
# Branches (Part I §6)
# ---------------------------------------------------------------------------


def test_branch_requires_both_names(db: Session):
    with pytest.raises(ValidationFailed):
        content_admin.save_branch(db, name_ar="الفرع", name_en="")


def test_branch_saves_coordinates_and_hours(db: Session):
    branch = content_admin.save_branch(
        db,
        name_ar="فرع عمّان", name_en="Amman branch",
        latitude=Decimal("31.963158"), longitude=Decimal("35.930359"),
    )
    content_admin.save_branch_hours(
        db, branch.pk_branch_id,
        {0: ("09:00", "18:00", False), 5: (None, None, True)},
    )
    db.commit()

    hours = content_admin.branch_hours(db, branch.pk_branch_id)
    assert hours[0].opens_at == "09:00"
    assert hours[5].is_closed_flag is True
    assert branch.latitude == Decimal("31.963158")


def test_branch_holding_stock_cannot_be_closed(db: Session):
    """Its units would become unreachable in every report."""
    from app.models.catalog import Product, ProductVariant

    now = utcnow()
    branch = content_admin.save_branch(db, name_ar="فرع", name_en="Branch")
    db.flush()

    # A real variant: foreign keys are enforced (PRAGMA foreign_keys=ON), so a
    # dangling id would fail the insert rather than the assertion under test.
    product = Product(
        name_ar="كتاب", name_en="Book", slug_ar="كتاب", slug_en="book",
        base_price_amt=Decimal("10.000"), scd_active_from=now,
    )
    db.add(product)
    db.flush()
    variant = ProductVariant(
        fk_product_id=product.pk_product_id, sku="SKU-BRANCH", scd_active_from=now
    )
    db.add(variant)

    pool = StockPool(
        pool_kind=StockPoolKind.BRANCH, fk_branch_id=branch.pk_branch_id,
        name_ar="مخزون", name_en="Stock", scd_active_from=now,
    )
    db.add(pool)
    db.flush()
    db.add(
        StockLevel(
            fk_product_variant_id=variant.pk_product_variant_id,
            fk_stock_pool_id=pool.pk_stock_pool_id,
            quantity_on_hand=4, quantity_reserved=0,
            average_cost_amt=Decimal("1.000"), scd_active_from=now,
        )
    )
    db.commit()

    with pytest.raises(Conflict):
        content_admin.remove_branch(db, branch.pk_branch_id)


def test_empty_branch_closes(db: Session):
    branch = content_admin.save_branch(db, name_ar="فرع", name_en="Branch")
    db.commit()

    content_admin.remove_branch(db, branch.pk_branch_id)
    db.commit()

    assert branch.scd_active_flag is False
    assert content_admin.branches(db) == []


# ---------------------------------------------------------------------------
# Shipping rules (Part I §2.2)
# ---------------------------------------------------------------------------


@pytest.fixture
def geography(db: Session) -> dict:
    now = utcnow()
    jordan = Country(
        iso_code="JO", phone_code="+962", name_ar="الأردن", name_en="Jordan",
        scd_active_from=now,
    )
    db.add(jordan)
    db.flush()
    amman = Province(
        fk_country_id=jordan.pk_country_id, name_ar="عمّان", name_en="Amman",
        scd_active_from=now,
    )
    db.add(amman)
    db.commit()
    return {"jordan": jordan, "amman": amman}


def test_governorate_rule_needs_its_country(db: Session, geography: dict):
    with pytest.raises(ValidationFailed):
        content_admin.save_shipping_rule(
            db, province_id=geography["amman"].pk_province_id
        )


def test_quote_on_contact_ignores_the_cost(db: Session, geography: dict):
    """The two must never disagree — a contact rule has no price (§2.2)."""
    rule = content_admin.save_shipping_rule(
        db,
        country_id=geography["jordan"].pk_country_id,
        cost_amt=Decimal("5.000"),
        free_above_amt=Decimal("30.000"),
        quote_on_contact=True,
    )
    db.commit()

    assert rule.quote_on_contact_flag is True
    assert rule.cost_amt == Decimal("0")
    assert rule.free_above_amt is None


def test_rules_list_most_specific_first(db: Session, geography: dict):
    """Listed in the order they actually resolve."""
    content_admin.save_shipping_rule(db, cost_amt=Decimal("9.000"))
    content_admin.save_shipping_rule(
        db, country_id=geography["jordan"].pk_country_id, cost_amt=Decimal("3.500")
    )
    content_admin.save_shipping_rule(
        db,
        country_id=geography["jordan"].pk_country_id,
        province_id=geography["amman"].pk_province_id,
        cost_amt=Decimal("2.000"),
    )
    db.commit()

    rows = content_admin.shipping_rules(db)
    assert [r.scope_label for r in rows] == ["Amman", "Jordan", "Everywhere else"]


def test_removing_a_rule_closes_it(db: Session, geography: dict):
    rule = content_admin.save_shipping_rule(db, cost_amt=Decimal("1.000"))
    db.commit()

    content_admin.remove_shipping_rule(db, rule.pk_shipping_rule_id)
    db.commit()

    assert rule.scd_active_flag is False
    assert content_admin.shipping_rules(db) == []
