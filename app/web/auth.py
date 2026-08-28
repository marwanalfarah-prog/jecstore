"""Registration, login and logout (Part I §2.4, §2.5, §2.7).

Two rules from the spec shape everything here:

* **Nothing is ever deleted.** A failed or abandoned registration leaves no
  orphan; an unverified account simply persists, unable to check out, with a
  "Not Verified" badge and a resend button (§2.5).
* **Abuse protection is progressive.** The CAPTCHA appears after repeated
  failures rather than on every request, and lockout is time-boxed, so a
  legitimate customer who mistypes twice is not punished (§2.4).
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.context import get_context
from app.core.errors import RateLimited, ValidationFailed
from app.core.logging import get_logger
from app.core.security import (
    clean_text,
    hash_password,
    is_valid_username,
    normalize_country_code,
    normalize_email,
    normalize_phone,
    normalize_username,
    password_problems,
    verify_password,
)
from app.core.templating import templates
from app.db.base import utcnow
from app.db.session import get_db
from app.models.enums import (
    ActivityEvent,
    AuthTokenPurpose,
    CustomerType,
    EmailTemplateCode,
    RoleCode,
    SessionEndReason,
)
from app.models.identity import (
    Address,
    CompanyProfile,
    Country,
    IndividualProfile,
    Province,
    Role,
    User,
)
from app.models.marketing import NewsletterSubscription
from app.services.email import queue_template_email
from app.services import accounts, rate_limit, tokens
from app.services.activity import record_event, record_login_attempt, recent_failed_attempts
from app.services.sessions import (
    clear_session_cookie,
    client_ip,
    create_session,
    end_session,
    set_session_cookie,
)

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)) -> Response:
    if get_context(request).is_authenticated:
        return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "auth/login.html",
        {
            "next_url": request.query_params.get("next", "/"),
            "captcha_required": _captcha_required(db, request),
        },
    )


@router.post("/login")
def login_submit(
    request: Request,
    identifier: str = Form(...),
    password: str = Form(...),
    remember: str | None = Form(None),
    next: str = Form("/"),
    db: Session = Depends(get_db),
) -> Response:
    identifier = normalize_username(identifier)
    ip = client_ip(request)

    # Two checks, deliberately in different places (Part II §6).
    #
    # The per-minute burst limit is a cache counter: it runs on every request,
    # so it must not be a hot write against the transactional tables.
    burst = rate_limit.limiter().check(rate_limit.login_policy(), ip or "unknown")
    if not burst.allowed:
        record_login_attempt(
            db, request, identifier, success=False, failure_reason="rate_limited"
        )
        record_event(db, ActivityEvent.RATE_LIMITED, request=request, detail=identifier)
        db.commit()
        raise RateLimited(details={"retry_after_seconds": burst.retry_after})

    # The account lockout is slower and coarser, and stays on the durable
    # ledger: it must survive a cache restart, since "this account is under
    # attack" is not something to forget because Redis blinked.
    # Checked before any password work, so a locked account cannot be used as a
    # timing oracle either (Part I §2.4).
    failures = recent_failed_attempts(
        db, identifier=identifier, within_minutes=settings.login_lockout_minutes
    )
    if failures >= settings.login_lockout_threshold:
        record_login_attempt(
            db, request, identifier, success=False,
            failure_reason="locked_out", lockout_triggered=True,
        )
        record_event(db, ActivityEvent.ACCOUNT_LOCKED, request=request, detail=identifier)
        db.commit()
        raise RateLimited(
            "Too many failed attempts. Please wait a few minutes and try again."
        )

    user = _find_user(db, identifier)

    # Verify against a real hash even when the user does not exist, so response
    # time does not reveal which usernames are registered.
    stored_hash = user.password_hash if user else _DUMMY_HASH
    password_ok = verify_password(password, stored_hash)

    if user is None or not password_ok or not user.is_active_flag:
        record_login_attempt(
            db, request, identifier,
            success=False,
            user_id=user.pk_user_id if user else None,
            failure_reason="bad_credentials" if user else "unknown_user",
            captcha_required=failures + 1 >= settings.captcha_failed_attempts_threshold,
        )
        record_event(db, ActivityEvent.LOGIN_FAILED, request=request, success=False)
        db.commit()

        # One message for every failure mode: never confirm that a username
        # exists, and never hint at which half was wrong.
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {
                "error": "Those details do not match an account.",
                "identifier": identifier,
                "next_url": next,
                "captcha_required": failures + 1 >= settings.captcha_failed_attempts_threshold,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    session = create_session(db, user, request)
    # A customer who mistyped twice should not still be near their limit.
    rate_limit.limiter().reset(rate_limit.login_policy(), ip or "unknown")
    record_login_attempt(db, request, identifier, success=True, user_id=user.pk_user_id)
    record_event(
        db, ActivityEvent.LOGIN_SUCCESS, request=request, success=True,
        target_table="scd_user", target_row_id=user.pk_user_id,
    )
    db.commit()

    response = RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, session)
    return response


@router.get("/forgot-password")
def forgot_password_page(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "auth/forgot_password.html",
        {"submitted": False},
    )


@router.post("/forgot-password")
def forgot_password_submit(
    request: Request,
    identifier: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    # §2.4: password-reset requests are rate limited per IP, so the endpoint
    # cannot be used to enumerate accounts or to mail-bomb one.
    rate_limit.enforce(rate_limit.password_reset_policy(), client_ip(request) or "unknown")

    user = _find_user(db, normalize_username(identifier))
    if user and user.is_active_flag:
        token = tokens.issue(
            db,
            user,
            AuthTokenPurpose.PASSWORD_RESET,
            requested_ip=client_ip(request),
        )
        queue_template_email(
            db,
            EmailTemplateCode.FORGOT_PASSWORD,
            recipient=user.email,
            language=user.preferred_language,
            idempotency_key=f"password-reset:{user.pk_user_id}:{token[:12]}",
            params={
                "reset_url": f"{settings.app_base_url}/auth/reset-password?token={token}",
                "expiry_hours": int(
                    tokens.LIFETIMES[AuthTokenPurpose.PASSWORD_RESET].total_seconds() // 3600
                ),
            },
        )
    # One response whether or not the account exists, so this route cannot be
    # used to enumerate registered emails (Part I §2.7).
    db.commit()
    return templates.TemplateResponse(
        request,
        "auth/forgot_password.html",
        {"submitted": True},
    )


@router.get("/verify")
def verify_email(
    request: Request,
    token: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    token = clean_text(token or "", max_length=128) or ""

    user = tokens.consume(db, token, AuthTokenPurpose.EMAIL_VERIFICATION) if token else None
    if user is not None:
        if not user.email_verified_flag:
            user.email_verified_flag = True
            user.email_verified_dt = utcnow()
        db.commit()

    return templates.TemplateResponse(
        request,
        "auth/verify.html",
        {"verified": user is not None},
    )


@router.get("/reset-password")
def reset_password_page(
    request: Request, token: str | None = None, db: Session = Depends(get_db)
) -> Response:
    """Show the new-password form, if the link is still good.

    The token is checked but **not** consumed here: a customer who opens the
    link, then mistypes the confirmation, must still be able to try again.
    """
    token = clean_text(token or "", max_length=128) or ""
    valid = bool(token) and _reset_token_is_live(db, token)

    return templates.TemplateResponse(
        request,
        "auth/reset_password.html",
        {"token": token, "token_valid": valid, "error": None},
    )


@router.post("/reset-password")
def reset_password_submit(
    request: Request,
    token: str = Form(""),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Set the new password (Part I §2.6, §2.3).

    §2.4 rate-limits this alongside the request step: without it the endpoint
    is an oracle for guessing tokens.
    """
    rate_limit.enforce(rate_limit.password_reset_policy(), client_ip(request) or "unknown")

    token = clean_text(token, max_length=128) or ""
    user = tokens.consume(db, token, AuthTokenPurpose.PASSWORD_RESET) if token else None

    if user is None:
        # One message for expired, used, unknown and wrong-purpose alike:
        # distinguishing them tells an attacker which guesses were once real.
        return templates.TemplateResponse(
            request,
            "auth/reset_password.html",
            {"token": token, "token_valid": False, "error": None},
        )

    try:
        # No `keep_session_key`: whoever knew the old password may still be
        # signed in somewhere, and removing them is the point of a reset (§2.3).
        accounts.set_password(db, user, password, confirm_password)
    except ValidationFailed as failure:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "auth/reset_password.html",
            {"token": token, "token_valid": True, "error": failure.message},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # A reset proves control of the mailbox, which is the same thing
    # verification proves — so an unverified account becomes verified here
    # rather than stranding the customer one email short of checking out (§2.5).
    if not user.email_verified_flag:
        user.email_verified_flag = True
        user.email_verified_dt = utcnow()

    db.commit()
    log.info("password_reset_completed", extra={"user_id": user.pk_user_id})

    return RedirectResponse(
        "/auth/login?reset=1", status_code=status.HTTP_303_SEE_OTHER
    )


def _reset_token_is_live(db: Session, token: str) -> bool:
    """Look without redeeming."""
    import hashlib

    from app.models.identity import AuthToken

    row = db.scalars(
        select(AuthToken).where(
            AuthToken.token_hash == hashlib.sha256(token.encode("utf-8")).hexdigest(),
            AuthToken.purpose == AuthTokenPurpose.PASSWORD_RESET,
            AuthToken.scd_active_flag.is_(True),
        )
    ).first()
    return row is not None and row.is_live(utcnow())


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    ctx = get_context(request)
    if ctx.session_key:
        from app.models.activity import UserSession

        session = db.scalars(
            select(UserSession).where(UserSession.session_key == ctx.session_key)
        ).first()
        if session:
            end_session(db, session, SessionEndReason.LOGOUT)
        record_event(db, ActivityEvent.LOGOUT, request=request, context=ctx)
        db.commit()

    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@router.get("/register")
def register_page(request: Request, db: Session = Depends(get_db)) -> Response:
    if get_context(request).is_authenticated:
        return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "auth/register.html",
        {
            "form": {},
            "errors": {},
            "countries": _countries(db),
            "provinces": _provinces(db, _default_country_id(db)),
            "captcha_required": False,
        },
    )


@router.post("/register")
async def register_submit(request: Request, db: Session = Depends(get_db)) -> Response:
    """Validate everything, then write once.

    Deliberately not incremental: a half-written account cannot be rolled back
    by deleting it, because deletion is not a feature anywhere on this platform
    (Part I §2.5). So nothing is written until every field passes.
    """
    # §2.4: rate limiting applies to registration submissions too.
    rate_limit.enforce(rate_limit.register_policy(), client_ip(request) or "unknown")

    form = await _read_form(request)
    errors = _validate_registration(db, form)

    if errors:
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {
                "form": form,
                "errors": errors,
                "countries": _countries(db),
                "provinces": _provinces(db, form.get("country_id")),
                "captcha_required": False,
            },
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    user = _create_account(db, form)
    db.commit()

    log.info("account_registered", extra={"user_id": user.pk_user_id})

    # The verification email is queued, not sent inline: a slow SMTP server must
    # not hold the response, and the outbox row makes the send idempotent.
    _queue_verification_email(db, user)
    db.commit()

    return RedirectResponse(
        "/auth/login?registered=1", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/provinces")
def provinces_partial(
    request: Request, country_id: int, db: Session = Depends(get_db)
) -> Response:
    """HTMX partial: repopulate the province dropdown when the country changes."""
    return templates.TemplateResponse(
        request,
        "auth/_province_field.html",
        {"provinces": _provinces(db, country_id)},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: A real Argon2 hash of an unusable password, so the "no such user" path costs
#: the same as a genuine verification.
_DUMMY_HASH = hash_password("unused-placeholder-for-constant-time-login")


async def _read_form(request: Request) -> dict[str, str]:
    raw = await request.form()
    return {key: str(value) for key, value in raw.items()}


def _find_user(db: Session, identifier: str) -> User | None:
    """Accept either a username or an email; both are stored normalised."""
    return db.scalars(
        select(User)
        .where(
            (User.username == identifier) | (User.email == normalize_email(identifier)),
            User.scd_active_flag.is_(True),
        )
        .options(selectinload(User.role))
    ).first()


def _captcha_required(db: Session, request: Request) -> bool:
    if settings.captcha_provider == "none":
        return False
    failures = recent_failed_attempts(db, ip_address=client_ip(request), within_minutes=15)
    return failures >= settings.captcha_failed_attempts_threshold


def _safe_next(target: str) -> str:
    """Only ever redirect within this site — an open redirect is a phishing
    vector, and login pages are exactly where they get used."""
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def _countries(db: Session) -> list[Country]:
    return list(
        db.scalars(
            select(Country)
            .where(Country.scd_active_flag.is_(True))
            .order_by(Country.sort_order, Country.pk_country_id)
        ).all()
    )


def _default_country_id(db: Session) -> int | None:
    country = db.scalars(
        select(Country).where(Country.iso_code == "JO", Country.scd_active_flag.is_(True))
    ).first()
    return country.pk_country_id if country else None


def _provinces(db: Session, country_id: int | str | None) -> list[Province]:
    if not country_id:
        return []
    return list(
        db.scalars(
            select(Province)
            .where(
                Province.fk_country_id == int(country_id),
                Province.scd_active_flag.is_(True),
            )
            .order_by(Province.sort_order, Province.pk_province_id)
        ).all()
    )


def _validate_registration(db: Session, form: dict[str, str]) -> dict[str, str]:
    """Collect every problem, rather than failing on the first one."""
    errors: dict[str, str] = {}

    username = normalize_username(form.get("username", ""))
    if not is_valid_username(username):
        errors["username"] = (
            "Username must be 3–30 Latin characters, digits, dots or underscores."
        )
    elif db.scalars(
        select(User).where(User.username == username, User.scd_active_flag.is_(True))
    ).first():
        errors["username"] = "That username is already taken."

    email = normalize_email(form.get("email", ""))
    if "@" not in email or "." not in email.split("@")[-1]:
        errors["email"] = "Enter a valid email address."
    elif db.scalars(
        select(User).where(User.email == email, User.scd_active_flag.is_(True))
    ).first():
        errors["email"] = "An account already exists with that email address."

    problems = password_problems(form.get("password", ""), form.get("password_confirm"))
    if problems:
        errors["password"] = " ".join(problems)

    account_type = form.get("account_type", CustomerType.INDIVIDUAL)
    if account_type == CustomerType.INDIVIDUAL:
        if not clean_text(form.get("first_name")):
            errors["first_name"] = "First name is required."
        if not clean_text(form.get("last_name")):
            errors["last_name"] = "Last name is required."
        if not form.get("birth_date"):
            errors["birth_date"] = "Date of birth is required."
        if not normalize_phone(form.get("phone_number", "")):
            errors["phone_number"] = "Phone number is required."
    elif not clean_text(form.get("company_name")):
        errors["company_name"] = "Company name is required."

    for field, label in (
        ("country_id", "Country"),
        ("province_id", "Governorate"),
        ("city", "City"),
        ("address_line", "Address"),
    ):
        if not clean_text(form.get(field)):
            errors[field] = f"{label} is required."

    return errors


def _create_account(db: Session, form: dict[str, str]) -> User:
    now = utcnow()
    account_type = form.get("account_type", CustomerType.INDIVIDUAL)

    role = db.scalars(
        select(Role).where(
            Role.role_code == RoleCode.CUSTOMER, Role.scd_active_flag.is_(True)
        )
    ).first()

    newsletter_opt_in = form.get("newsletter_opt_in") == "1"

    user = User(
        fk_role_id=role.pk_role_id,
        username=normalize_username(form["username"]),
        email=normalize_email(form["email"]),
        password_hash=hash_password(form["password"]),
        password_changed_dt=now,
        email_verified_flag=False,
        is_active_flag=True,
        newsletter_opt_in_flag=newsletter_opt_in,
        scd_active_from=now,
    )
    db.add(user)
    db.flush()

    if account_type == CustomerType.COMPANY:
        db.add(
            CompanyProfile(
                fk_user_id=user.pk_user_id,
                company_name=clean_text(form["company_name"], max_length=200),
                scd_active_from=now,
            )
        )
    else:
        db.add(
            IndividualProfile(
                fk_user_id=user.pk_user_id,
                first_name=clean_text(form["first_name"], max_length=80),
                second_name=clean_text(form.get("second_name"), max_length=80),
                third_name=clean_text(form.get("third_name"), max_length=80),
                last_name=clean_text(form["last_name"], max_length=80),
                birth_date=dt.date.fromisoformat(form["birth_date"]),
                phone_country_code=normalize_country_code(
                    form.get("phone_country_code", "+962")
                ),
                phone_number=normalize_phone(form["phone_number"]),
                scd_active_from=now,
            )
        )

    db.add(
        Address(
            fk_user_id=user.pk_user_id,
            fk_country_id=int(form["country_id"]),
            fk_province_id=int(form["province_id"]),
            city=clean_text(form["city"], max_length=120),
            address_line=clean_text(form["address_line"]),
            is_default_flag=True,
            scd_active_from=now,
        )
    )

    if newsletter_opt_in:
        db.add(
            NewsletterSubscription(
                email=user.email,
                fk_user_id=user.pk_user_id,
                is_subscribed_flag=True,
                subscribed_dt=now,
                source="registration",
                scd_active_from=now,
            )
        )

    return user


def _queue_verification_email(
    db: Session, user: User, *, requested_ip: str | None = None
) -> None:
    """Mint a stored token and mail its link (Part I §2.5).

    The token is recorded — hashed — by ``services/tokens.py``, so the link can
    actually be redeemed and stops working once used or expired. Issuing also
    supersedes any earlier verification link for this account.
    """
    token = tokens.issue(
        db,
        user,
        AuthTokenPurpose.EMAIL_VERIFICATION,
        requested_ip=requested_ip,
    )
    queue_template_email(
        db,
        EmailTemplateCode.EMAIL_VERIFICATION,
        recipient=user.email,
        language=user.preferred_language,
        # Idempotency key: re-running registration for the same account can
        # never queue a second identical email (Part II §5). The token is part
        # of the key so a *deliberate* resend is not collapsed into the first.
        idempotency_key=f"verify:{user.pk_user_id}:{token[:12]}",
        params={
            "username": user.username,
            "verify_url": f"{settings.app_base_url}/auth/verify?token={token}",
        },
    )
