"""Accounts: users, roles, customer profiles, addresses (Part I §2.1, §2.5, §2.6).

Account deletion is not a feature anywhere on the platform (Part I §2.5), so
every table here is ``SCD_``: an account is closed, never removed, and stays
referenceable from the orders it placed and the audit entries it produced.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SCDMixin, UtcDateTime, bilingual, fk, pk
from app.models.enums import CustomerType


class Role(Base, SCDMixin):
    """A named set of default permissions (Part I §2.1).

    The five spec'd roles are seeded, but the table is open: Admin can add a
    role without a code change, and it inherits the whole permission model.
    """

    __tablename__ = "scd_role"
    __grain__ = "One version of one role definition."

    pk_role_id: Mapped[int] = pk("role")
    role_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    name_ar, name_en = bilingual("name", 120)
    description: Mapped[str | None] = mapped_column(Text)
    #: Staff roles reach the admin panel; customers never do.
    is_staff_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Stricter idle timeout for this role, overriding the global default (Part I §2.3).
    session_timeout_minutes: Mapped[int | None] = mapped_column(Integer)
    is_system_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="Seeded roles the application depends on by code; cannot be closed.",
    )

    users: Mapped[list["User"]] = relationship(back_populates="role")

    __table_args__ = (
        Index("ix_scd_role_code_active", "role_code", "scd_active_flag"),
    )


class User(Base, SCDMixin):
    """A login. Every account type shares this table — customers and staff alike —
    so session monitoring and activity logging apply uniformly (Part I §2.8)."""

    __tablename__ = "scd_user"
    __grain__ = "One version of one user account."

    pk_user_id: Mapped[int] = pk("user")
    fk_role_id: Mapped[int] = fk("role", "scd_role.pk_role_id")

    #: Stored lower-cased and Latin-only, Instagram-style; Arabic usernames are
    #: not allowed and uniqueness is case-insensitive (Part I §2.5).
    username: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Bumped on password change so every other live session dies (Part I §2.3).
    password_changed_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    #: Confirm-link required before the account is fully active. Unverified
    #: accounts are never purged — they simply cannot check out (Part I §2.5).
    email_verified_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_verified_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    #: Set false to deactivate; the row is still never deleted.
    is_active_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    preferred_language: Mapped[str] = mapped_column(String(2), nullable=False, default="ar")
    preferred_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="JOD")
    newsletter_opt_in_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Store credit (رصيد) — always JOD-denominated internally; any USD-display
    #: value is converted to JOD before it lands here (Part I §1.1).
    #: Derived from TRX_STORE_CREDIT_ENTRY rather than stored (Part II §1),
    #: see app/services/store_credit.py.

    role: Mapped[Role] = relationship(back_populates="users")
    individual: Mapped["IndividualProfile | None"] = relationship(
        back_populates="user", uselist=False
    )
    company: Mapped["CompanyProfile | None"] = relationship(back_populates="user", uselist=False)
    addresses: Mapped[list["Address"]] = relationship(back_populates="user")

    __table_args__ = (
        # Partial unique indexes: uniqueness is enforced over the *active*
        # version only, so a closed historical row never blocks a legitimate new
        # registration. Both SQLite (3.8+) and PostgreSQL support these.
        Index(
            "uq_scd_user_username_active",
            "username",
            unique=True,
            sqlite_where=text("scd_active_flag = 1"),
            postgresql_where=text("scd_active_flag"),
        ),
        Index(
            "uq_scd_user_email_active",
            "email",
            unique=True,
            sqlite_where=text("scd_active_flag = 1"),
            postgresql_where=text("scd_active_flag"),
        ),
    )

    @property
    def customer_type(self) -> CustomerType | None:
        if self.company is not None:
            return CustomerType.COMPANY
        if self.individual is not None:
            return CustomerType.INDIVIDUAL
        return None


class IndividualProfile(Base, SCDMixin):
    """Individual registration fields (Part I §2.5)."""

    __tablename__ = "scd_individual_profile"
    __grain__ = "One version of one individual customer's personal details."

    pk_individual_profile_id: Mapped[int] = pk("individual_profile")
    fk_user_id: Mapped[int] = fk("user", "scd_user.pk_user_id")

    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    second_name: Mapped[str | None] = mapped_column(String(80))
    third_name: Mapped[str | None] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    birth_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    #: Stored normalised: country code split out, digits only, no formatting
    #: characters (Part I §2.5).
    phone_country_code: Mapped[str] = mapped_column(String(6), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    user: Mapped[User] = relationship(back_populates="individual")

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.second_name, self.third_name, self.last_name]
        return " ".join(p for p in parts if p)


class CompanyProfile(Base, SCDMixin):
    """Company registration fields (Part I §2.5)."""

    __tablename__ = "scd_company_profile"
    __grain__ = "One version of one company customer's details."

    pk_company_profile_id: Mapped[int] = pk("company_profile")
    fk_user_id: Mapped[int] = fk("user", "scd_user.pk_user_id")
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)

    user: Mapped[User] = relationship(back_populates="company")
    contacts: Mapped[list["CompanyContact"]] = relationship(back_populates="company")
    phones: Mapped[list["CompanyPhone"]] = relationship(back_populates="company")


class CompanyContact(Base, SCDMixin):
    """A person authorised to act for a company.

    The contact must already hold their own account — if they don't, they create
    one before the company registration completes (Part I §2.5). Because
    accounts are never deleted, a contact whose role at the company changes
    simply stays on record while a new contact is added alongside them.
    """

    __tablename__ = "scd_company_contact"
    __grain__ = "One version of one contact-person link between a user and a company."

    pk_company_contact_id: Mapped[int] = pk("company_contact")
    fk_company_profile_id: Mapped[int] = fk(
        "company_profile", "scd_company_profile.pk_company_profile_id"
    )
    fk_user_id: Mapped[int] = fk("user", "scd_user.pk_user_id")
    title: Mapped[str | None] = mapped_column(String(120))
    reason: Mapped[str | None] = mapped_column(Text)

    company: Mapped[CompanyProfile] = relationship(back_populates="contacts")


class CompanyPhone(Base, SCDMixin):
    """A company line, optionally with an extension tagged to a person or
    department (Part I §2.5)."""

    __tablename__ = "scd_company_phone"
    __grain__ = "One version of one company phone number or extension."

    pk_company_phone_id: Mapped[int] = pk("company_phone")
    fk_company_profile_id: Mapped[int] = fk(
        "company_profile", "scd_company_profile.pk_company_profile_id"
    )
    phone_country_code: Mapped[str] = mapped_column(String(6), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False)
    extension: Mapped[str | None] = mapped_column(String(10))
    tagged_to: Mapped[str | None] = mapped_column(
        String(120), comment="Person or department this extension reaches."
    )

    company: Mapped[CompanyProfile] = relationship(back_populates="phones")


class Address(Base, SCDMixin):
    """A saved address. Also used as the shipping address snapshot source at
    checkout — the order copies the values rather than pointing here, so a later
    edit never rewrites a past order's delivery record (Part I §8)."""

    __tablename__ = "scd_address"
    __grain__ = "One version of one saved address belonging to one user."

    pk_address_id: Mapped[int] = pk("address")
    fk_user_id: Mapped[int] = fk("user", "scd_user.pk_user_id")
    fk_country_id: Mapped[int] = fk("country", "lkp_country.pk_country_id")
    fk_province_id: Mapped[int] = fk("province", "lkp_province.pk_province_id")

    label: Mapped[str | None] = mapped_column(String(60))
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    address_line: Mapped[str] = mapped_column(Text, nullable=False)
    zip_code: Mapped[str | None] = mapped_column(String(20))
    po_box: Mapped[str | None] = mapped_column(String(20))
    is_default_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user: Mapped[User] = relationship(back_populates="addresses")


class Country(Base, SCDMixin):
    """Lookup, bilingual per Part I §2.5 ("all dropdowns support AR and EN")."""

    __tablename__ = "lkp_country"
    __grain__ = "One version of one country."

    pk_country_id: Mapped[int] = pk("country")
    iso_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    phone_code: Mapped[str] = mapped_column(String(6), nullable=False)
    name_ar, name_en = bilingual("name", 120)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    provinces: Mapped[list["Province"]] = relationship(back_populates="country")


class Province(Base, SCDMixin):
    """Province / governorate / state.

    Jordanian governorates double as the key for shipping-cost rules; countries
    outside Jordan fall through to "not included, will be contacted"
    (Part I §2.2).
    """

    __tablename__ = "lkp_province"
    __grain__ = "One version of one province/governorate/state within a country."

    pk_province_id: Mapped[int] = pk("province")
    fk_country_id: Mapped[int] = fk("country", "lkp_country.pk_country_id")
    name_ar, name_en = bilingual("name", 120)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    country: Mapped[Country] = relationship(back_populates="provinces")

    __table_args__ = (UniqueConstraint("fk_country_id", "name_en", name="province_per_country"),)


class AuthToken(Base, SCDMixin):
    """A single-use link sent by email (Part I §2.5, §2.7).

    Verification and password-reset links used to be generated, mailed, and
    thrown away — nothing stored them, so a reset link could never be redeemed
    and verification worked only by string-searching the outbox body. This is
    where they live now.

    Only the *hash* of the token is stored. The link goes to the customer's
    inbox; a database dump must not hand an attacker a working set of
    password-reset URLs for every account that has ever asked for one.
    """

    __tablename__ = "scd_auth_token"
    __grain__ = "One version of one single-use authentication link."

    pk_auth_token_id: Mapped[int] = pk("auth_token")
    fk_user_id: Mapped[int] = fk("user", "scd_user.pk_user_id")
    #: ``AuthTokenPurpose``. Part of the lookup, so a verification link cannot
    #: be replayed as a password reset.
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    expires_dt: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    #: Set the moment it is redeemed. A used link stops working immediately
    #: rather than staying live until it expires — password-reset emails sit in
    #: inboxes and get forwarded.
    consumed_dt: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    requested_ip: Mapped[str | None] = mapped_column(String(45))

    user: Mapped[User] = relationship()

    __table_args__ = (
        Index("ix_scd_auth_token_lookup", "token_hash", "purpose", "scd_active_flag"),
        Index("ix_scd_auth_token_user_purpose", "fk_user_id", "purpose"),
    )

    def is_live(self, now: dt.datetime) -> bool:
        return (
            self.scd_active_flag
            and self.consumed_dt is None
            and self.expires_dt > now
        )
