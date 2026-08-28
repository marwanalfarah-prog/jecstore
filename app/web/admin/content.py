"""Admin content screens: homepage, announcements, settings, email templates,
shipping rules (Part I §4, §3.2, §3.3, §2.7, §2.2).

Everything here is what the spec means by "Admin rearranges without a
developer". Branches live in their own module because §6 gives them their own
data (hours, coordinates) and their own permission.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailed
from app.core.templating import templates
from app.db.session import get_db
from app.models.enums import EmailTemplateCode, HomepageSectionKind
from app.models.identity import User
from app.services import catalog_admin, content_admin
from app.services.permissions import GrantDecision
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/content")


# ---------------------------------------------------------------------------
# Homepage builder (Part I §4)
# ---------------------------------------------------------------------------


@router.get("")
def content_home(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_homepage")),
    db: Session = Depends(get_db),
) -> Response:
    sections = content_admin.homepage_sections(db)
    return templates.TemplateResponse(
        request,
        "admin/content/homepage.html",
        admin_context(
            db, staff,
            sections=sections,
            curated={
                s.pk_homepage_section_id: content_admin.curated_items(
                    db, s.pk_homepage_section_id
                )
                for s in sections
                if s.section_kind not in content_admin.AUTO_SECTION_KINDS
            },
            section_kinds=list(HomepageSectionKind),
            auto_kinds=content_admin.AUTO_SECTION_KINDS,
            categories=catalog_admin.active_categories(db),
            products=catalog_admin.product_options(db),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/homepage/sections")
async def create_section(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_homepage")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    content_admin.create_section(
        db,
        section_kind=str(form.get("section_kind") or ""),
        title_ar=str(form.get("title_ar") or ""),
        title_en=str(form.get("title_en") or ""),
        subtitle_ar=str(form.get("subtitle_ar") or ""),
        subtitle_en=str(form.get("subtitle_en") or ""),
        category_id=_int(form.get("category_id")),
        item_limit=_int(form.get("item_limit")) or 12,
        link_url=str(form.get("link_url") or ""),
        starts_dt=_datetime(form.get("starts_dt")),
        ends_dt=_datetime(form.get("ends_dt")),
        is_enabled=form.get("is_enabled") == "1",
        actor_user_id=staff.pk_user_id,
    )
    db.commit()
    return _back("saved")


@router.post("/homepage/sections/{section_id}")
async def update_section(
    request: Request,
    section_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_homepage")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    content_admin.update_section(
        db, section_id,
        actor_user_id=staff.pk_user_id,
        title_ar=str(form.get("title_ar") or ""),
        title_en=str(form.get("title_en") or ""),
        subtitle_ar=str(form.get("subtitle_ar") or ""),
        subtitle_en=str(form.get("subtitle_en") or ""),
        link_url=str(form.get("link_url") or ""),
        category_id=_int(form.get("category_id")),
        item_limit=_int(form.get("item_limit")),
        starts_dt=_datetime(form.get("starts_dt")),
        ends_dt=_datetime(form.get("ends_dt")),
        is_enabled=form.get("is_enabled") == "1",
    )
    db.commit()
    return _back("saved")


@router.post("/homepage/reorder")
async def reorder_sections(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_homepage")),
    db: Session = Depends(get_db),
) -> Response:
    """Apply a new section order.

    Accepts the full ordered list — from the drag-and-drop builder as JSON, or
    from the no-JS fallback as repeated form fields. Taking the whole order
    rather than a move instruction means a dropped id cannot leave the page
    half-reordered.
    """
    ordered: list[int] = []
    if request.headers.get("content-type", "").startswith("application/json"):
        payload = await request.json()
        ordered = [int(i) for i in payload.get("order", [])]
    else:
        form = await request.form()
        ordered = [int(i) for i in form.getlist("section_id") if str(i).strip()]

    if not ordered:
        raise ValidationFailed("Nothing to reorder.")

    content_admin.reorder_sections(db, ordered, actor_user_id=staff.pk_user_id)
    db.commit()

    if request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"ok": True, "order": ordered})
    return _back("saved")


@router.post("/homepage/sections/{section_id}/remove")
def remove_section(
    request: Request,
    section_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_homepage")),
    db: Session = Depends(get_db),
) -> Response:
    content_admin.remove_section(db, section_id, actor_user_id=staff.pk_user_id)
    db.commit()
    return _back("saved")


@router.post("/homepage/sections/{section_id}/items")
async def set_curated_items(
    request: Request,
    section_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_homepage")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    product_ids = [int(i) for i in form.getlist("product_id") if str(i).strip()]
    content_admin.set_curated_items(
        db, section_id, product_ids, actor_user_id=staff.pk_user_id
    )
    db.commit()
    return _back("saved")


# ---------------------------------------------------------------------------
# Announcement bars (Part I §3.3)
# ---------------------------------------------------------------------------


@router.get("/announcements")
def announcements(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_announcements")),
    db: Session = Depends(get_db),
) -> Response:
    return templates.TemplateResponse(
        request,
        "admin/content/announcements.html",
        admin_context(
            db, staff,
            bars=content_admin.announcement_bars(db),
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/announcements")
async def create_announcement(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_announcements")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    content_admin.create_announcement(
        db,
        message_ar=str(form.get("message_ar") or ""),
        message_en=str(form.get("message_en") or ""),
        link_url=str(form.get("link_url") or ""),
        starts_dt=_datetime(form.get("starts_dt")),
        ends_dt=_datetime(form.get("ends_dt")),
        is_enabled=form.get("is_enabled") == "1",
        is_dismissible=form.get("is_dismissible") == "1",
        priority=_int(form.get("priority")) or 0,
        actor_user_id=staff.pk_user_id,
    )
    db.commit()
    return _back("saved", "/admin/content/announcements")


@router.post("/announcements/{bar_id}/toggle")
def toggle_announcement(
    request: Request,
    bar_id: int,
    enabled: str = Form("0"),
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_announcements")),
    db: Session = Depends(get_db),
) -> Response:
    content_admin.toggle_announcement(
        db, bar_id, enabled=enabled == "1", actor_user_id=staff.pk_user_id
    )
    db.commit()
    return _back("saved", "/admin/content/announcements")


@router.post("/announcements/{bar_id}/remove")
def remove_announcement(
    request: Request,
    bar_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_announcements")),
    db: Session = Depends(get_db),
) -> Response:
    content_admin.remove_announcement(db, bar_id, actor_user_id=staff.pk_user_id)
    db.commit()
    return _back("saved", "/admin/content/announcements")


# ---------------------------------------------------------------------------
# Site settings / footer (Part I §3.2)
# ---------------------------------------------------------------------------


@router.get("/settings")
def settings_page(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_footer")),
    db: Session = Depends(get_db),
) -> Response:
    return templates.TemplateResponse(
        request,
        "admin/content/settings.html",
        admin_context(
            db, staff,
            settings=content_admin.site_settings(db),
            setting_keys=content_admin.SETTING_KEYS,
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("content", "manage_footer")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    for key, kind in content_admin.SETTING_KEYS:
        if kind == "flag":
            content_admin.save_setting(
                db, key,
                value_flag=form.get(f"{key}__flag") == "1",
                actor_user_id=staff.pk_user_id,
            )
        else:
            content_admin.save_setting(
                db, key,
                value_ar=str(form.get(f"{key}__ar") or ""),
                value_en=str(form.get(f"{key}__en") or ""),
                actor_user_id=staff.pk_user_id,
            )
    db.commit()
    return _back("saved", "/admin/content/settings")


# ---------------------------------------------------------------------------
# Email templates (Part I §2.7)
# ---------------------------------------------------------------------------


@router.get("/emails")
def email_templates(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(
        require_permission("content", "manage_email_templates")
    ),
    db: Session = Depends(get_db),
) -> Response:
    return templates.TemplateResponse(
        request,
        "admin/content/emails.html",
        admin_context(
            db, staff,
            email_templates=content_admin.email_templates(db),
            missing=content_admin.missing_template_codes(db),
            flash=request.query_params.get("flash"),
            error=request.query_params.get("error"),
        ),
    )


@router.post("/emails/{template_code}")
async def save_email_template(
    request: Request,
    template_code: str,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(
        require_permission("content", "manage_email_templates")
    ),
    db: Session = Depends(get_db),
) -> Response:
    """Save wording and branding.

    A save that drops a required placeholder is rejected — the system owns those
    fields even though Admin owns the copy around them (§2.7).
    """
    from app.services.email import MissingPlaceholderError

    form = await request.form()
    try:
        content_admin.save_email_template(
            db, template_code,
            subject_ar=str(form.get("subject_ar") or ""),
            subject_en=str(form.get("subject_en") or ""),
            body_ar=str(form.get("body_ar") or ""),
            body_en=str(form.get("body_en") or ""),
            is_enabled=form.get("is_enabled") == "1",
            actor_user_id=staff.pk_user_id,
        )
    except MissingPlaceholderError as exc:
        db.rollback()
        return RedirectResponse(
            f"/admin/content/emails?error={exc}", status_code=status.HTTP_303_SEE_OTHER
        )

    db.commit()
    return _back("saved", "/admin/content/emails")


# ---------------------------------------------------------------------------
# Shipping rules (Part I §2.2)
# ---------------------------------------------------------------------------


@router.get("/shipping")
def shipping_rules(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("shipping", "view")),
    db: Session = Depends(get_db),
) -> Response:
    countries, provinces = content_admin.countries_and_provinces(db)
    return templates.TemplateResponse(
        request,
        "admin/content/shipping.html",
        admin_context(
            db, staff,
            rules=content_admin.shipping_rules(db),
            countries=countries,
            provinces=provinces,
            flash=request.query_params.get("flash"),
        ),
    )


@router.post("/shipping")
async def save_shipping_rule(
    request: Request,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("shipping", "manage_rules")),
    db: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    content_admin.save_shipping_rule(
        db,
        rule_id=_int(form.get("rule_id")),
        country_id=_int(form.get("country_id")),
        province_id=_int(form.get("province_id")),
        cost_amt=_decimal(form.get("cost_amt")) or Decimal("0"),
        free_above_amt=_decimal(form.get("free_above_amt")),
        quote_on_contact=form.get("quote_on_contact") == "1",
        priority=_int(form.get("priority")) or 0,
        note_ar=str(form.get("note_ar") or ""),
        note_en=str(form.get("note_en") or ""),
        actor_user_id=staff.pk_user_id,
    )
    db.commit()
    return _back("saved", "/admin/content/shipping")


@router.post("/shipping/{rule_id}/remove")
def remove_shipping_rule(
    request: Request,
    rule_id: int,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("shipping", "manage_rules")),
    db: Session = Depends(get_db),
) -> Response:
    content_admin.remove_shipping_rule(db, rule_id, actor_user_id=staff.pk_user_id)
    db.commit()
    return _back("saved", "/admin/content/shipping")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _back(flash: str, path: str = "/admin/content") -> RedirectResponse:
    return RedirectResponse(f"{path}?flash={flash}", status_code=status.HTTP_303_SEE_OTHER)


def _int(raw) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _decimal(raw) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValidationFailed("That is not a valid number.") from exc


def _datetime(raw) -> dt.datetime | None:
    """Parse a ``datetime-local`` value as UTC.

    Stored UTC per Part II §1; the admin panel edits in local time, so this is
    the one conversion point.
    """
    if raw is None or not str(raw).strip():
        return None
    try:
        naive = dt.datetime.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise ValidationFailed("That is not a valid date and time.") from exc
    return naive.replace(tzinfo=dt.timezone.utc) if naive.tzinfo is None else naive
