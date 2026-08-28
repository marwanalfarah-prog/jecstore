"""The permission registry and the access checks built on it (Part I §2.2).

Permissions are **per module and action**, not per page — that is what allows an
Employee to be given order-prep access without money-box access. The registry
below is the single source of truth for what actions exist; ``seed.py`` mirrors
it into ``LKP_PERMISSION`` so Admin has a real list to tick.

Resolution order when deciding whether someone may act:

1. A grant scoped to their **username** wins outright — including a
   ``granted_flag = false`` row, which is how one person is carved out of a
   permission their role otherwise has.
2. Otherwise, a grant scoped to their **role** applies.
3. Otherwise, denied.

The approval mode rides on whichever grant won, so the same action can be
Single Approval for one role and Maker-Checker for another (Part I §2.2.1).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import LastAdminLockout, PermissionDenied
from app.models.access import Permission, PermissionGrant
from app.models.enums import ApprovalMode, GrantScope, RoleCode
from app.models.identity import Role, User


@dataclass(frozen=True, slots=True)
class PermissionSpec:
    module: str
    action: str
    name_ar: str
    name_en: str
    #: Sensitive by default: unless a grant says otherwise, this action is
    #: parked for a second pair of eyes.
    default_mode: str = ApprovalMode.SINGLE


def _p(module: str, action: str, ar: str, en: str, mode: str = ApprovalMode.SINGLE):
    return PermissionSpec(module, action, ar, en, mode)


MC = ApprovalMode.MAKER_CHECKER

#: Every grantable action in the system, grouped by the modules named in
#: Part I §2.2. Adding a capability means adding a line here.
PERMISSION_REGISTRY: tuple[PermissionSpec, ...] = (
    # --- Access management (the meta-permission) ---------------------------
    _p("access", "view", "عرض إدارة الصلاحيات", "View access management"),
    _p("access", "grant_permission", "منح الصلاحيات", "Grant permissions", MC),
    _p("access", "revoke_permission", "سحب الصلاحيات", "Revoke permissions", MC),
    _p("access", "set_approval_mode", "تحديد نمط الموافقة", "Set approval mode", MC),
    _p("access", "login_as", "الدخول كمستخدم آخر", "Log in as another user", MC),

    # --- Catalog -----------------------------------------------------------
    _p("catalog", "view", "عرض الكتالوج", "View catalog"),
    _p("catalog", "create_product", "إضافة منتج", "Create product"),
    _p("catalog", "edit_product", "تعديل منتج", "Edit product"),
    _p("catalog", "change_price", "تعديل السعر", "Change price", MC),
    _p("catalog", "apply_discount", "تطبيق خصم", "Apply discount", MC),
    _p("catalog", "create_category", "إضافة قسم", "Create category"),
    _p("catalog", "delete_category", "حذف قسم", "Delete category", MC),
    _p("catalog", "manage_publishers", "إدارة دور النشر", "Manage publishers"),
    _p("catalog", "manage_tags", "إدارة الوسوم", "Manage tags"),
    _p("catalog", "moderate_reviews", "مراجعة التقييمات", "Moderate reviews"),

    # --- Orders ------------------------------------------------------------
    _p("orders", "view", "عرض الطلبات", "View orders"),
    _p("orders", "prepare", "تجهيز الطلب", "Prepare order"),
    _p("orders", "set_line_status", "تحديث حالة التسليم", "Set line fulfilment status"),
    _p("orders", "edit_items", "تعديل أصناف الطلب", "Edit order items"),
    _p("orders", "apply_item_discount", "خصم على صنف", "Apply item discount", MC),
    _p("orders", "apply_invoice_discount", "خصم على الفاتورة", "Apply invoice discount", MC),
    _p("orders", "record_payment", "تسجيل دفعة", "Record payment"),
    _p("orders", "change_shipping_cost", "تعديل كلفة الشحن", "Change shipping cost"),
    _p("orders", "cancel", "إلغاء الطلب", "Cancel order", MC),
    _p("orders", "modify_placed_order", "تعديل طلب مثبت", "Modify a placed order", MC),

    # --- Returns -----------------------------------------------------------
    _p("returns", "view", "عرض المرتجعات", "View returns"),
    _p("returns", "inspect", "فحص المرتجع", "Inspect a return"),
    _p("returns", "issue_refund", "إصدار استرداد", "Issue refund", MC),
    _p("returns", "issue_store_credit", "إصدار رصيد", "Issue store credit", MC),

    # --- Money boxes -------------------------------------------------------
    _p("money_boxes", "view", "عرض الصناديق", "View money boxes"),
    _p("money_boxes", "create_box", "إنشاء صندوق", "Create money box", MC),
    _p("money_boxes", "close_box", "إغلاق صندوق", "Close money box", MC),
    _p("money_boxes", "create_transaction", "تسجيل حركة مالية", "Create money transaction", MC),
    _p("money_boxes", "reconcile", "جرد الصندوق", "Reconcile money box", MC),

    # --- Inventory ---------------------------------------------------------
    _p("inventory", "view", "عرض المخزون", "View inventory"),
    _p("inventory", "receive_shipment", "استلام شحنة", "Receive shipment"),
    _p("inventory", "adjust_stock", "تعديل المخزون", "Adjust stock", MC),
    _p("inventory", "write_off", "شطب تالف أو مفقود", "Write off damaged or lost stock", MC),
    _p("inventory", "transfer_between_branches", "نقل بين الفروع", "Transfer between branches"),
    _p("inventory", "stock_take", "الجرد الفعلي", "Run a stock take"),
    _p("inventory", "print_barcodes", "طباعة الباركود", "Print barcode labels"),

    # --- Consignment -------------------------------------------------------
    _p("consignment", "view", "عرض الأمانات", "View consignment"),
    _p("consignment", "create_arrangement", "إنشاء اتفاقية أمانة", "Create arrangement"),
    _p("consignment", "record_return", "تسجيل إرجاع أمانة", "Record consignment return"),
    _p("consignment", "settle", "تسوية الأمانة", "Settle consignment", MC),

    # --- Users -------------------------------------------------------------
    _p("users", "view", "عرض المستخدمين", "View users"),
    _p("users", "create_staff", "إنشاء حساب موظف", "Create staff account", MC),
    _p("users", "deactivate", "تعطيل حساب", "Deactivate account", MC),
    _p("users", "view_sessions", "عرض الجلسات النشطة", "View active sessions"),
    _p("users", "terminate_session", "إنهاء جلسة", "Terminate a session"),

    # --- Reports -----------------------------------------------------------
    _p("reports", "view", "عرض التقارير", "View reports"),
    _p("reports", "export", "تصدير التقارير", "Export reports"),
    _p("reports", "view_financials", "عرض القوائم المالية", "View financial statements"),
    _p("reports", "view_costs", "عرض الكلف", "View cost data"),

    # --- Newsletter --------------------------------------------------------
    _p("newsletter", "view", "عرض المشتركين", "View subscribers"),
    _p("newsletter", "send_campaign", "إرسال نشرة", "Send a newsletter", MC),

    # --- Shipping ----------------------------------------------------------
    _p("shipping", "view", "عرض قواعد الشحن", "View shipping rules"),
    _p("shipping", "manage_rules", "إدارة قواعد الشحن", "Manage shipping rules"),

    # --- Content -----------------------------------------------------------
    _p("content", "manage_homepage", "إدارة الصفحة الرئيسية", "Manage homepage"),
    _p("content", "manage_announcements", "إدارة الإعلانات", "Manage announcements"),
    _p("content", "manage_footer", "إدارة التذييل", "Manage footer"),
    _p("content", "manage_email_templates", "إدارة قوالب البريد", "Manage email templates"),
    _p("content", "manage_branches", "إدارة الفروع", "Manage branches"),
    _p("content", "manage_promocodes", "إدارة رموز الخصم", "Manage promocodes"),

    # --- Settings ----------------------------------------------------------
    _p("settings", "view", "عرض الإعدادات", "View settings"),
    _p("settings", "set_exchange_rate", "تعديل سعر الصرف", "Set exchange rate", MC),
    _p("settings", "set_session_timeout", "تعديل مهلة الجلسة", "Set session timeout"),
    _p("settings", "view_audit_log", "عرض سجل التدقيق", "View audit log"),
)


def permission_code(module: str, action: str) -> str:
    return f"{module}.{action}"


@dataclass(frozen=True, slots=True)
class GrantDecision:
    """The outcome of an access check, and how the action must be executed."""

    allowed: bool
    approval_mode: str = ApprovalMode.SINGLE
    grant_id: int | None = None
    required_approvals: int = 1

    @property
    def needs_approval(self) -> bool:
        return self.allowed and self.approval_mode == ApprovalMode.MAKER_CHECKER


def resolve_grant(db: Session, user: User, module: str, action: str) -> GrantDecision:
    """Decide whether ``user`` may perform ``module.action``, and how."""
    permission = db.scalars(
        select(Permission).where(
            Permission.module_code == module,
            Permission.action_code == action,
            Permission.scd_active_flag.is_(True),
        )
    ).first()
    if permission is None:
        return GrantDecision(allowed=False)

    grants = db.scalars(
        select(PermissionGrant).where(
            PermissionGrant.fk_permission_id == permission.pk_permission_id,
            PermissionGrant.scd_active_flag.is_(True),
            (PermissionGrant.fk_user_id == user.pk_user_id)
            | (PermissionGrant.fk_role_id == user.fk_role_id),
        )
    ).all()

    # A username-scoped grant beats a role-scoped one, including a revocation.
    user_grant = next((g for g in grants if g.grant_scope == GrantScope.USER), None)
    role_grant = next((g for g in grants if g.grant_scope == GrantScope.ROLE), None)
    winner = user_grant or role_grant

    if winner is None or not winner.granted_flag:
        return GrantDecision(allowed=False)

    return GrantDecision(
        allowed=True,
        approval_mode=winner.approval_mode,
        grant_id=winner.pk_permission_grant_id,
        required_approvals=winner.required_approvals,
    )


def require(db: Session, user: User | None, module: str, action: str) -> GrantDecision:
    """Raise unless the user may act; return how the action must be executed."""
    if user is None:
        raise PermissionDenied()
    decision = resolve_grant(db, user, module, action)
    if not decision.allowed:
        raise PermissionDenied()
    return decision


def assert_not_last_admin(db: Session, target_user: User) -> None:
    """Block anything that would leave the system with no administrator.

    Part I §2.2 says this is blocked outright — not warned about — and §2.2.2
    adds that impersonation cannot be used to route around it. Because the check
    lives here rather than in a route, both paths hit the same guard.
    """
    admin_role = db.scalars(
        select(Role).where(
            Role.role_code == RoleCode.ADMIN, Role.scd_active_flag.is_(True)
        )
    ).first()
    if admin_role is None or target_user.fk_role_id != admin_role.pk_role_id:
        return

    remaining = db.scalars(
        select(User).where(
            User.fk_role_id == admin_role.pk_role_id,
            User.scd_active_flag.is_(True),
            User.is_active_flag.is_(True),
            User.pk_user_id != target_user.pk_user_id,
        )
    ).all()

    if not remaining:
        raise LastAdminLockout()
