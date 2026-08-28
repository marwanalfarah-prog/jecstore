"""Every admin action the web layer submits must be a real, grantable action.

The bug this pins: `/admin/money-boxes/{id}/close` submitted itself under the
`money_boxes.create_box` action, so closing a box was gated by the *create*
permission and landed in the audit log as a *create* — the §2.2 audit trail
recorded the wrong verb, and `/admin/audit`'s action filter could never surface
a close. Nothing failed loudly, because `create_box` is a perfectly valid
action; it was simply the wrong one.

So the guard here is structural rather than behavioural: read the
`execute_or_submit(...)` call sites out of the source and require that each
one names an action that exists in the permission registry, has a replay
handler, and is the same action the route gates itself on.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from app.services import approvals, money, money_actions
from app.services.permissions import PERMISSION_REGISTRY
from app.web.admin import money_boxes as money_boxes_web

# Importing the action modules is what populates the approvals registry.
import app.services.access_actions  # noqa: F401
import app.services.catalog_actions  # noqa: F401
import app.services.consignment_actions  # noqa: F401
import app.services.inventory_actions  # noqa: F401
import app.services.order_actions  # noqa: F401
import app.services.promocode_actions  # noqa: F401
import app.services.return_actions  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"

GRANTABLE = {f"{spec.module}.{spec.action}" for spec in PERMISSION_REGISTRY}


def _submitted_actions() -> list[tuple[str, str, str, int]]:
    """Every ``execute_or_submit(module=..., action=...)`` in the web layer.

    Returns ``(file, module, action, lineno)``. Keyword arguments only — the
    call site always spells them out, and a positional variant should fail
    review rather than be silently accepted here.
    """
    found: list[tuple[str, str, str, int]] = []
    for path in sorted(WEB.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", "") != "execute_or_submit":
                continue
            kwargs = {
                kw.arg: kw.value.value
                for kw in node.keywords
                if isinstance(kw.value, ast.Constant)
            }
            module, action = kwargs.get("module"), kwargs.get("action")
            if module and action:
                found.append(
                    (str(path.relative_to(ROOT)), module, action, node.lineno)
                )
    return found


def test_the_scan_actually_finds_call_sites() -> None:
    """Guard the guard: an AST scan that silently matches nothing proves nothing."""
    assert len(_submitted_actions()) > 10


@pytest.mark.parametrize(
    "where,module,action",
    [(f"{f}:{line}", m, a) for f, m, a, line in _submitted_actions()],
)
def test_submitted_action_is_grantable(where: str, module: str, action: str) -> None:
    assert f"{module}.{action}" in GRANTABLE, (
        f"{where} submits '{module}.{action}', which is not in PERMISSION_REGISTRY. "
        f"An action Admin cannot see on /admin/access cannot be granted, scoped "
        f"to Maker-Checker, or filtered in the audit log (§2.2)."
    )


@pytest.mark.parametrize(
    "where,module,action",
    [(f"{f}:{line}", m, a) for f, m, a, line in _submitted_actions()],
)
def test_submitted_action_can_be_replayed(where: str, module: str, action: str) -> None:
    assert f"{module}.{action}" in approvals.registered_actions(), (
        f"{where} submits '{module}.{action}' with no @approvals.register handler, "
        f"so an approved Maker-Checker request could never execute."
    )


def _permission_gates(source: str) -> dict[str, set[str]]:
    """Map each route function to the ``require_permission`` codes it declares."""
    gates: dict[str, set[str]] = {}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.get_source_segment(source, node) or ""
        gates[node.name] = {
            f"{m.group(1)}.{m.group(2)}"
            for m in re.finditer(
                r'require_permission\(\s*"([^"]+)"\s*,\s*"([^"]+)"', body
            )
        }
    return gates


@pytest.mark.parametrize("path", sorted((WEB / "admin").rglob("*.py")))
def test_route_gates_on_the_action_it_submits(path: pathlib.Path) -> None:
    """A route must not be gated on one permission and audited as another.

    Gating on A while submitting B means the audit log names an action the
    checker never actually had to hold — which is exactly how the close-box
    bug hid.
    """
    source = path.read_text(encoding="utf-8")
    gates = _permission_gates(source)
    tree = ast.parse(source)

    mismatches = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        submitted = {
            f"{kw['module']}.{kw['action']}"
            for kw in (
                {
                    k.arg: k.value.value
                    for k in call.keywords
                    if isinstance(k.value, ast.Constant)
                }
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and getattr(call.func, "attr", "") == "execute_or_submit"
            )
            if kw.get("module") and kw.get("action")
        }
        gated = gates.get(node.name, set())
        # A route may legitimately gate on `view` and submit a write action, so
        # only flag a submitted action that is gated on *nothing* it declares.
        for code in submitted:
            if gated and code not in gated:
                mismatches.append((node.name, code, sorted(gated)))

    assert not mismatches, (
        f"{path.relative_to(ROOT)}: route submits an action it does not gate on: "
        + "; ".join(
            f"{fn}() submits '{code}' but requires {gated}"
            for fn, code, gated in mismatches
        )
    )


def test_closing_a_money_box_is_its_own_action() -> None:
    """The specific regression: close is not create."""
    assert "money_boxes.close_box" in GRANTABLE
    assert "money_boxes.close_box" in approvals.registered_actions()

    source = pathlib.Path(money_boxes_web.__file__).read_text(encoding="utf-8")
    close = re.search(
        r"def close_box\(.*?(?=\n@router|\ndef )", source, flags=re.DOTALL
    )
    assert close is not None
    assert 'action="close_box"' in close.group(0)
    assert 'require_permission("money_boxes", "close_box")' in close.group(0)


def test_close_box_action_replays_and_closes_the_box(db) -> None:
    """The registered handler does the thing its name promises."""
    from decimal import Decimal

    box = money.create_box(
        db,
        box_code="to_close",
        name_ar="صندوق للإغلاق",
        name_en="Box to close",
        opening_balance_amt=Decimal("0"),
    )
    db.commit()
    assert box.is_open_flag is True

    money_actions.close_box(db, {"money_box_id": box.pk_money_box_id}, None)
    db.commit()

    assert box.is_open_flag is False


def test_legacy_close_requests_still_replay(db) -> None:
    """A request queued before the split carried operation='close_box'.

    Those rows are immutable history (§2.2), so the old entry point has to keep
    honouring them rather than replaying as a create.
    """
    from decimal import Decimal

    box = money.create_box(
        db,
        box_code="legacy_close",
        name_ar="صندوق قديم",
        name_en="Legacy box",
        opening_balance_amt=Decimal("0"),
    )
    db.commit()

    money_actions.create_or_close_box(
        db, {"operation": "close_box", "money_box_id": box.pk_money_box_id}, None
    )
    db.commit()

    assert box.is_open_flag is False


pytest_plugins = ()

from tests.test_checkout import db, store  # noqa: E402,F401 - fixtures
