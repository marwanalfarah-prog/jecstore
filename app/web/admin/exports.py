"""Report export endpoints (Part I §2.2).

One route serves every report in every format, because §2.2 applies the
requirement uniformly: *"Every report and dashboard view must be exportable
(CSV, Excel, and PDF at minimum)."* Adding a report to
``services/report_datasets.py`` makes it exportable here with no further work.

Exporting is gated on ``reports.export`` — separate from ``reports.view``, so a
role can read a dashboard on screen without being able to walk out with the
whole dataset.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.context import get_context
from app.core.errors import ValidationFailed
from app.db.session import get_db
from app.models.identity import User
from app.services import exports, report_datasets
from app.services.permissions import GrantDecision
from app.web.admin.deps import current_staff, require_permission

router = APIRouter(prefix="/exports")


@router.get("/{report_key}.{fmt}")
def export_report(
    request: Request,
    report_key: str,
    fmt: str,
    staff: User = Depends(current_staff),
    _: GrantDecision = Depends(require_permission("reports", "export")),
    db: Session = Depends(get_db),
) -> Response:
    """Download a report.

    ``fmt`` is one of ``csv``, ``xlsx``, ``pdf`` or ``html`` (the print view).
    The dataset is built in the viewer's language, so an Arabic export has
    Arabic column headers rather than English ones over Arabic data.
    """
    if fmt not in {"csv", "xlsx", "pdf", "html"}:
        raise ValidationFailed("Unsupported export format.")

    dataset = report_datasets.build(
        db,
        report_key,
        language=get_context(request).language,
        start_date=_date(request.query_params.get("start")),
        end_date=_date(request.query_params.get("end")),
        category_code=request.query_params.get("category") or None,
    )
    return exports.export_response(request, dataset, fmt)


def _date(raw: str | None) -> dt.date | None:
    if not raw or not raw.strip():
        return None
    try:
        return dt.date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValidationFailed("That is not a valid date.") from exc
