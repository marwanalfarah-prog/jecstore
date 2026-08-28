"""Every admin screen renders, in both languages, for someone who may see it.

The gap this closes is the one that let a broken link ship: the dashboard's
pending-reviews tile pointed at ``/admin/reviews``, a path no router has ever
served — the real screen is ``/admin/products/reviews``. Nothing failed,
because nothing rendered the dashboard and then followed its own links. Four
more screens (stock takes, transfers, barcode labels, review moderation) were
built, routed and reachable from no navigation at all.

So this module does the dull thing properly:

* signs in as an Admin holding every permission in the registry,
* GETs every admin route the panel exposes, in Arabic and in English,
* follows every internal ``href`` the rendered pages emit and requires it to
  resolve.

A template that raises, a context key a route forgot to pass, and a link to a
path nobody registered all fail here rather than in a branch.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.i18n import translate
from app.db.base import utcnow
from app.models.access import Permission, PermissionGrant
from app.models.enums import ApprovalMode, GrantScope, RoleCode
from app.models.identity import Role, User
from app.services import sessions
from app.services.permissions import PERMISSION_REGISTRY
from tests.test_checkout import _FakeRequest, db, store  # noqa: F401 - fixtures
from tests.test_order_management import shop  # noqa: F401 - fixture

#: Every GET screen in the panel. Detail pages are covered by the link crawl
#: below, which reaches them with ids that actually exist.
ADMIN_SCREENS = [
    "/admin/",
    "/admin/approvals",
    "/admin/access",
    "/admin/products",
    "/admin/products/new",
    "/admin/products/reviews",
    "/admin/products/tags",
    "/admin/products/attributes",
    "/admin/categories",
    "/admin/promocodes",
    "/admin/orders",
    "/admin/returns",
    "/admin/returns/new",
    "/admin/inventory",
    "/admin/inventory/shipments",
    "/admin/inventory/shipments/new",
    "/admin/inventory/stock-takes",
    "/admin/inventory/transfers",
    "/admin/inventory/labels",
    "/admin/consignment",
    "/admin/consignment/new",
    "/admin/money-boxes",
    "/admin/money-boxes/new",
    "/admin/reports",
    "/admin/users",
    "/admin/sessions",
    "/admin/audit",
    "/admin/content",
    "/admin/content/announcements",
    "/admin/content/settings",
    "/admin/content/emails",
    "/admin/content/shipping",
    "/admin/branches",
    "/admin/search",
]


@pytest.fixture
def admin(db: Session) -> User:
    """An Admin granted every registered permission, on Single Approval.

    Single rather than Maker-Checker throughout: this module is about whether
    the screens render, and a parked action would test the approval engine
    instead. :mod:`tests.test_approvals` covers that.
    """
    now = utcnow()
    role = Role(
        role_code=RoleCode.ADMIN,
        name_ar="مدير",
        name_en="Admin",
        is_staff_flag=True,
        scd_active_from=now,
    )
    db.add(role)
    db.flush()

    user = User(
        fk_role_id=role.pk_role_id,
        username="smoke_admin",
        email="smoke_admin@example.com",
        password_hash="x",
        email_verified_flag=True,
        is_active_flag=True,
        scd_active_from=now,
    )
    db.add(user)
    db.flush()

    for spec in PERMISSION_REGISTRY:
        permission = Permission(
            module_code=spec.module,
            action_code=spec.action,
            name_ar=spec.name_ar,
            name_en=spec.name_en,
            default_approval_mode=ApprovalMode.SINGLE,
            scd_active_from=now,
        )
        db.add(permission)
        db.flush()
        db.add(
            PermissionGrant(
                fk_permission_id=permission.pk_permission_id,
                grant_scope=GrantScope.ROLE,
                fk_role_id=role.pk_role_id,
                granted_flag=True,
                approval_mode=ApprovalMode.SINGLE,
                required_approvals=1,
                scd_active_from=now,
            )
        )
    db.commit()
    return user


@pytest.fixture
def client(db: Session, admin: User, monkeypatch) -> TestClient:
    """A signed-in staff client whose app talks to the fixture database."""
    import app.db.session as db_session
    import app.services.activity as activity
    import app.web.middleware as middleware
    from app.main import app

    maker = sessionmaker(
        bind=db.get_bind(), autoflush=False, expire_on_commit=False, class_=Session
    )
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    monkeypatch.setattr(middleware, "SessionLocal", maker)
    monkeypatch.setattr(activity, "record_page_view", lambda *a, **k: None)

    session = sessions.create_session(db, admin, _FakeRequest())
    db.commit()

    test_client = TestClient(app)
    test_client.cookies.set(settings.session_cookie_name, session.session_key)
    return test_client


# ---------------------------------------------------------------------------
# The screens themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ADMIN_SCREENS)
@pytest.mark.parametrize("language", ["en", "ar"])
def test_every_admin_screen_renders(client: TestClient, path: str, language: str):
    response = client.get(path, params={"lang": language})
    assert response.status_code == 200, f"{path} [{language}] → {response.status_code}"
    assert "<html" in response.text


@pytest.mark.parametrize("path", ADMIN_SCREENS)
def test_no_screen_leaves_a_translation_key_on_the_page(client: TestClient, path: str):
    """``t()`` returns the key itself when it is missing from both catalogues,
    so an untranslated label ships as ``admin.something`` in the markup."""
    body = client.get(path).text
    stray = set(re.findall(r">\s*([a-z_]+\.[a-z_]{3,})\s*<", body))
    # `module.action` codes are legitimate page *content* on these two.
    if path in ("/admin/audit", "/admin/access"):
        return
    assert not stray, f"{path} rendered raw translation keys: {sorted(stray)}"


# ---------------------------------------------------------------------------
# The links those screens emit
# ---------------------------------------------------------------------------

_HREF = re.compile(r'href="(/admin[^"#?]*)(\?[^"#]*)?"')


def test_every_internal_admin_link_resolves(client: TestClient):
    """Crawl one hop out of every screen and require each target to answer.

    One hop is enough: every admin path is linked from a screen in the list
    above, so a dead link anywhere in the panel is a dead link one hop from
    somewhere here.
    """
    targets: dict[str, str] = {}
    for path in ADMIN_SCREENS:
        for href, query in _HREF.findall(client.get(path).text):
            targets.setdefault(href + (query or ""), path)

    broken = {}
    for target, source in sorted(targets.items()):
        status = client.get(target).status_code
        if status >= 400:
            broken[target] = f"{status}, linked from {source}"

    assert not broken, f"admin links that do not resolve: {broken}"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_every_admin_template_compiles():
    """Jinja parses lazily, so a syntax error in a template nobody rendered in
    this run ships silently. Compile all of them up front instead."""
    from app.core.templating import templates

    env = templates.env
    broken = {}
    for path in sorted((settings.templates_dir / "admin").rglob("*.html")):
        name = path.relative_to(settings.templates_dir).as_posix()
        try:
            env.get_template(name)
        except Exception as error:  # noqa: BLE001 - reported, not handled
            broken[name] = f"{type(error).__name__}: {error}"

    assert not broken, f"templates that do not compile: {broken}"


# ---------------------------------------------------------------------------
# Detail screens, which need a row to be about
# ---------------------------------------------------------------------------
#
# The list screens above render against an empty database. The detail screens
# cannot, and they hold most of the markup — the order screen alone is four
# hundred lines. So this pair builds one real order and renders its screens.


@pytest.fixture
def placed_order(db: Session, shop: dict):
    from tests.test_order_management import _place

    order = _place(db, shop)
    db.commit()
    return order


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_order_detail_screen_renders(
    client: TestClient, placed_order, language: str
):
    response = client.get(
        f"/admin/orders/{placed_order.pk_order_id}", params={"lang": language}
    )
    assert response.status_code == 200
    assert placed_order.order_number in response.text


def test_the_order_detail_screen_names_the_preparer_not_their_id(
    client: TestClient, db: Session, placed_order, admin: User
):
    """It used to print `prepared_by_user_id` straight, so the page read
    "Prepared by 4" and staff had no way to turn 4 into a colleague."""
    from app.services import orders as order_service

    order_service.mark_prepared(db, placed_order, admin)
    db.commit()

    # Pinned to English: the panel defaults to Arabic, so an unpinned
    # request renders labels this assertion would not recognise.
    body = client.get(
        f"/admin/orders/{placed_order.pk_order_id}", params={"lang": "en"}
    ).text

    # Scoped to the field itself: a bare "is the id absent?" check trips over
    # the sidebar's work-count badges, which legitimately render small numbers.
    label = translate("admin.prepared_by", "en")
    field = re.search(
        rf"<dt>{re.escape(label)}</dt>\s*<dd>(.*?)</dd>", body, re.S
    )
    assert field, "the Prepared by field is missing from the page"
    assert admin.username in field.group(1)
    assert str(admin.pk_user_id) not in field.group(1)


def test_the_order_detail_screen_offers_a_staff_return(
    client: TestClient, placed_order
):
    """§12 lets staff raise a return at the counter. Nothing linked to that
    screen, so the only way in was to type the URL with an order id by hand."""
    body = client.get(f"/admin/orders/{placed_order.pk_order_id}").text
    assert f"/admin/returns/new?order_id={placed_order.pk_order_id}" in body


def test_the_staff_return_screen_asks_which_order_when_none_is_given(
    client: TestClient, placed_order
):
    """It required `order_id` as a query parameter, so reaching it from the
    Returns list — or following its own language toggle — answered 422."""
    response = client.get("/admin/returns/new")
    assert response.status_code == 200
    assert placed_order.order_number in response.text
