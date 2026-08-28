"""Seed the database with everything the application needs to run.

Idempotent: every step checks before inserting, so this can be re-run against an
existing database without duplicating rows. That matters because nothing here is
ever deleted — a careless second run would otherwise leave two of everything,
permanently.

Run with::

    python -m app.db.seed              # reference + demo catalog
    python -m app.db.seed --no-demo    # reference data only (production)
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.db.base import utcnow
from app.db.session import session_scope
from app.models.access import Permission, PermissionGrant
from app.models.catalog import (
    Category,
    Discount,
    Product,
    ProductCategory,
    ProductVariant,
    Publisher,
)
from app.models.enums import (
    ApprovalMode,
    DiscountKind,
    DiscountScope,
    PromocodeKind,
    EmailTemplateCode,
    GrantScope,
    HomepageSectionKind,
    RoleCode,
    StockPoolKind,
)
from app.models.identity import Country, Province, Role, User
from app.models.inventory import Branch, StockLevel, StockPool
from app.models.marketing import (
    AnnouncementBar,
    EmailTemplate,
    HomepageSection,
    Promocode,
    SiteSetting,
)
from app.models.money import ExchangeRate, MoneyBox
from app.models.orders import PaymentChannel, ShippingRule
from app.services.catalog import slugify
from app.services.permissions import PERMISSION_REGISTRY

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


def seed_roles(db: Session) -> dict[str, Role]:
    """The five account types in Part I §2.1."""
    definitions = [
        (RoleCode.ADMIN, "مدير النظام", "Admin", True, None),
        (RoleCode.GENERAL_SECRETARIAT, "الأمانة العامة", "General Secretariat", True, None),
        (RoleCode.STORE_MANAGER, "مدير المكتبة", "Store Manager", True, None),
        # A shared in-branch terminal should not stay signed in all day.
        (RoleCode.STORE_EMPLOYEE, "موظف المكتبة", "Store Employee", True, 60),
        (RoleCode.CUSTOMER, "زبون", "Customer", False, None),
    ]
    roles: dict[str, Role] = {}
    for code, name_ar, name_en, is_staff, timeout in definitions:
        role = db.scalars(
            select(Role).where(Role.role_code == code, Role.scd_active_flag.is_(True))
        ).first()
        if role is None:
            role = Role(
                role_code=code,
                name_ar=name_ar,
                name_en=name_en,
                is_staff_flag=is_staff,
                session_timeout_minutes=timeout,
                is_system_flag=True,
                scd_active_from=utcnow(),
            )
            db.add(role)
            db.flush()
        roles[code] = role
    log.info("seeded_roles", extra={"count": len(roles)})
    return roles


def seed_permissions(db: Session) -> dict[str, Permission]:
    """Mirror the code registry into the table Admin ticks boxes in."""
    permissions: dict[str, Permission] = {}
    for spec in PERMISSION_REGISTRY:
        existing = db.scalars(
            select(Permission).where(
                Permission.module_code == spec.module,
                Permission.action_code == spec.action,
            )
        ).first()
        if existing is None:
            existing = Permission(
                module_code=spec.module,
                action_code=spec.action,
                name_ar=spec.name_ar,
                name_en=spec.name_en,
                default_approval_mode=spec.default_mode,
                scd_active_from=utcnow(),
            )
            db.add(existing)
            db.flush()
        permissions[f"{spec.module}.{spec.action}"] = existing
    log.info("seeded_permissions", extra={"count": len(permissions)})
    return permissions


def seed_role_grants(
    db: Session, roles: dict[str, Role], permissions: dict[str, Permission]
) -> None:
    """Sensible starting grants. Admin re-cuts these from the UI afterwards.

    Admin receives everything on Single Approval — a maker-checker rule that
    nobody can check would deadlock the system on day one. The other staff roles
    inherit each permission's own default mode, which is where the
    Maker-Checker defaults from the registry take effect.
    """
    baseline: dict[str, list[str]] = {
        RoleCode.ADMIN: list(permissions),
        RoleCode.GENERAL_SECRETARIAT: [
            code for code in permissions
            if code.startswith(("reports.", "orders.view", "inventory.view",
                                "money_boxes.view", "consignment.view", "settings.view"))
        ],
        RoleCode.STORE_MANAGER: [
            code for code in permissions
            if not code.startswith(("access.", "settings.set_", "users.create_staff"))
        ],
        RoleCode.STORE_EMPLOYEE: [
            "catalog.view", "orders.view", "orders.prepare", "orders.set_line_status",
            "orders.record_payment",
            "inventory.view", "inventory.receive_shipment", "inventory.print_barcodes",
            "returns.view", "returns.inspect",
        ],
        RoleCode.CUSTOMER: [],
    }

    created = 0
    for role_code, codes in baseline.items():
        role = roles[role_code]
        for code in codes:
            permission = permissions.get(code)
            if permission is None:
                continue
            exists = db.scalars(
                select(PermissionGrant).where(
                    PermissionGrant.fk_permission_id == permission.pk_permission_id,
                    PermissionGrant.fk_role_id == role.pk_role_id,
                    PermissionGrant.scd_active_flag.is_(True),
                )
            ).first()
            if exists:
                continue
            db.add(
                PermissionGrant(
                    fk_permission_id=permission.pk_permission_id,
                    grant_scope=GrantScope.ROLE,
                    fk_role_id=role.pk_role_id,
                    granted_flag=True,
                    approval_mode=(
                        ApprovalMode.SINGLE
                        if role_code == RoleCode.ADMIN
                        else permission.default_approval_mode
                    ),
                    scd_active_from=utcnow(),
                )
            )
            created += 1
    log.info("seeded_role_grants", extra={"count": created})


def seed_geography(db: Session) -> Country:
    """Jordan and its twelve governorates, plus the neighbours most orders go to."""
    countries = [
        ("JO", "+962", "الأردن", "Jordan", 0),
        ("PS", "+970", "فلسطين", "Palestine", 1),
        ("SA", "+966", "السعودية", "Saudi Arabia", 2),
        ("AE", "+971", "الإمارات", "United Arab Emirates", 3),
        ("LB", "+961", "لبنان", "Lebanon", 4),
        ("IQ", "+964", "العراق", "Iraq", 5),
    ]
    jordan: Country | None = None
    for iso, phone, name_ar, name_en, order in countries:
        country = db.scalars(
            select(Country).where(Country.iso_code == iso, Country.scd_active_flag.is_(True))
        ).first()
        if country is None:
            country = Country(
                iso_code=iso, phone_code=phone, name_ar=name_ar, name_en=name_en,
                sort_order=order, scd_active_from=utcnow(),
            )
            db.add(country)
            db.flush()
        if iso == "JO":
            jordan = country

    governorates = [
        ("عمّان", "Amman"), ("إربد", "Irbid"), ("الزرقاء", "Zarqa"),
        ("البلقاء", "Balqa"), ("المفرق", "Mafraq"), ("جرش", "Jerash"),
        ("عجلون", "Ajloun"), ("مادبا", "Madaba"), ("الكرك", "Karak"),
        ("الطفيلة", "Tafilah"), ("معان", "Ma'an"), ("العقبة", "Aqaba"),
    ]
    for index, (name_ar, name_en) in enumerate(governorates):
        exists = db.scalars(
            select(Province).where(
                Province.fk_country_id == jordan.pk_country_id,
                Province.name_en == name_en,
            )
        ).first()
        if exists is None:
            db.add(
                Province(
                    fk_country_id=jordan.pk_country_id,
                    name_ar=name_ar, name_en=name_en,
                    sort_order=index, scd_active_from=utcnow(),
                )
            )
    log.info("seeded_geography")
    return jordan


def seed_exchange_rate(db: Session) -> None:
    """Default 1 JOD = 1.41 USD (Part I §1.1)."""
    existing = db.scalars(
        select(ExchangeRate).where(ExchangeRate.scd_active_flag.is_(True))
    ).first()
    if existing is None:
        db.add(
            ExchangeRate(
                jod_to_usd_rate=Decimal(str(settings.default_usd_rate)),
                note="Seeded default.",
                scd_active_from=utcnow(),
            )
        )
        log.info("seeded_exchange_rate")


def seed_payment_channels(db: Session) -> None:
    channels = [
        ("cash", "نقداً", "Cash", False),
        ("visa", "فيزا", "Visa", False),
        ("cliq", "كليك", "CliQ", False),
        ("bank_transfer", "حوالة بنكية", "Bank transfer", False),
        ("store_credit", "رصيد", "Store credit", True),
    ]
    for index, (code, name_ar, name_en, is_credit) in enumerate(channels):
        exists = db.scalars(
            select(PaymentChannel).where(PaymentChannel.channel_code == code)
        ).first()
        if exists is None:
            db.add(
                PaymentChannel(
                    channel_code=code, name_ar=name_ar, name_en=name_en,
                    is_store_credit_flag=is_credit, sort_order=index,
                    scd_active_from=utcnow(),
                )
            )
    log.info("seeded_payment_channels")


def seed_site_settings(db: Session) -> None:
    """Footer copy, social links and the storewide display toggles (§3.2, §5.3)."""
    values: list[tuple[str, str | None, str | None, bool | None]] = [
        ("footer_about",
         "شبيبة ستور — المكتبة الكاثوليكية المتكاملة. كتب وأيقونات وهدايا روحية.",
         "JEC Store — the complete Catholic bookstore. Books, icons and spiritual gifts.",
         None),
        ("contact_phone", "+962 6 000 0000", "+962 6 000 0000", None),
        ("contact_email", "info@jecjordan.com", "info@jecjordan.com", None),
        ("whatsapp_number", "962790000000", "962790000000", None),
        ("facebook_url", "https://facebook.com/jecjordan", "https://facebook.com/jecjordan", None),
        ("facebook_page_url", "https://www.facebook.com/jecjordan",
         "https://www.facebook.com/jecjordan", None),
        ("messenger_url", "https://m.me/jecjordan", "https://m.me/jecjordan", None),
        ("instagram_url", "https://instagram.com/jecjordan", "https://instagram.com/jecjordan", None),
        # Storewide defaults for the public counters; per-product overrides win.
        ("show_view_count", None, None, True),
        ("show_purchase_count", None, None, True),
    ]
    for key, text_ar, text_en, flag in values:
        exists = db.scalars(
            select(SiteSetting).where(
                SiteSetting.setting_key == key, SiteSetting.scd_active_flag.is_(True)
            )
        ).first()
        if exists is None:
            db.add(
                SiteSetting(
                    setting_key=key, value_text_ar=text_ar, value_text_en=text_en,
                    value_flag=flag, scd_active_from=utcnow(),
                )
            )
    log.info("seeded_site_settings")


def seed_email_templates(db: Session) -> None:
    """Starting templates. Admin owns the wording; the system owns the tokens
    listed in ``required_placeholders`` (Part I §2.7)."""
    templates = [
        (
            EmailTemplateCode.WELCOME,
            "أهلاً بك في شبيبة ستور", "Welcome to JEC Store",
            "مرحباً {username}،\n\nأهلاً بك في شبيبة ستور.",
            "Hello {username},\n\nWelcome to JEC Store.",
            None,
        ),
        (
            EmailTemplateCode.EMAIL_VERIFICATION,
            "تفعيل حسابك", "Verify your account",
            "مرحباً {username}،\n\nلتفعيل حسابك اضغط الرابط:\n{verify_url}",
            "Hello {username},\n\nVerify your account:\n{verify_url}",
            "verify_url",
        ),
        (
            EmailTemplateCode.FORGOT_PASSWORD,
            "إعادة تعيين كلمة المرور", "Reset your password",
            "لإعادة تعيين كلمة المرور اضغط:\n{reset_url}\nينتهي الرابط خلال {expiry_hours} ساعة.",
            "Reset your password:\n{reset_url}\nThis link expires in {expiry_hours} hours.",
            "reset_url,expiry_hours",
        ),
        (
            EmailTemplateCode.ORDER_CONFIRMATION,
            "تأكيد الطلب {order_number}", "Order {order_number} confirmed",
            "شكراً لك. تم استلام طلبك رقم {order_number} بقيمة {total}.",
            "Thank you. We received order {order_number} for {total}.",
            "order_number",
        ),
        (
            EmailTemplateCode.ORDER_STATUS_CHANGE,
            "تحديث على طلبك {order_number}", "Update on order {order_number}",
            "حالة طلبك {order_number} أصبحت: {status}.",
            "Your order {order_number} is now: {status}.",
            "order_number,status",
        ),
        (
            EmailTemplateCode.PAYMENT_RECEIVED,
            "تم استلام الدفعة", "Payment received",
            "تم استلام دفعة بقيمة {amount} على الطلب {order_number}.",
            "We received {amount} towards order {order_number}.",
            "amount,order_number",
        ),
        (
            EmailTemplateCode.RETURN_PROCESSED,
            "تمت معالجة المرتجع", "Return processed",
            "تمت معالجة المرتجع {return_number}.",
            "Return {return_number} has been processed.",
            "return_number",
        ),
        (
            EmailTemplateCode.LOW_STOCK_ALERT,
            "تنبيه: مخزون منخفض", "Low stock alert",
            "المنتج {product_name} وصل إلى {quantity}.",
            "{product_name} is down to {quantity}.",
            "product_name",
        ),
    ]
    for code, subject_ar, subject_en, body_ar, body_en, required in templates:
        exists = db.scalars(
            select(EmailTemplate).where(
                EmailTemplate.template_code == code,
                EmailTemplate.scd_active_flag.is_(True),
            )
        ).first()
        if exists is None:
            db.add(
                EmailTemplate(
                    template_code=code,
                    subject_ar=subject_ar, subject_en=subject_en,
                    body_ar=body_ar, body_en=body_en,
                    required_placeholders=required,
                    scd_active_from=utcnow(),
                )
            )
    log.info("seeded_email_templates")


def seed_branches_and_pools(db: Session) -> dict[str, StockPool]:
    """One branch plus central storage.

    Central storage exists from the start because it is what makes the
    "Available — pickup/shipping arranged on order" state reachable (§5.3).
    """
    branch = db.scalars(
        select(Branch).where(Branch.scd_active_flag.is_(True))
    ).first()
    if branch is None:
        branch = Branch(
            name_ar="الفرع الرئيسي — عمّان",
            name_en="Main Branch — Amman",
            phone_country_code="+962",
            phone_number="60000000",
            address_ar="عمّان، الأردن",
            address_en="Amman, Jordan",
            latitude=Decimal("31.963158"),
            longitude=Decimal("35.930359"),
            scd_active_from=utcnow(),
        )
        db.add(branch)
        db.flush()

    pools: dict[str, StockPool] = {}
    definitions = [
        ("branch", StockPoolKind.BRANCH, branch.pk_branch_id,
         "مخزون الفرع الرئيسي", "Main Branch stock", True, True),
        ("central", StockPoolKind.CENTRAL_STORAGE, None,
         "المستودع المركزي", "Central storage", True, True),
    ]
    for key, kind, branch_id, name_ar, name_en, sellable, owned in definitions:
        pool = db.scalars(
            select(StockPool).where(
                StockPool.pool_kind == kind,
                StockPool.name_en == name_en,
                StockPool.scd_active_flag.is_(True),
            )
        ).first()
        if pool is None:
            pool = StockPool(
                pool_kind=kind, fk_branch_id=branch_id,
                name_ar=name_ar, name_en=name_en,
                is_sellable_flag=sellable, is_owned_flag=owned,
                scd_active_from=utcnow(),
            )
            db.add(pool)
            db.flush()
        pools[key] = pool

    if not db.scalars(select(MoneyBox).where(MoneyBox.scd_active_flag.is_(True))).first():
        db.add(
            MoneyBox(
                box_code="main_till", name_ar="الصندوق الرئيسي", name_en="Main till",
                fk_branch_id=branch.pk_branch_id, opening_balance_amt=Decimal("0"),
                opened_dt=utcnow(), scd_active_from=utcnow(),
            )
        )

    log.info("seeded_branches_and_pools")
    return pools


def seed_shipping_rules(db: Session, jordan: Country) -> None:
    """Shipping by Jordanian governorate, with a free-above threshold (Part I §2.2).

    Amman is cheaper than the rest of the country, everywhere else in Jordan
    falls through to the country-wide rule, and anywhere outside Jordan hits the
    global "we will contact you" rule — which is the third outcome §2.2 requires
    alongside a price.
    """
    if db.scalars(select(ShippingRule).limit(1)).first():
        return

    now = utcnow()
    amman = db.scalars(
        select(Province).where(
            Province.fk_country_id == jordan.pk_country_id,
            Province.name_en == "Amman",
            Province.scd_active_flag.is_(True),
        )
    ).first()

    rules = [
        # Most specific: Amman.
        ShippingRule(
            fk_country_id=jordan.pk_country_id,
            fk_province_id=amman.pk_province_id if amman else None,
            cost_amt=Decimal("2.000"),
            free_above_amt=Decimal("30.000"),
            priority=20,
            note_ar="توصيل داخل عمّان",
            note_en="Delivery within Amman",
            scd_active_from=now,
        ),
        # Rest of Jordan.
        ShippingRule(
            fk_country_id=jordan.pk_country_id,
            fk_province_id=None,
            cost_amt=Decimal("3.500"),
            free_above_amt=Decimal("50.000"),
            priority=10,
            note_ar="توصيل داخل المملكة",
            note_en="Delivery within Jordan",
            scd_active_from=now,
        ),
        # Everywhere else: not included, will be contacted.
        ShippingRule(
            fk_country_id=None,
            fk_province_id=None,
            cost_amt=Decimal("0"),
            quote_on_contact_flag=True,
            priority=0,
            note_ar="غير مشمول — سيتم التواصل معك",
            note_en="Not included — we will contact you",
            scd_active_from=now,
        ),
    ]
    for rule in rules:
        db.add(rule)
    log.info("seeded_shipping_rules", extra={"count": len(rules)})


def seed_demo_promocode(db: Session) -> None:
    """One working code, so the promocode path is exercisable end to end."""
    if db.scalars(select(Promocode).limit(1)).first():
        return

    db.add(
        Promocode(
            code="WELCOME10",
            name_ar="خصم الترحيب",
            name_en="Welcome discount",
            promocode_kind=PromocodeKind.PERCENTAGE_CAPPED,
            percentage=Decimal("10"),
            max_discount_amt=Decimal("5.000"),
            minimum_order_amt=Decimal("10.000"),
            max_uses_per_customer=1,
            stacks_with_item_discount_flag=True,
            expires_dt=utcnow() + dt.timedelta(days=365),
            scd_active_from=utcnow(),
        )
    )
    log.info("seeded_demo_promocode", extra={"code": "WELCOME10"})


def seed_admin_user(db: Session, roles: dict[str, Role]) -> None:
    """A first administrator, so the panel is reachable on a fresh install.

    The password is a known placeholder and is meant to be changed immediately —
    it is logged loudly on creation for exactly that reason.
    """
    existing = db.scalars(
        select(User).where(User.username == "admin", User.scd_active_flag.is_(True))
    ).first()
    if existing is not None:
        return

    db.add(
        User(
            fk_role_id=roles[RoleCode.ADMIN].pk_role_id,
            username="admin",
            email="admin@jecjordan.com",
            password_hash=hash_password("ChangeMe!2026"),
            password_changed_dt=utcnow(),
            email_verified_flag=True,
            email_verified_dt=utcnow(),
            is_active_flag=True,
            preferred_language="ar",
            scd_active_from=utcnow(),
        )
    )
    log.warning(
        "seeded_admin_user",
        extra={"username": "admin", "action_required": "change this password now"},
    )


# ---------------------------------------------------------------------------
# Demo catalog
# ---------------------------------------------------------------------------


def seed_demo_catalog(db: Session, pools: dict[str, StockPool]) -> None:
    """A small bilingual catalog, so the storefront has something real to show."""
    if db.scalars(select(Product).limit(1)).first():
        log.info("demo_catalog_exists_skipping")
        return

    now = utcnow()

    publishers = []
    for index, (name_ar, name_en) in enumerate([
        ("دار المشرق", "Dar El-Machreq"),
        ("منشورات المكتبة البولسية", "Pauline Publications"),
        ("دار الكلمة", "Dar Al-Kalima"),
        ("منشورات الرسالة", "Al-Risala Publications"),
    ]):
        publisher = Publisher(
            name_ar=name_ar, name_en=name_en, slug=slugify(name_en),
            sort_order=index, scd_active_from=now,
        )
        db.add(publisher)
        publishers.append(publisher)
    db.flush()

    category_tree = [
        ("كتب", "Books", [
            ("الكتاب المقدس", "Bibles"),
            ("كتب روحية", "Spiritual books"),
            ("كتب للأطفال", "Children's books"),
        ]),
        ("أيقونات", "Icons", []),
        ("مسابح وصلبان", "Rosaries & crosses", []),
        ("تماثيل", "Statues", []),
        ("هدايا روحية", "Spiritual gifts", []),
        ("أفلام وتراتيل", "Films & hymns", []),
    ]

    categories: dict[str, Category] = {}
    for index, (name_ar, name_en, children) in enumerate(category_tree):
        parent = Category(
            name_ar=name_ar, name_en=name_en,
            slug_ar=slugify(name_ar), slug_en=slugify(name_en),
            sort_order=index, ancestor_path="/", depth=0,
            scd_active_from=now,
        )
        db.add(parent)
        db.flush()
        categories[name_en] = parent

        for child_index, (child_ar, child_en) in enumerate(children):
            child = Category(
                fk_parent_category_id=parent.pk_category_id,
                name_ar=child_ar, name_en=child_en,
                slug_ar=slugify(child_ar), slug_en=slugify(child_en),
                sort_order=child_index,
                ancestor_path=f"/{parent.pk_category_id}/",
                depth=1,
                scd_active_from=now,
            )
            db.add(child)
            db.flush()
            categories[child_en] = child

    demo_products = [
        ("الكتاب المقدس — الترجمة العربية المشتركة", "Holy Bible — Arabic Common Translation",
         "Bibles", 0, Decimal("18.500"), 14446, 184),
        ("الكتاب المقدس — طبعة الجيب", "Holy Bible — Pocket Edition",
         "Bibles", 0, Decimal("9.750"), 8210, 96),
        ("الاقتداء بالمسيح", "The Imitation of Christ",
         "Spiritual books", 1, Decimal("7.250"), 5120, 73),
        ("قصص القديسين للأطفال", "Lives of the Saints for Children",
         "Children's books", 1, Decimal("6.000"), 3980, 61),
        ("أيقونة السيدة العذراء", "Icon of the Virgin Mary",
         "Icons", 2, Decimal("24.000"), 6720, 118),
        ("أيقونة المسيح الضابط الكل", "Icon of Christ Pantocrator",
         "Icons", 2, Decimal("26.500"), 4310, 54),
        ("مسبحة خشب الزيتون", "Olive Wood Rosary",
         "Rosaries & crosses", 3, Decimal("4.500"), 11200, 342),
        ("صليب خشب الزيتون", "Olive Wood Cross",
         "Rosaries & crosses", 3, Decimal("12.000"), 7640, 155),
        ("تمثال القديس يوسف", "Statue of Saint Joseph",
         "Statues", 0, Decimal("32.000"), 2890, 27),
        ("شمعة العائلة المقدسة", "Holy Family Candle",
         "Spiritual gifts", 1, Decimal("3.750"), 4470, 208),
        ("ألبوم تراتيل الميلاد", "Christmas Hymns Album",
         "Films & hymns", 2, Decimal("5.500"), 1930, 42),
        ("فيلم حياة المسيح", "The Life of Christ — Film",
         "Films & hymns", 3, Decimal("8.000"), 2240, 35),
    ]

    for index, (name_ar, name_en, category_key, publisher_index,
                price, views, purchases) in enumerate(demo_products):
        product = Product(
            fk_publisher_id=publishers[publisher_index].pk_publisher_id,
            name_ar=name_ar, name_en=name_en,
            slug_ar=slugify(name_ar), slug_en=slugify(name_en),
            short_description_ar="منتج من مكتبة شبيبة ستور.",
            short_description_en="Available from JEC Store.",
            description_ar="وصف تفصيلي للمنتج يُدخل من لوحة التحكم بالعربية والإنجليزية.",
            description_en="A full product description, entered from the admin panel "
                           "in both Arabic and English.",
            base_price_amt=price,
            view_count=views,
            purchase_count=purchases,
            min_stock_level=3,
            optimal_stock_level=20,
            max_stock_level=50,
            # Staggered so New Arrivals has a meaningful order.
            published_dt=now - dt.timedelta(days=index * 3),
            scd_active_from=now,
        )
        db.add(product)
        db.flush()

        db.add(
            ProductCategory(
                fk_product_id=product.pk_product_id,
                fk_category_id=categories[category_key].pk_category_id,
                is_primary_flag=True,
                scd_active_from=now,
            )
        )

        variant = ProductVariant(
            fk_product_id=product.pk_product_id,
            sku=f"JEC-{product.pk_product_id:05d}",
            barcode=f"628{product.pk_product_id:010d}",
            fk_default_stock_pool_id=pools["branch"].pk_stock_pool_id,
            scd_active_from=now,
        )
        db.add(variant)
        db.flush()

        # Most items in the branch; two deliberately only in central storage, so
        # the "arranged on order" state is visible on the seeded site.
        pool = pools["central"] if index in (8, 11) else pools["branch"]
        db.add(
            StockLevel(
                fk_product_variant_id=variant.pk_product_variant_id,
                fk_stock_pool_id=pool.pk_stock_pool_id,
                quantity_on_hand=12,
                quantity_reserved=0,
                average_cost_amt=price * Decimal("0.6"),
                last_movement_dt=now,
                scd_active_from=now,
            )
        )

    # A live category-wide discount, so strikethrough pricing and the
    # Discounted carousel both have something to render.
    db.add(
        Discount(
            name_ar="خصم الكتب المقدسة", name_en="Bibles promotion",
            discount_scope=DiscountScope.CATEGORY,
            fk_category_id=categories["Bibles"].pk_category_id,
            include_subcategories_flag=True,
            discount_kind=DiscountKind.PERCENTAGE,
            percentage=Decimal("20"),
            starts_dt=now - dt.timedelta(days=1),
            ends_dt=now + dt.timedelta(days=30),
            priority=10,
            scd_active_from=now,
        )
    )

    log.info("seeded_demo_catalog", extra={"products": len(demo_products)})


def seed_homepage(db: Session) -> None:
    """The homepage as data — the tabbed carousels from §17.3 and §4."""
    if db.scalars(select(HomepageSection).limit(1)).first():
        return

    now = utcnow()
    sections = [
        (HomepageSectionKind.BANNER, "أهلاً بكم في شبيبة ستور",
         "Welcome to JEC Store", 0, 0),
        (HomepageSectionKind.CATEGORY_SHOWCASE, "تسوّق حسب القسم",
         "Shop by category", 1, 12),
        (HomepageSectionKind.NEW_ARRIVALS, "وصل حديثاً", "New arrivals", 2, 6),
        (HomepageSectionKind.BEST_SELLERS, "الأكثر مبيعاً", "Best sellers", 3, 6),
        (HomepageSectionKind.DISCOUNTED, "أسعار مخفّضة", "Discounted", 4, 6),
        (HomepageSectionKind.MOST_VIEWED, "الأكثر مشاهدة", "Most viewed", 5, 6),
        (HomepageSectionKind.PUBLISHER_CAROUSEL, "دور النشر", "Publishers", 6, 12),
    ]
    for kind, title_ar, title_en, order, limit in sections:
        db.add(
            HomepageSection(
                section_kind=kind,
                title_ar=title_ar, title_en=title_en,
                subtitle_ar="المكتبة الكاثوليكية المتكاملة" if order == 0 else None,
                subtitle_en="The Complete Catholic Bookstore" if order == 0 else None,
                sort_order=order,
                item_limit=limit,
                is_enabled_flag=True,
                scd_active_from=now,
            )
        )

    if not db.scalars(select(AnnouncementBar).limit(1)).first():
        db.add(
            AnnouncementBar(
                message_ar="التوصيل متاح لكل محافظات المملكة — الشحن مجاني للطلبات فوق ٣٠ ديناراً.",
                message_en="Delivery available across Jordan — free shipping on orders over 30 JOD.",
                is_enabled_flag=True,
                is_dismissible_flag=True,
                priority=0,
                scd_active_from=now,
            )
        )

    log.info("seeded_homepage")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(*, include_demo: bool = True) -> None:
    with session_scope() as db:
        roles = seed_roles(db)
        permissions = seed_permissions(db)
        seed_role_grants(db, roles, permissions)
        jordan = seed_geography(db)
        seed_shipping_rules(db, jordan)
        seed_exchange_rate(db)
        seed_payment_channels(db)
        seed_site_settings(db)
        seed_email_templates(db)
        pools = seed_branches_and_pools(db)
        seed_admin_user(db, roles)

        if include_demo:
            seed_demo_catalog(db, pools)
            seed_demo_promocode(db)
            seed_homepage(db)

    log.info("seed_complete", extra={"demo": include_demo})


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(description="Seed the JEC Store database.")
    parser.add_argument(
        "--no-demo",
        action="store_true",
        help="Seed reference data only — no demo catalog or homepage.",
    )
    args = parser.parse_args()
    run(include_demo=not args.no_demo)
