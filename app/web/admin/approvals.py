"""The maker-checker queue (Part I §2.2.1).

Two views on the same data: what *I* can decide, and what *I* have submitted.
A maker never sees their own request in the first list — the engine excludes
them before any role matching, because §2.2.1 is explicit that a maker cannot
approve their own action even when their role would otherwise qualify.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.templating import templates
from app.db.session import get_db
from app.models.access import ApprovalDecision, ApprovalRequest, Permission
from app.models.identity import User
from app.services import approvals as approvals_service
from app.web.admin.context import admin_context
from app.web.admin.deps import current_staff

router = APIRouter(prefix="/approvals")


@router.get("")
def queue(
    request: Request,
    staff: User = Depends(current_staff),
    db: Session = Depends(get_db),
) -> Response:
    pending = approvals_service.pending_queue(db, staff)
    mine = approvals_service.my_requests(db, staff)

    return templates.TemplateResponse(
        request,
        "admin/approvals/queue.html",
        admin_context(
            db,
            staff,
            pending=[_decorate(db, a) for a in pending],
            mine=[_decorate(db, a) for a in mine],
        ),
    )


@router.get("/{request_id}")
def detail(
    request: Request,
    request_id: int,
    staff: User = Depends(current_staff),
    db: Session = Depends(get_db),
) -> Response:
    approval = _get(db, request_id)
    decisions = db.scalars(
        select(ApprovalDecision)
        .where(ApprovalDecision.fk_approval_request_id == request_id)
        .order_by(ApprovalDecision.created_dt)
    ).all()

    return templates.TemplateResponse(
        request,
        "admin/approvals/detail.html",
        admin_context(
            db,
            staff,
            approval=_decorate(db, approval),
            params=approvals_service.request_params(db, approval),
            decisions=decisions,
            can_decide=approvals_service.is_eligible_checker(db, approval, staff),
            is_mine=approval.fk_maker_user_id == staff.pk_user_id,
        ),
    )


@router.post("/{request_id}/approve")
def approve(
    request: Request,
    request_id: int,
    note: str | None = Form(None),
    staff: User = Depends(current_staff),
    db: Session = Depends(get_db),
) -> Response:
    approval = _get(db, request_id)
    approvals_service.approve(db, request, approval, staff, note=note)
    db.commit()
    return RedirectResponse(
        f"/admin/approvals/{request_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{request_id}/reject")
def reject(
    request: Request,
    request_id: int,
    note: str | None = Form(None),
    staff: User = Depends(current_staff),
    db: Session = Depends(get_db),
) -> Response:
    approval = _get(db, request_id)
    approvals_service.reject(db, request, approval, staff, note=note)
    db.commit()
    return RedirectResponse(
        f"/admin/approvals/{request_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{request_id}/cancel")
def cancel(
    request: Request,
    request_id: int,
    staff: User = Depends(current_staff),
    db: Session = Depends(get_db),
) -> Response:
    """A maker withdrawing their own request."""
    approval = _get(db, request_id)
    approvals_service.cancel(db, request, approval, staff)
    db.commit()
    return RedirectResponse("/admin/approvals", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------


def _get(db: Session, request_id: int) -> ApprovalRequest:
    approval = db.scalars(
        select(ApprovalRequest).where(
            ApprovalRequest.pk_approval_request_id == request_id,
            ApprovalRequest.scd_active_flag.is_(True),
        )
    ).first()
    if approval is None:
        raise NotFound("That approval request does not exist.")
    return approval


def _decorate(db: Session, approval: ApprovalRequest) -> dict:
    """Attach the permission and maker names the queue needs to be readable."""
    permission = db.get(Permission, approval.fk_permission_id)
    maker = db.get(User, approval.fk_maker_user_id)
    return {
        "row": approval,
        "permission": permission,
        "maker": maker,
    }
