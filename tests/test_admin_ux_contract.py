"""The admin panel's cross-screen promises, checked as a contract.

§17.6 asks for "consistent use of tables, filters, status badges, and the same
loading/success/error state handling required in Part II across every admin
screen, so staff aren't relearning UI patterns module to module". That is not a
promise a per-screen test can keep — every one of these failure modes was a
screen that looked fine on its own and disagreed with its neighbours:

* twenty copies of the flash banner, three of which had no Maker-Checker case
  and so told a maker their parked action was "Saved";
* filter forms that dropped any query parameter they had no control for, which
  is what made the dashboard's tiles link to lists they then silently widened;
* destructive forms with no confirmation, in a panel that is append-only and
  therefore has no undo to fall back on;
* module screens with no export, when §2.2 asks for every report to be
  exportable and not only from /admin/reports.

So each test here reads all the templates at once and asserts the property
holds across them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.config import settings

ADMIN = settings.templates_dir / "admin"

#: Print-only pages served outside the admin chrome, and the chrome itself.
STANDALONE = {"base.html", "exports/report.html", "inventory/labels.html"}


def _screens() -> list[Path]:
    return [
        path
        for path in sorted(ADMIN.rglob("*.html"))
        if "partials" not in path.parts
        and path.relative_to(ADMIN).as_posix() not in STANDALONE
    ]


def _name(path: Path) -> str:
    return path.relative_to(ADMIN).as_posix()


# ---------------------------------------------------------------------------
# One flash banner
# ---------------------------------------------------------------------------


def test_no_screen_rolls_its_own_flash_banner():
    """The banner lives in one partial. A screen that hand-rolls it drifts —
    that is how "saved" ended up worded one way on Products and another on
    Money Boxes, and how three screens forgot Maker-Checker entirely."""
    offenders = [
        _name(path)
        for path in _screens()
        if re.search(r'\{%-? if flash ==', path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"screens with an inline flash block: {offenders}"


def test_every_screen_that_is_told_about_a_flash_renders_one():
    """A route passing `flash=` into a template that never shows it means the
    action silently appears to have done nothing."""
    import ast

    web = Path(settings.templates_dir).parent / "web" / "admin"
    told: dict[str, str] = {}

    for path in sorted(web.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # templates.TemplateResponse(request, "<name>", admin_context(...))
            if getattr(node.func, "attr", "") != "TemplateResponse":
                continue
            template = next(
                (
                    a.value
                    for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                ),
                None,
            )
            if not template or not template.startswith("admin/"):
                continue
            source = ast.unparse(node)
            if "flash=" in source:
                told[template] = path.name

    missing = [
        template
        for template in sorted(told)
        if "flash.html" not in (settings.templates_dir / template).read_text(
            encoding="utf-8"
        )
    ]
    assert not missing, f"routes pass a flash these templates never render: {missing}"


# ---------------------------------------------------------------------------
# Filters that keep their state
# ---------------------------------------------------------------------------


def test_every_filter_form_preserves_the_parameters_it_does_not_own():
    """A GET form submits only its own fields, so every parameter without a
    control is dropped on submit. The dashboard's "awaiting a shipping quote"
    tile linked to /admin/orders?shipping=pending, the filter bar had no
    `shipping` control, and one press of "Filter" widened the list back to
    every order — with the narrower tile count still on screen."""
    offenders = []
    for path in _screens():
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r'<form[^>]*method="get"[^>]*class="[^"]*admin-filters', source
        ):
            end = source.find("</form>", match.start())
            body = source[match.start() : end]
            if "filter_state.html" not in body:
                offenders.append(_name(path))
                break
    assert not offenders, (
        "filter bars that drop unowned query parameters on submit: " f"{offenders}"
    )


# ---------------------------------------------------------------------------
# Confirmation before something that cannot be undone
# ---------------------------------------------------------------------------

#: Paths whose POST cannot be reversed from the panel. The panel is
#: append-only — closing a box or retiring a code writes history rather than
#: deleting a row — so there is no undo behind any of these.
IRREVERSIBLE = re.compile(
    r'action="[^"]*/(close|remove|retire|deactivate|terminate|revoke|cancel|reject'
    r"|write-off|loss|deliver)(/[^\"]*)?\"",
)

#: Reversible look-alikes: `/cancel` on the approvals queue withdraws your own
#: pending request, which the maker can simply submit again.
EXEMPT = {"approvals/queue.html"}


def test_every_irreversible_form_asks_first():
    offenders = []
    for path in _screens():
        if _name(path) in EXEMPT:
            continue
        source = path.read_text(encoding="utf-8")
        for match in IRREVERSIBLE.finditer(source):
            start = source.rfind("<form", 0, match.start())
            end = source.find(">", match.end())
            if start == -1 or end == -1:
                continue
            tag = source[start:end]
            if 'method="post"' not in tag:
                continue
            # A button inside the form may carry the question instead.
            close = source.find("</form>", end)
            body = source[start : close if close != -1 else end]
            if "data-confirm" not in body:
                offenders.append(f"{_name(path)}: {match.group(0)}")
    assert not offenders, f"irreversible actions with no confirmation: {offenders}"


# ---------------------------------------------------------------------------
# Exports where staff work (Part I §2.2)
# ---------------------------------------------------------------------------

#: The module screens §2.2 names, and the report each one exports.
EXPORTABLE_SCREENS = {
    "orders/list.html": "sales",
    "products/list.html": "inventory",
    "inventory/list.html": "inventory",
    "consignment/list.html": "consignment",
    "money_boxes/list.html": "money_boxes",
    "promocodes/list.html": "promocodes",
    "returns/list.html": "returns",
}


@pytest.mark.parametrize("screen,report", sorted(EXPORTABLE_SCREENS.items()))
def test_module_screens_can_export_what_they_show(screen: str, report: str):
    """§2.2: "every report and dashboard view must be exportable". The export
    layer had always been able to; the panel offered the buttons on
    /admin/reports alone, so exporting the list already on screen meant leaving
    it, finding the report in a directory, and setting a period."""
    source = (ADMIN / screen).read_text(encoding="utf-8")
    assert "export_buttons.html" in source, f"{screen} offers no export"
    assert f'report = "{report}"' in source, f"{screen} exports the wrong report"


def test_the_export_partial_is_gated_on_the_export_permission():
    """§2.2 makes `reports.export` a permission of its own, separate from
    `reports.view`. The partial is now included from seven screens, so the gate
    belongs in the partial rather than at each call site."""
    source = (ADMIN / "partials" / "export_buttons.html").read_text(encoding="utf-8")
    assert 'can.get("reports.export")' in source


def test_every_report_key_is_reachable_from_the_reports_screen():
    """Five of the seven built reports were reachable from no screen at all —
    the export routes served them, and nothing linked to them."""
    from app.services.report_datasets import REPORT_KEYS
    from app.web.admin.reports import _directory

    assert {entry["key"] for entry in _directory()} == set(REPORT_KEYS)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def test_every_admin_screen_is_reachable_from_the_navigation():
    """Review moderation, stock takes, transfers, barcode labels and the tag
    screen were all built, routed, and linked from nowhere in the sidebar."""
    nav = (ADMIN / "partials" / "nav.html").read_text(encoding="utf-8")

    must_appear = [
        "/admin/orders",
        "/admin/returns",
        "/admin/products",
        "/admin/products/tags",
        "/admin/products/reviews",
        "/admin/categories",
        "/admin/promocodes",
        "/admin/inventory",
        "/admin/inventory/shipments",
        "/admin/inventory/transfers",
        "/admin/inventory/stock-takes",
        "/admin/inventory/labels",
        "/admin/consignment",
        "/admin/money-boxes",
        "/admin/reports",
        "/admin/users",
        "/admin/access",
        "/admin/sessions",
        "/admin/audit",
        "/admin/content",
        "/admin/branches",
    ]
    missing = [href for href in must_appear if f'"{href}"' not in nav]
    assert not missing, f"screens with no nav entry: {missing}"


def test_every_nav_item_gates_on_a_permission_that_exists():
    """A nav item gated on a permission no registry entry defines is hidden
    from everyone, forever, with no error anywhere."""
    from app.services.permissions import PERMISSION_REGISTRY
    from app.web.admin.context import NAV_PERMISSIONS

    nav = (ADMIN / "partials" / "nav.html").read_text(encoding="utf-8")
    used = set(re.findall(r'"([a-z_]+\.[a-z_]+)"\)?\s*\)?\s*\}\}', nav))
    used |= set(re.findall(r'item\([^)]*"([a-z_]+\.[a-z_]+)"', nav))

    registered = {f"{spec.module}.{spec.action}" for spec in PERMISSION_REGISTRY}
    gates = {code for code in used if code in registered or "." in code}
    gates &= set(NAV_PERMISSIONS)

    unknown = sorted(gates - registered)
    assert not unknown, f"nav gates on unregistered permissions: {unknown}"

    # And every permission the context resolves must be a real one, or the
    # batch lookup silently returns False for a typo.
    unresolvable = sorted(set(NAV_PERMISSIONS) - registered)
    assert not unresolvable, f"NAV_PERMISSIONS names unknown codes: {unresolvable}"


# ---------------------------------------------------------------------------
# Behaviour that only works if the script is actually loaded
# ---------------------------------------------------------------------------


def test_the_admin_shell_loads_the_behaviour_script():
    """`data-confirm`, submit-once and `data-autosubmit` are all inert without
    it, and all three fail silently — the form simply submits as it used to."""
    shell = (ADMIN / "base.html").read_text(encoding="utf-8")
    assert "js/admin.js" in shell


def test_no_screen_uses_an_inline_event_handler_for_form_behaviour():
    """Auto-submitting selects were inline `onchange` attributes on some
    screens and absent on others. One attribute, handled in one place."""
    offenders = [
        _name(path)
        for path in _screens()
        if re.search(r'\bonchange="', path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"inline onchange handlers: {offenders}"
