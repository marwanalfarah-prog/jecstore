"""Admin-managed site content (Part I §4, §3.2, §3.3, §2.7, §6, §2.2).

This is the write side of everything the spec says Admin controls **without a
developer**: the homepage layout, the storewide announcement bar, footer and
site settings, transactional email templates, branches and their hours, and the
shipping-cost rules.

Two rules shape the whole module:

* **Every editable text comes in an AR/EN pair** (§1). The helpers here take
  both and never silently accept one.
* **Nothing is deleted** (Part II §6). "Removing" a homepage section or a branch
  closes the row; the rendered site filters on ``scd_active_flag``, so it
  disappears from view while its history survives.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.enums import EmailTemplateCode, HomepageSectionKind
from app.models.identity import Country, Province
from app.models.inventory import Branch, BranchHours
from app.models.marketing import (
    AnnouncementBar,
    EmailTemplate,
    HomepageSection,
    HomepageSectionItem,
    SiteSetting,
)
from app.models.orders import ShippingRule
from app.services.email import render_template
from app.services.pricing import q

log = get_logger(__name__)

#: Section kinds that auto-populate and therefore need no curated item list.
AUTO_SECTION_KINDS = frozenset({
    HomepageSectionKind.NEW_ARRIVALS,
    HomepageSectionKind.DISCOUNTED,
    HomepageSectionKind.BEST_SELLERS,
    HomepageSectionKind.MOST_VIEWED,
    HomepageSectionKind.PUBLISHER_CAROUSEL,
    HomepageSectionKind.CATEGORY_SHOWCASE,
})

#: Settings the footer and storefront read. Kept here so the settings screen can
#: render a known list rather than whatever happens to be in the table.
SETTING_KEYS: tuple[tuple[str, str], ...] = (
    ("footer_about", "text"),
    ("contact_phone", "text"),
    ("contact_email", "text"),
    ("whatsapp_number", "text"),
    ("facebook_url", "text"),
    ("facebook_page_url", "text"),
    ("messenger_url", "text"),
    ("instagram_url", "text"),
    ("show_view_count", "flag"),
    ("show_purchase_count", "flag"),
)


def _require_bilingual(ar: str | None, en: str | None, field: str) -> tuple[str, str]:
    """Both languages or neither — §1 requires separate AR and EN inputs."""
    ar, en = (ar or "").strip(), (en or "").strip()
    if not ar or not en:
        raise ValidationFailed(f"Enter {field} in both Arabic and English.")
    return ar, en


# ---------------------------------------------------------------------------
# Homepage sections (Part I §4)
# ---------------------------------------------------------------------------


def homepage_sections(db: Session) -> list[HomepageSection]:
    """Every section in display order — including disabled and expired ones,
    because the builder must show what it can edit, not what a visitor sees."""
    return list(
        db.scalars(
            select(HomepageSection)
            .where(HomepageSection.scd_active_flag.is_(True))
            .order_by(HomepageSection.sort_order, HomepageSection.pk_homepage_section_id)
        ).all()
    )


def create_section(
    db: Session,
    *,
    section_kind: str,
    title_ar: str | None = None,
    title_en: str | None = None,
    subtitle_ar: str | None = None,
    subtitle_en: str | None = None,
    category_id: int | None = None,
    item_limit: int = 12,
    link_url: str | None = None,
    image_path_ar: str | None = None,
    image_path_en: str | None = None,
    starts_dt: dt.datetime | None = None,
    ends_dt: dt.datetime | None = None,
    is_enabled: bool = True,
    actor_user_id: int | None = None,
) -> HomepageSection:
    """Add a section, appended to the end of the page."""
    if section_kind not in set(HomepageSectionKind):
        raise ValidationFailed("Choose a section type.")

    now = utcnow()
    last = db.scalar(
        select(func.coalesce(func.max(HomepageSection.sort_order), -1)).where(
            HomepageSection.scd_active_flag.is_(True)
        )
    )

    section = HomepageSection(
        section_kind=section_kind,
        title_ar=(title_ar or "").strip() or None,
        title_en=(title_en or "").strip() or None,
        subtitle_ar=(subtitle_ar or "").strip() or None,
        subtitle_en=(subtitle_en or "").strip() or None,
        fk_category_id=category_id,
        item_limit=max(1, min(int(item_limit or 12), 48)),
        link_url=(link_url or "").strip() or None,
        image_path_ar=(image_path_ar or "").strip() or None,
        image_path_en=(image_path_en or "").strip() or None,
        starts_dt=starts_dt,
        ends_dt=ends_dt,
        is_enabled_flag=bool(is_enabled),
        sort_order=int(last) + 1,
        scd_active_from=now,
        scd_changed_by=actor_user_id,
    )
    db.add(section)
    db.flush()

    log.info("homepage_section_created", extra={"kind": section_kind})
    return section


def update_section(
    db: Session, section_id: int, *, actor_user_id: int | None = None, **fields
) -> HomepageSection:
    section = _section(db, section_id)

    for name in (
        "title_ar", "title_en", "subtitle_ar", "subtitle_en",
        "link_url", "image_path_ar", "image_path_en",
    ):
        if name in fields:
            value = (fields[name] or "").strip() or None
            setattr(section, name, value)

    if "category_id" in fields:
        section.fk_category_id = fields["category_id"]
    if "item_limit" in fields and fields["item_limit"]:
        section.item_limit = max(1, min(int(fields["item_limit"]), 48))
    if "starts_dt" in fields:
        section.starts_dt = fields["starts_dt"]
    if "ends_dt" in fields:
        section.ends_dt = fields["ends_dt"]
    if "is_enabled" in fields:
        section.is_enabled_flag = bool(fields["is_enabled"])

    section.scd_changed_by = actor_user_id
    return section


def reorder_sections(
    db: Session, ordered_ids: list[int], *, actor_user_id: int | None = None
) -> None:
    """Apply a new display order — what the drag-and-drop builder writes (§4).

    Takes the full ordered list rather than a move instruction, so a dropped or
    duplicated id cannot leave the page in a half-reordered state.
    """
    sections = {s.pk_homepage_section_id: s for s in homepage_sections(db)}
    unknown = [i for i in ordered_ids if i not in sections]
    if unknown:
        raise ValidationFailed("That section is not on the homepage.")

    for position, section_id in enumerate(ordered_ids):
        sections[section_id].sort_order = position
        sections[section_id].scd_changed_by = actor_user_id

    # Anything not named keeps a stable position after the ordered block.
    for offset, (section_id, section) in enumerate(
        (sid, s) for sid, s in sections.items() if sid not in ordered_ids
    ):
        section.sort_order = len(ordered_ids) + offset

    log.info("homepage_reordered", extra={"count": len(ordered_ids)})


def remove_section(
    db: Session, section_id: int, *, actor_user_id: int | None = None
) -> HomepageSection:
    """Close the section. Never deleted (Part II §6)."""
    section = _section(db, section_id)
    section.close(changed_by=actor_user_id)
    return section


def set_curated_items(
    db: Session, section_id: int, product_ids: list[int], *, actor_user_id: int | None = None
) -> HomepageSection:
    """Replace a curated section's picks ("our picks for you", §4).

    Old rows are closed and new ones inserted rather than edited, so the history
    of what was promoted when survives.
    """
    section = _section(db, section_id)
    if section.section_kind in AUTO_SECTION_KINDS:
        raise Conflict("This section populates itself and takes no manual picks.")

    now = utcnow()
    for existing in db.scalars(
        select(HomepageSectionItem).where(
            HomepageSectionItem.fk_homepage_section_id == section_id,
            HomepageSectionItem.scd_active_flag.is_(True),
        )
    ).all():
        existing.close(changed_by=actor_user_id, at=now)

    for position, product_id in enumerate(product_ids):
        db.add(
            HomepageSectionItem(
                fk_homepage_section_id=section_id,
                fk_product_id=product_id,
                sort_order=position,
                scd_active_from=now,
                scd_changed_by=actor_user_id,
            )
        )

    return section


def curated_items(db: Session, section_id: int) -> list[HomepageSectionItem]:
    return list(
        db.scalars(
            select(HomepageSectionItem)
            .where(
                HomepageSectionItem.fk_homepage_section_id == section_id,
                HomepageSectionItem.scd_active_flag.is_(True),
            )
            .order_by(HomepageSectionItem.sort_order)
        ).all()
    )


def _section(db: Session, section_id: int) -> HomepageSection:
    section = db.scalars(
        select(HomepageSection).where(
            HomepageSection.pk_homepage_section_id == section_id,
            HomepageSection.scd_active_flag.is_(True),
        )
    ).first()
    if section is None:
        raise NotFound("That homepage section does not exist.")
    return section


# ---------------------------------------------------------------------------
# Announcement bars (Part I §3.3)
# ---------------------------------------------------------------------------


def announcement_bars(db: Session) -> list[AnnouncementBar]:
    return list(
        db.scalars(
            select(AnnouncementBar)
            .where(AnnouncementBar.scd_active_flag.is_(True))
            .order_by(
                AnnouncementBar.priority.desc(),
                AnnouncementBar.pk_announcement_bar_id.desc(),
            )
        ).all()
    )


def create_announcement(
    db: Session,
    *,
    message_ar: str,
    message_en: str,
    link_url: str | None = None,
    starts_dt: dt.datetime | None = None,
    ends_dt: dt.datetime | None = None,
    is_enabled: bool = True,
    is_dismissible: bool = True,
    priority: int = 0,
    background_hex: str | None = None,
    text_hex: str | None = None,
    actor_user_id: int | None = None,
) -> AnnouncementBar:
    """Create a storewide bar.

    §3.3 allows zero, one, or several live at once, so nothing here deactivates
    the others — ``priority`` decides stacking.
    """
    message_ar, message_en = _require_bilingual(message_ar, message_en, "the message")

    bar = AnnouncementBar(
        message_ar=message_ar,
        message_en=message_en,
        link_url=(link_url or "").strip() or None,
        starts_dt=starts_dt,
        ends_dt=ends_dt,
        is_enabled_flag=bool(is_enabled),
        is_dismissible_flag=bool(is_dismissible),
        priority=int(priority or 0),
        background_hex=(background_hex or "").strip() or None,
        text_hex=(text_hex or "").strip() or None,
        scd_active_from=utcnow(),
        scd_changed_by=actor_user_id,
    )
    db.add(bar)
    db.flush()
    return bar


def toggle_announcement(
    db: Session, bar_id: int, *, enabled: bool, actor_user_id: int | None = None
) -> AnnouncementBar:
    """The on/off switch §3.3 asks for — immediate use without scheduling."""
    bar = db.scalars(
        select(AnnouncementBar).where(
            AnnouncementBar.pk_announcement_bar_id == bar_id,
            AnnouncementBar.scd_active_flag.is_(True),
        )
    ).first()
    if bar is None:
        raise NotFound("That announcement does not exist.")
    bar.is_enabled_flag = bool(enabled)
    bar.scd_changed_by = actor_user_id
    return bar


def remove_announcement(
    db: Session, bar_id: int, *, actor_user_id: int | None = None
) -> AnnouncementBar:
    bar = db.scalars(
        select(AnnouncementBar).where(
            AnnouncementBar.pk_announcement_bar_id == bar_id,
            AnnouncementBar.scd_active_flag.is_(True),
        )
    ).first()
    if bar is None:
        raise NotFound("That announcement does not exist.")
    bar.close(changed_by=actor_user_id)
    return bar


# ---------------------------------------------------------------------------
# Site settings / footer (Part I §3.2)
# ---------------------------------------------------------------------------


def site_settings(db: Session) -> dict[str, SiteSetting]:
    return {
        row.setting_key: row
        for row in db.scalars(
            select(SiteSetting).where(SiteSetting.scd_active_flag.is_(True))
        ).all()
    }


def save_setting(
    db: Session,
    key: str,
    *,
    value_ar: str | None = None,
    value_en: str | None = None,
    value_flag: bool | None = None,
    value_number: Decimal | None = None,
    actor_user_id: int | None = None,
) -> SiteSetting:
    """Update a setting by closing the old version and inserting a new one.

    A true SCD write rather than an in-place edit, so "who changed the footer
    and when" is answerable — which is the point of the convention.
    """
    now = utcnow()
    existing = db.scalars(
        select(SiteSetting).where(
            SiteSetting.setting_key == key,
            SiteSetting.scd_active_flag.is_(True),
        )
    ).first()

    if existing is not None:
        existing.close(changed_by=actor_user_id, at=now)

    replacement = SiteSetting(
        setting_key=key,
        value_text_ar=(value_ar or "").strip() or None,
        value_text_en=(value_en or "").strip() or None,
        value_flag=value_flag,
        value_number=q(value_number) if value_number is not None else None,
        description=existing.description if existing else None,
        scd_active_from=now,
        scd_changed_by=actor_user_id,
    )
    db.add(replacement)
    db.flush()
    return replacement


# ---------------------------------------------------------------------------
# Email templates (Part I §2.7)
# ---------------------------------------------------------------------------


def email_templates(db: Session) -> list[EmailTemplate]:
    return list(
        db.scalars(
            select(EmailTemplate)
            .where(EmailTemplate.scd_active_flag.is_(True))
            .order_by(EmailTemplate.template_code)
        ).all()
    )


def save_email_template(
    db: Session,
    template_code: str,
    *,
    subject_ar: str,
    subject_en: str,
    body_ar: str,
    body_en: str,
    is_enabled: bool = True,
    actor_user_id: int | None = None,
) -> EmailTemplate:
    """Update wording and branding, keeping the tokens the system needs.

    §2.7 splits ownership: the system defines the required technical fields (a
    reset link, an expiry), Admin owns everything around them. So a save that
    would drop a required placeholder is **rejected here**, rather than
    discovered later by a locked-out customer.
    """
    subject_ar, subject_en = _require_bilingual(subject_ar, subject_en, "the subject")
    if not (body_ar or "").strip() or not (body_en or "").strip():
        raise ValidationFailed("Enter the body in both Arabic and English.")

    existing = db.scalars(
        select(EmailTemplate).where(
            EmailTemplate.template_code == template_code,
            EmailTemplate.scd_active_flag.is_(True),
        )
    ).first()
    if existing is None:
        raise NotFound("That email template does not exist.")

    now = utcnow()
    candidate = EmailTemplate(
        template_code=template_code,
        subject_ar=subject_ar,
        subject_en=subject_en,
        body_ar=body_ar.strip(),
        body_en=body_en.strip(),
        required_placeholders=existing.required_placeholders,
        is_enabled_flag=bool(is_enabled),
        scd_active_from=now,
        scd_changed_by=actor_user_id,
    )

    # Validate both languages before writing anything — render_template raises
    # MissingPlaceholderError if a required token was removed.
    for language in ("ar", "en"):
        render_template(candidate, language, {})

    existing.close(changed_by=actor_user_id, at=now)
    db.add(candidate)
    db.flush()

    log.info("email_template_saved", extra={"template_code": template_code})
    return candidate


def missing_template_codes(db: Session) -> list[str]:
    """Template codes the system expects but the database does not have."""
    present = {t.template_code for t in email_templates(db)}
    return [code for code in EmailTemplateCode if code not in present]


# ---------------------------------------------------------------------------
# Branches (Part I §6)
# ---------------------------------------------------------------------------

#: 0 = Sunday, matching the local working week.
WEEKDAYS: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)


def branches(db: Session) -> list[Branch]:
    return list(
        db.scalars(
            select(Branch)
            .where(Branch.scd_active_flag.is_(True))
            .order_by(Branch.sort_order, Branch.pk_branch_id)
        ).all()
    )


def branch_hours(db: Session, branch_id: int) -> dict[int, BranchHours]:
    return {
        row.weekday: row
        for row in db.scalars(
            select(BranchHours).where(
                BranchHours.fk_branch_id == branch_id,
                BranchHours.scd_active_flag.is_(True),
            )
        ).all()
    }


def save_branch(
    db: Session,
    *,
    branch_id: int | None = None,
    name_ar: str,
    name_en: str,
    phone_country_code: str | None = None,
    phone_number: str | None = None,
    address_ar: str | None = None,
    address_en: str | None = None,
    latitude: Decimal | None = None,
    longitude: Decimal | None = None,
    is_pickup_point: bool = True,
    sort_order: int = 0,
    actor_user_id: int | None = None,
) -> Branch:
    """Create or update a branch (§6: name, phone, lat/long for the map)."""
    name_ar, name_en = _require_bilingual(name_ar, name_en, "the branch name")

    if branch_id is None:
        branch = Branch(scd_active_from=utcnow())
        db.add(branch)
    else:
        branch = db.scalars(
            select(Branch).where(
                Branch.pk_branch_id == branch_id, Branch.scd_active_flag.is_(True)
            )
        ).first()
        if branch is None:
            raise NotFound("That branch does not exist.")

    branch.name_ar = name_ar
    branch.name_en = name_en
    branch.phone_country_code = (phone_country_code or "").strip() or None
    branch.phone_number = (phone_number or "").strip() or None
    branch.address_ar = (address_ar or "").strip() or None
    branch.address_en = (address_en or "").strip() or None
    branch.latitude = latitude
    branch.longitude = longitude
    branch.is_pickup_point_flag = bool(is_pickup_point)
    branch.sort_order = int(sort_order or 0)
    branch.scd_changed_by = actor_user_id

    db.flush()
    return branch


def save_branch_hours(
    db: Session,
    branch_id: int,
    hours: dict[int, tuple[str | None, str | None, bool]],
    *,
    actor_user_id: int | None = None,
) -> None:
    """Set the weekly opening hours.

    ``hours`` maps weekday → ``(opens_at, closes_at, is_closed)``. One-off
    closures are homepage announcements instead, per the decision in §4 — this
    is the standing schedule only.
    """
    existing = branch_hours(db, branch_id)
    now = utcnow()

    for weekday, (opens_at, closes_at, is_closed) in hours.items():
        if weekday not in WEEKDAYS:
            continue
        row = existing.get(weekday)
        if row is None:
            row = BranchHours(
                fk_branch_id=branch_id, weekday=weekday, scd_active_from=now
            )
            db.add(row)
        row.opens_at = (opens_at or "").strip() or None
        row.closes_at = (closes_at or "").strip() or None
        row.is_closed_flag = bool(is_closed)
        row.scd_changed_by = actor_user_id

    db.flush()


def remove_branch(
    db: Session, branch_id: int, *, actor_user_id: int | None = None
) -> Branch:
    """Close a branch, refusing while it still holds stock.

    A branch with stock on its shelves cannot simply vanish — the units would
    become unreachable in every report.
    """
    from app.models.inventory import StockLevel, StockPool

    branch = db.scalars(
        select(Branch).where(
            Branch.pk_branch_id == branch_id, Branch.scd_active_flag.is_(True)
        )
    ).first()
    if branch is None:
        raise NotFound("That branch does not exist.")

    held = db.scalar(
        select(func.coalesce(func.sum(StockLevel.quantity_on_hand), 0))
        .select_from(StockLevel)
        .join(StockPool, StockPool.pk_stock_pool_id == StockLevel.fk_stock_pool_id)
        .where(
            StockPool.fk_branch_id == branch_id,
            StockPool.scd_active_flag.is_(True),
            StockLevel.scd_active_flag.is_(True),
        )
    ) or 0
    if held > 0:
        raise Conflict(
            "Move or write off this branch's stock before closing it.",
            details={"units_on_hand": int(held)},
        )

    branch.close(changed_by=actor_user_id)
    return branch


# ---------------------------------------------------------------------------
# Shipping rules (Part I §2.2)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ShippingRuleRow:
    rule: ShippingRule
    country: Country | None
    province: Province | None

    @property
    def scope_label(self) -> str:
        if self.province is not None:
            return self.province.name_en
        if self.country is not None:
            return self.country.name_en
        return "Everywhere else"


def shipping_rules(db: Session) -> list[ShippingRuleRow]:
    """Rules with their place names, most specific first — the order they
    actually resolve in (``services/shipping.py``)."""
    rules = db.scalars(
        select(ShippingRule)
        .where(ShippingRule.scd_active_flag.is_(True))
        .order_by(ShippingRule.priority.desc(), ShippingRule.pk_shipping_rule_id)
    ).all()

    countries = {
        c.pk_country_id: c
        for c in db.scalars(select(Country).where(Country.scd_active_flag.is_(True))).all()
    }
    provinces = {
        p.pk_province_id: p
        for p in db.scalars(select(Province).where(Province.scd_active_flag.is_(True))).all()
    }

    rows = [
        ShippingRuleRow(
            rule=rule,
            country=countries.get(rule.fk_country_id),
            province=provinces.get(rule.fk_province_id),
        )
        for rule in rules
    ]
    # Governorate rules first, then country, then the global fallback.
    rows.sort(
        key=lambda r: (
            2 if r.province else 1 if r.country else 0,
            r.rule.priority,
        ),
        reverse=True,
    )
    return rows


def save_shipping_rule(
    db: Session,
    *,
    rule_id: int | None = None,
    country_id: int | None = None,
    province_id: int | None = None,
    cost_amt: Decimal = Decimal("0"),
    free_above_amt: Decimal | None = None,
    quote_on_contact: bool = False,
    priority: int = 0,
    note_ar: str | None = None,
    note_en: str | None = None,
    actor_user_id: int | None = None,
) -> ShippingRule:
    """Create or update a shipping rule (§2.2).

    ``quote_on_contact`` is the third outcome the spec requires alongside a
    price: "not included, will be contacted". A rule with it set ignores the
    cost, so the two can never disagree.
    """
    if province_id is not None and country_id is None:
        raise ValidationFailed("A governorate rule also needs its country.")
    if cost_amt is not None and Decimal(cost_amt) < 0:
        raise ValidationFailed("Shipping cost cannot be negative.")

    if rule_id is None:
        rule = ShippingRule(scd_active_from=utcnow())
        db.add(rule)
    else:
        rule = db.scalars(
            select(ShippingRule).where(
                ShippingRule.pk_shipping_rule_id == rule_id,
                ShippingRule.scd_active_flag.is_(True),
            )
        ).first()
        if rule is None:
            raise NotFound("That shipping rule does not exist.")

    rule.fk_country_id = country_id
    rule.fk_province_id = province_id
    rule.quote_on_contact_flag = bool(quote_on_contact)
    rule.cost_amt = Decimal("0") if quote_on_contact else q(Decimal(cost_amt or 0))
    rule.free_above_amt = (
        None if quote_on_contact or free_above_amt is None else q(Decimal(free_above_amt))
    )
    rule.priority = int(priority or 0)
    rule.note_ar = (note_ar or "").strip() or None
    rule.note_en = (note_en or "").strip() or None
    rule.scd_changed_by = actor_user_id

    db.flush()
    return rule


def remove_shipping_rule(
    db: Session, rule_id: int, *, actor_user_id: int | None = None
) -> ShippingRule:
    rule = db.scalars(
        select(ShippingRule).where(
            ShippingRule.pk_shipping_rule_id == rule_id,
            ShippingRule.scd_active_flag.is_(True),
        )
    ).first()
    if rule is None:
        raise NotFound("That shipping rule does not exist.")
    rule.close(changed_by=actor_user_id)
    return rule


def countries_and_provinces(db: Session) -> tuple[list[Country], list[Province]]:
    countries = list(
        db.scalars(
            select(Country)
            .where(Country.scd_active_flag.is_(True))
            .order_by(Country.sort_order, Country.pk_country_id)
        ).all()
    )
    provinces = list(
        db.scalars(
            select(Province)
            .where(Province.scd_active_flag.is_(True))
            .order_by(Province.sort_order, Province.pk_province_id)
        ).all()
    )
    return countries, provinces
