# شبيبة ستور — المكتبة الكاثوليكية المتكاملة
## Website Requirements Specification (Consolidated)

> This document merges the original functional spec with all follow-up decisions and edge-case resolutions, organized by module, plus the project's technical/data engineering standards. Every previously "open" question has been folded into its relevant section as a resolved rule, marked **[Decision]**. Features carried over from an audit of the legacy site (jecjordan.com) that weren't in the original spec are marked **[Added — legacy parity]**.

---

## 1. Project Basics

- **Site name:** شبيبة ستور - المكتبة الكاثوليكية المتكاملة
- **Logo:** `JECStoreLogo.png` — used on the website, browser tab (favicon), emails, invoices, and any other branded surface.
- **Languages:** Arabic and English, fully bilingual. Any field whose value can differ by language (names, descriptions, category names, homepage content, footer text, email templates, etc.) must have **separate Arabic and English inputs** wherever it's entered in the admin panel.
- **Platform:** Must work as both a website and a mobile website (responsive is a hard requirement, not an afterthought — most shoppers arrive via WhatsApp-linked mobile browsers).
- **RTL handling:** Beyond basic text direction, must also correctly handle RTL-aware numeral formatting and price/date layout throughout the site and admin panel.

### 1.1 Currency
- JOD and USD toggle available to the shopper site-wide.
- All prices are **stored in JOD only** — USD is a display-only conversion.
- Conversion rate is set in the admin panel, **default 1 JOD = 1.41 USD**.
- Keep a dated history of rate changes; completed/past orders always reflect the rate at time of sale for reporting purposes.
- **[Decision] Cart-in-progress pricing:** An item sitting in an open cart does **not** lock in the rate or discount at add-to-cart time — it always reflects the **current** rate and current discount at the moment of checkout, not the rate/discount that applied when it was added.
- **[Decision] Store credit (رصيد) currency:** رصيد is always JOD-denominated internally. Any value a customer paid or is owed in USD-display must be converted to JOD **before** being stored.
- **[Decision] Foreign-currency shipments:** Only JOD or USD are accepted for shipment invoice costing — no other foreign currencies.
- Currency must be clearly specified/labeled whenever an invoice is downloaded.

---

## 2. Accounts & Access Management

### 2.1 Account Types (Roles)
1. Admin
2. JEC Jordan General Secretariat
3. JEC Store Manager
4. JEC Store Employee
5. Customer (Company / Individual)

### 2.2 Privilege / Access Control
- A dedicated **Access Management page**, initially visible only to Admin.
- Admin can grant other roles/users access to this page itself (meta-permission).
- Permissions can be assigned at two levels:
  - **Per individual username**
  - **Per role** (applies to everyone in that role)
- Permissions are broken down **per module** — catalog, orders, money boxes, consignment, users, reports, newsletter, shipping cost rules (by Jordanian governorate, or "not included, will be contacted" for non-Jordan / free-shipping-above-threshold rules), etc. — rather than all-or-nothing per page, so e.g. an Employee can be given order-prep access without money-box access.
- **[Decision] Last-admin lockout protection:** The system must prevent an Admin from revoking their own or the only remaining other Admin's access to the Access Management page — this action is blocked outright.

#### 2.2.1 Maker-Checker vs. Single Approval [Decision]
Every permission entry on the Access Management page (per module/action — e.g. "apply discount," "issue refund," "delete category," "modify placed order," "create money box transaction," etc.) carries an additional **approval-mode** setting, independent of whether the permission itself is granted:

- **Single Approval** — the action executes immediately once performed.
- **Maker-Checker** — the action is created in a **pending** state and requires a second person's approval before it takes effect.

**Granularity:** The approval-mode setting is configured at the **same two levels as permissions themselves** — per role, or per specific username — so the same action can have a different approval mode depending on who's performing it. Example: *JEC Store Manager* has **Maker-Checker** on "apply invoice-level discount," while *JEC Jordan General Secretariat* has **Single Approval** on that same action.

**Defining the checker(s):** When an action/role/username combination is set to Maker-Checker, Admin also defines **who is eligible to check it** — any combination of:
- One or more specific **roles** (anyone holding that role can approve), and/or
- One or more specific **usernames**

Any one eligible checker approving or rejecting resolves the request — it does not require unanimous sign-off unless Admin explicitly configures multiple required approvals.

**Rules:**
- A maker can **never approve their own action**, even if they also happen to hold a role that would otherwise qualify them as a checker for that action type.
- Pending actions sit in a **pending-approval queue**, visible to all eligible checkers, until approved or rejected.
- Checkers can **approve** (action executes) or **reject** (action is discarded, with an optional note), and the maker is notified of the outcome either way.
- Every maker-checker event — request created, approved, rejected, by whom, when — is written to the **audit log** (Section 2.2), in addition to the underlying action itself once/if it executes.
- If a checker's access is later revoked or their account deactivated, their pending approval decisions remain **immutable historical records** (same rule as 2.2's deleted-staff clause) — they don't retroactively invalidate.

#### 2.2.2 "Login As" / User Impersonation [Added]
Admin (and anyone granted the privilege) can **log in as another specific user** — seeing and acting on the system exactly as that user would — most commonly for troubleshooting a customer's account issue or verifying what a staff member's permissions actually let them see.

- **It's a permission like any other.** "Login As" is its own entry in the Access Management module list (Section 2.2), so it follows the exact same granularity rules as every other permission:
  - Grantable **per role** or **per specific username**.
  - The grant also specifies **who it can be used on** — e.g. Admin can give a Store Manager the ability to log in as any *Customer* account, without extending that to logging in as another *Store Manager* or *Admin*. This scoping is defined at grant time (by role and/or specific username on the target side, not just the granting side).
- **Delegatable.** Because it's a normal permission, whoever holds it — including Admin — can grant it onward to others, same as any other module/action in Access Management. It isn't a hardcoded Admin-only superpower; it's configured like the rest of the system.
- **Approval mode applies too.** "Login As" is eligible for the **Single Approval / Maker-Checker** setting from Section 2.2.1, same as any other permission — e.g. Admin might require Maker-Checker for anyone logging in as another *Admin* or *Store Manager* account, while Single Approval is fine for logging in as a *Customer* account to help with a support issue.
- **Visible while active:** a persistent on-screen indicator/banner shows that the current session is an impersonated one (who is impersonating, who they're logged in as), so it's never ambiguous which account is actually acting.
- **Fully logged:** every impersonation session — who started it, whose account, start time, end time, and every action taken while impersonating — is written to the audit log (Section 2.2) and the activity-tracking log (Section 2.8), tagged distinctly from that user's own normal sessions so the two are never confused in reporting.
- **Restrictions:**
  - Cannot be used to bypass the last-admin lockout protection (Section 2.2) — impersonating into the last remaining Admin account still can't be used to strip that account's own access.
  - An impersonated session inherits the **target account's permissions**, not the impersonator's — someone logging in as a Customer can't use elevated Admin actions while in that session.
  - Ending the impersonated session returns the person to their own original session automatically; it isn't a full re-login.

- **[Updated] Analytics & reporting:** Basic analytics dashboard (top sellers, slow movers, traffic sources) — Google Analytics integration or in-house build. The system produces extensive supporting dashboards and reports across every module (sales, inventory, consignment, money boxes, promocodes, returns, staff activity, etc.) — reporting is treated as a first-class feature throughout, not an afterthought bolted onto one page. **Every report and dashboard view must be exportable** (CSV, Excel, and PDF at minimum) for offline use and accounting handoff.
- **Audit log:** who changed what, when (price changes, discounts applied, permission changes, stock adjustments) — essential once multiple roles can touch money and inventory.
- **[Decision] Deleted/deactivated staff:** Audit-log entries, orders they prepared, and money-box transactions they recorded must reference them as **immutable historical records**, not live foreign keys that break or cascade if the staff account is later removed.

### 2.3 Session Management & Security [Added]
- **Session timeout:** idle/session-length timeout is **configurable in the admin panel** (a global default, with the option to set a stricter value per role if needed) rather than hardcoded.
- **Force logout on password change:** changing a password immediately invalidates all other active sessions for that account, on every device.
- **Timeout behavior:** when a session times out, the user is logged out and redirected to login; in-progress cart contents are preserved (not lost) so they aren't punished for stepping away.
- **Admin session control:** Admin can view a user's active sessions and force-terminate any of them manually (e.g. a lost device, a suspicious login) — see the "currently logged in" dashboard in 2.8.

### 2.4 Bot & Abuse Protection [Added]
- **CAPTCHA (or equivalent challenge)** on registration, login, and password-reset — tuned to trigger progressively (e.g. after repeated failed attempts) rather than on every single request, to avoid friction for legitimate users.
- **Rate limiting** on login attempts, registration submissions, and password-reset requests, per account and per IP, with temporary lockout/backoff on abuse patterns.
- Failed-login and lockout events feed into the activity log in 2.8, so Admin can see patterns (e.g. repeated attempts against one account) rather than isolated events.

### 2.5 Registration Flow
Entry point: same screen as login, with an account-type selector first (**Company** vs **Individual**).

**Individual fields:**
- First name *(required)*, Second name *(optional)*, Third name *(optional)*, Last name *(required)*
- Birth date *(required)*
- Phone number *(required)* — country code selector + number, validated on entry, normalized before storage (strip dashes/spaces/formatting)

**Company fields:**
- Company name
- One or more **contact persons**: existing username + title/position/reason for being a contact. If the person has no account yet, they must create one before the company registration can complete. *(Optional)*
- **Company phone number**, with optional extensions, each tagged to a specific person or department. *(Optional if a contact person is set)*
- **[Decision] Contact-person departure:** Account deletion is not offered as an option anywhere on the platform (see 2.6), which sidesteps the "contact-less company" problem — a contact person's account persists even if their role at the company changes, and a new contact can always be added alongside them.

**Shared fields (both types):**
- Country *(dropdown, required)*, Province/Governorate/State *(dropdown, required)*, City *(free text, required)*, Address *(required)*
- Email *(required, validated)*
- Username *(required)*
- Password + Confirm Password (with strength requirements)
- Newsletter opt-in (Yes/No)

**Rules:**
- All dropdowns support Arabic and English.
- All input is trimmed/cleaned before storage, not just phone numbers.
- **[Decision] Username/email normalization:** Always unify on storage. Usernames are **not case-sensitive** and follow Instagram-style rules (Latin characters only — **Arabic usernames are not allowed**). Uniqueness is enforced with clear inline error messages.
- **Email verification:** Confirm-link required before an account is fully "active," separate from the welcome email.
  - **[Decision]** Unverified accounts are **never purged** — they sit indefinitely. An unverified account **cannot check out**, and the profile page shows a **"Not Verified"** badge next to the email with a button to resend/complete verification.
- **[Decision] Account deletion:** Not offered as a feature anywhere in the system (see also 2.5 contact-person note).
- **CAPTCHA and rate limiting apply to this flow** — see 2.4.

### 2.6 Customer Profile (post-registration)
- Edit their own submitted data
- Change password
- View order history, track current orders, view/download past invoices, view saved addresses, manage saved payment/shipping preferences, and view their رصيد (store credit) balance if applicable
- Email verification status/badge, per 2.5
- **[Added — legacy parity] Dedicated newsletter preferences page:** a standalone page within the customer profile (not just the registration checkbox or footer signup box) where the customer can view and change their newsletter subscription status at any time.
- **[Added — legacy parity] Product Compare list:** customers can view and manage their active comparison list from their profile — see Section 14.
- **[Added] Active sessions view:** customers can see their own active login sessions (device/approximate location, last active) and log out of other devices remotely — the customer-facing counterpart to the admin session control in 2.3/2.8.

### 2.7 Transactional Emails
All templates below use one shared, reusable admin-controlled template system (not one-off emails):
- **Welcome email** — content fully controlled by Admin
- **Forgot password** — system defines the required technical fields (reset link/token, expiry); Admin customizes wording/branding
- **Order confirmation**
- **Order status change** (shipped / ready for pickup / delivered)
- **Payment received**
- **Low-stock alerts** (internal)
- **Return processed**
- **Promo / newsletter emails**
- **Login page** is a standard component of this flow.

### 2.8 User Activity Tracking & Session Monitoring [Added]
Applies to **every account type** — customers, staff, and admin alike.

**What's logged** (insert-only, transactional — see Part II naming conventions):
- Login and logout events (timestamp, IP address, device/user-agent, success or failure)
- Session start/end, including timeouts (idle timeout per 2.3) and forced terminations
- Page views (which pages, when, by which account or anonymous session)
- Cart activity: items added/removed, cart abandoned, cart converted to order
- Failed login attempts and any rate-limit/lockout events (per 2.4)
- IP address captured on every logged event, not just login
- **[Added] "Login As" impersonation sessions** (Section 2.2.2) — logged distinctly from normal sessions, tagged with both the impersonator and the target account, so reporting never conflates a user's own activity with actions taken on their behalf during impersonation.

**Admin-facing dashboards built on this data:**
- **Currently logged-in users** — real-time view of active sessions across customers and staff, with the ability to drill into and force-terminate any one of them.
- **Login/session history** per user — full timeline of logins, logouts, timeouts, and devices used.
- **Suspicious activity view** — repeated failed logins, logins from unusual IPs/locations, or rapid session churn, surfaced for review.
- **Cart and browsing behavior dashboards** — most-viewed products/pages, cart abandonment rates, and drop-off points in the funnel.
- All of the above are exportable, per the reporting rule in 2.2.

**Retention:** this activity log follows the same "nothing is truly deleted" principle as the rest of the system (Part II) — historical activity records persist even if the underlying account is later deactivated, remaining as immutable references.

---

## 3. Site-Wide Layout

### 3.1 Header (top bar)
- Logo
- Site-wide search bar (searches whole catalog/site, redirects to relevant results/pages) — must account for Arabic/English text normalization
- Shopping cart icon/summary
- Main categories displayed across the top of every page
- Language switch (AR/EN) and currency switch (JOD/USD)
- Login/account icon with quick access to order tracking, persistently visible

### 3.2 Footer
Admin-customizable block containing, at minimum:
- Short "About Us" description
- Facebook, Instagram links
- **[Added — legacy parity]** Live **embedded Facebook Page feed** (iframe/widget of the store's Facebook page), not just a link out.
- **[Added — legacy parity]** **Facebook Messenger** deep-link contact icon, alongside WhatsApp.
- Phone number, email, WhatsApp
- Newsletter signup box (if not already covered at registration)

### 3.3 Site-Wide Announcement Bar [Added — legacy parity]
Distinct from the homepage-only announcements in Section 4, this is a **persistent bar that renders above the header on every single page** (not just the homepage) — the mechanism used, for example, to display a "delivery temporarily suspended" style notice storewide.
- Admin-managed: message text (AR/EN), start-showing date, end-showing date, and an on/off toggle for immediate use without scheduling.
- Supports zero, one, or more than one active bar at a time (Admin decides stacking/priority if multiple are active).
- Rendered consistently across desktop and mobile.

---

## 4. Homepage

Fully admin-managed, shopping-site style:
- Upload photos/videos/banners
- Feature specific products ("our picks for you")
- Feature specific images/promo blocks
- Show discounted items
- Add custom sections
- Display categories/subcategories with their items
- Post announcements
- Section ordering/drag-and-drop (Admin rearranges without a developer)
- Scheduling for banners/sections (e.g. auto-activate/expire on set dates — Christmas promo, etc.)
- **[Decision] Branch closures/holidays:** On top of standing operating hours (Section 6), special closures are handled as homepage **announcements**, configured in the admin panel with a defined start-showing date and an end date.
- **[Added — legacy parity] Built-in curated carousel section types:** in addition to fully custom sections, the homepage section library includes ready-made, **auto-populating** carousel types Admin can drop in without manually curating each one:
  - **New Arrivals** (most recently added products)
  - **Discounted Prices** (currently active discounts)
  - **Best Sellers** (by purchase count)
  - **Most Viewed** (by view count)
  - Any of these can also be scoped to a specific category (e.g. a "Best Sellers — Statues" carousel), matching the old site's category-specific carousel tabs.
- **[Added — legacy parity] Publisher/Manufacturer carousel:** a homepage section showing publisher/manufacturer logos, each linking to that publisher's filtered product listing (see Section 5.6).

---

## 5. Catalog: Categories & Products

### 5.1 Category Structure
- Unlimited depth: main category → sub → sub-sub → …
- An item can belong to **multiple** categories at any level simultaneously (e.g., MainCat1 + SubCat3-of-MainCat2).
- Each category level has: name (AR/EN) + image.
- Category page shows:
  - Direct child categories in one section
  - Items belonging to it in another section
  - Grid view (default) or list view toggle
  - Sort and filter controls (price, availability, popularity, etc.)
- **[Decision] Category deletion with products assigned:** Deletion is blocked until every product in that category is reassigned to another category — no cascade-delete of products, no orphaned items.

### 5.2 Product Data
**On upload:** one main image (used for listings/thumbnails) + unlimited internal/gallery images.
- **[Added — legacy parity] Image lightbox/zoom:** the product gallery opens into a full-size lightbox/zoom view with a thumbnail strip for navigating between images, on both desktop and mobile.

**Migrated items** (from old system, incomplete history) additionally get:
- Quantity, cost per unit, selling price per unit
- Current location (branch/storage)
- View count (auto-increments going forward from launch)
- Purchase count (auto-increments going forward from launch)

**Standard fields:** Name, Description, ISBN (books), tags/keywords (search), dimensions (L/W/H), weight, manufacturer/publisher, size (XS–XXL), color, etc.
- Admin can define **custom attributes** beyond the defaults (e.g. "Shelf Number"), as free text or dropdown (editable after creation), each with visibility set to **public** or **admin-only**.
- Items can be manually set **visible/invisible** to customers (e.g. tied to stock status).

### 5.3 What Customers See vs. Don't
- Item details shown unless explicitly hidden.
- **Never shown publicly:** cost, exact quantity.
- **Always shown:** availability status.
- Which branches currently stock the item, displayed next to availability.
- If in central storage or unassigned to any branch: shown as **"Available — pickup/shipping arranged on order."**
- **[Added — legacy parity] Public view count & purchase count:** in addition to being internal metrics, Admin can toggle these on **per product page** as customer-facing social-proof numbers (e.g. "14,446 views," "184 purchases") — visible/hidden storewide by default, with per-item override.

### 5.4 Variants / Sub-Items
- A single item can have multiple variant combinations (e.g., bracelet: black qty 5, blue qty 3). Each specification combination tracks its **own quantity** independently; stock checks/holds happen at the **variant** level, not the parent item level.
- Variant-level SKU/barcode support for physical checkout/inventory counts in-branch — scanning a barcode redirects staff to the specific item/variant.
  - **[Decision] Barcode → consigned or storage-only items:** Because a scanned barcode could match a consigned item or one sitting in central storage with no branch, the **stock pool it should pull from must be explicitly specified by the Admin** per item (or per invoice — see 11).
- **[Decision] Variant explosion / validation:** Color × size × material combinations must be handled with proper validation (no unbounded SKU explosion left unchecked) — the system generates and manages the combination matrix rather than requiring free-form manual entry per combination.
- **[Decision] Variant-level images:** A bracelet in black and blue is still one item, differentiated by specification, not two products. Structure: one **main photo for the item** + an optional **main photo per sub-item/variant**, each with its own optional **gallery**.
- **[Decision] All variants at zero stock:** The parent item automatically shows as **unavailable** rather than landing customers on a page with dead variant options.
- **[Decision] Zero-stock costing:** If stock is at zero, the item isn't available at all — so the cost-average division-by-zero case (Section 11) never surfaces on the customer side.

### 5.5 Pricing & Discounts
- Discount by percentage **or** fixed discounted price.
- Shown to customer as strikethrough original price + new price + discount %.
- Time-boxed discounts (start/end date).
- Both **item-level** and **category-wide** discounts supported (e.g., "20% off all Bibles this week").
- **[Decision] Overlapping discounts:** When an item belongs to two categories that each have an active promotion (e.g., "Bibles -20%" and "Christmas -15%"), the precedence rule (additive / max-only / first-match) is **configurable per product, by the Admin** — not a single global rule.

### 5.6 Publisher / Manufacturer Pages [Added — legacy parity]
- Publisher/manufacturer (already a product field, Section 5.2) gets its own **filtered landing page** — a pre-built listing of every product tagged to that publisher, reachable from the product page's publisher link and from the homepage publisher carousel (Section 4).
- Admin manages the list of publishers/manufacturers (name AR/EN + logo image) from the catalog module.

---

## 6. Branches

- Admin can add branches with: name, phone number, latitude/longitude (rendered on an embedded map), and a button linking out to Google Maps directions.
- Branch-level operating hours.
- Special closures/holidays handled via homepage announcements — see 4.

---

## 7. Consignment (Sale on Behalf of Third Parties)

Two directions, both need a full tracking pipeline:

**A. We give our items to someone else to sell** (e.g., at a bazaar):
- At original price or a set price.
- Revenue split configurable **per item** (e.g., first item: 85% us / 15% them) or **overall** (e.g., 90/10 across everything they're holding).
- Reflected against the giving party's holding record and against our inventory.

**B. We receive someone else's items to sell in our store** — same price/percentage split logic, in reverse.

### Requirements
- Dedicated monitoring/reporting pages per consignment arrangement (who holds what, what's sold, what's owed to whom).
- A **settlement/payout workflow** to periodically reconcile what's owed and mark it paid, tied into the money-box system (Section 10).
- Consigned stock is excluded from "our" sellable inventory count unless it physically sits in our branch, to avoid double-counting stock we don't own.
- Consigned stock is also excluded from the **cost-average pool** (Section 11), since it isn't owned inventory.
- Option to **"return to consignor"** for unsold items.
- **[Decision] Consigned item returned by a customer:** Goes back into the appropriate party's holding (ours or the consignor's) and the **revenue split reverses accordingly** — the original split is unwound, not just the stock movement.
- **[Decision] Partial recall:** A consignor may request some (not all) unsold units back before settlement — handled the same way as a full return-to-consignor, just at partial quantity.
- **[Decision] Discount applied to a consigned item at sale time:** Whether the revenue-split percentage applies to the **discounted price** or the **original listed price** is a choice left to the **Admin**, configurable per arrangement.
- **[Decision] Consigned items + storewide promocodes:** Whether consigned items are eligible for storewide promocodes is **specified by the Admin**, and if eligible, the discount routes through the revenue-split logic per the point above.
- **[Decision] Damage/loss in custody:** Items damaged or lost while in our custody are marked **damaged/lost** and excluded from all sales/inventory reporting going forward — except that their cost remains included in the item's historical cost calculation (it is not simply erased).

---

## 8. Shopping Cart & Checkout

- Cart shows items, chosen specifications/variants, and a promocode field.
- Fulfillment choice: **pickup by hand** or **shipped**, with shipping address (country, governorate/province/state, zip, P.O. box) if shipping.
- **Login required to check out.** **[Decision] Guest checkout is not offered** — this resolves the earlier contradiction between Sections 8 and 14; guest *browsing* may still be allowed, but final checkout always requires a logged-in, verified account.
- Final stock/quantity re-validation happens right **before** checkout completes.
- **[Decision] Stock reservation locking:** Because two customers could otherwise both pass validation for the last unit within the same window, stock must be **locked** (proper reservation/locking strategy) at the moment of checkout validation, not just re-checked — closing the race condition.
- On checkout, ordered quantities go **on hold** (reserved, not yet deducted from sellable stock) until the order is fully delivered/handed over, at which point they're fully deducted.
- **[Decision] Unclaimed pickup holds:** No automatic timeout/expiry on held stock for "pickup by hand" orders. If pickup is taking too long, **JEC Store staff manually contact the customer or cancel the order** — this stays a human-driven process, not an automated expiry.
- Order notes/special instructions field at checkout.
- Order cancellation may be initiated by either an Admin or the customer.
  - **[Decision] Cancellation after payment:** This needs its own money-box reversal path, distinct from a post-delivery return (Section 12). On cancellation-after-payment, the system **prompts the Admin** to specify where the refunded money goes (cash, CliQ, رصيد, etc.).
- **[Decision] Partial/deposit payments:** Allowed — not restricted to full payment split across channels. Any partial/deposit balance is reflected against the customer's رصيد.

---

## 9. Order Management (Admin Side)

- Orders land on an admin dashboard: view, prepare, adjust.
- Admin can apply, at time of fulfillment:
  - Per-item discount (percentage or one-off price for that sale)
  - Whole-invoice discount (percentage or flat amount)
- Payment status: Paid / Not Paid, with paying **channel(s)** — Admin defines available channels (Cash, Visa, CliQ, etc.); a single order can be **split across multiple channels**.
- Delivery/pickup status pipeline: *Ordered – To Be Taken By Hand*, *Ordered – For Delivery*, *On Route*, *Delivered*, *Complete*.
- Dashboards summarizing all of the above.
- Printable/emailable **invoice/receipt** generation per order, with **currency clearly specified**.
- Internal order notes visible only to staff, separate from customer-facing order notes.
- Shipping costs can be changed/specified on the order.
- Staff can modify an order's items (not just discount/payment) after placement but before fulfillment.
  - **[Decision]** Editing items on an already-placed order does **not** re-trigger stock hold recalculation on the customer side, and the customer is **not** required to reconfirm — placed orders stay unaffected from the customer's perspective.
- Tracks which employee prepared/packed a given order.
- **[Decision] Split/partial fulfillment:** Supported — some items can ship/be ready now while others are backordered; a single order can carry more than one fulfillment status across its line items.
- **[Decision] Mixed fulfillment:** Supported — some items in the same order picked up by hand, others shipped, tracked independently per line item/group.

---

## 10. Payments & Money Boxes

- Every money movement (in or out) records: which money box, which channel, and what it was for/where it came from. A single transaction can **split across multiple money boxes**.
- Money boxes are created by Admin with an initial balance set by Admin; each box keeps its own transaction history and current balance/status.
- Money box **reconciliation** view: expected (system-calculated) balance vs. actual counted balance, for periodic cash audits.

---

## 11. Inventory, Costing & Shipments

- **Item cost formula:**
  `(sum of cost of all historical + currently available units) − (cost of already-sold units) ÷ (number of currently available units)`
  - **[Decision] Division-by-zero:** Resolved by the rule in 5.4 — an item at zero stock is never shown as available, so this formula is never evaluated against a zero denominator on the live site. (Historical reporting can still reference the last computed average as a fallback baseline for the next shipment.)
- At the moment of each sale, the system freezes and stores: the item's cost at that time, the price it sold for, and its listed/original price at that time — so historical margins stay accurate even if cost/price later changes.
- Per item, Admin sets **maximum**, **optimal**, and **minimum** stock levels; a dashboard surfaces what's most critically in need of restocking.
- **New shipments:** Admin inputs the invoice, the items it contains, and cost per item — updates inventory and links the item to all of its historical invoices. Invoices can be uploaded as image/PDF, or marked "no invoice available."
  - **[Decision] Stock-pool assignment:** For each shipment, the Admin specifies whether the **entire invoice** or **each item individually** is coming out of a given storage pool (branch, central storage, etc.) — this is the same mechanism that resolves the barcode-scan ambiguity in 5.4.
  - **[Decision] Foreign-currency invoices:** Only JOD or USD accepted for shipment costing (see 1.1) — no other currency conversion needed.
- Each item gets its own dashboard showing all in/out movement.
- **[Added — legacy parity] Barcode label printing:** in addition to scanning existing barcodes (Section 5.4), the system can **generate and print barcode labels** for new stock that doesn't already have one — whether newly created items/variants or existing items receiving a barcode for the first time. Supports batch/bulk label printing for a full shipment at once.
- **Stock transfer between branches** is its own tracked movement type (not a sale, not a shipment-in).
- **[Decision] Write-off/damage/expiry movement type:** A dedicated movement type exists for shrinkage — damaged, lost, or expired stock — alongside sale, shipment-in, transfer, and return, so this stock has a defined home in the system rather than nowhere to go (consistent with the consignment damage/loss handling in Section 7).
- Low-stock and out-of-stock **alerting**, tied to the min/optimal/max thresholds.
- Periodic **stock take / physical count** workflow to reconcile system quantity vs. actual counted quantity, with a variance report.
- The system also stores the store's **ongoing operating costs**, and supports **full end-to-end financial statements** covering the entire store process (invoices, money boxes, channels, etc.) — not just item-level costing.

---

## 12. Returns

- Returns supported after purchase, with a **return reason** captured (e.g., damaged).
- Return effects flow through the rest of the system (inventory, sales figures).
- Admin specifies which money box the refund comes out of.
- Alternative to cash refund: convert to **رصيد** (store credit), usable as a payment method at future checkout.
- A return **approval/condition-check step** before refund/credit is issued (item must be inspected as returned-in-good-condition) — not every return auto-refunds.
- Partial returns (e.g., return 1 of 3 units ordered) supported at the variant/line-item level, not just whole-order.
- Consigned-item return handling and revenue-split reversal: see Section 7.

---

## 13. Promocodes

- Types: percentage off, percentage off **capped** at a max JD amount, or flat JD amount off.
- Standard controls: usage limits (total uses, uses-per-customer), minimum order value to qualify, expiry date/time, restriction to specific categories/items, and whether it stacks with existing item discounts.
- **[Decision] Single-use-globally vs. reusable-until-limit:** These are distinct settings from the per-customer cap, and both are **configurable by the Admin** directly in the promocode creation UI.
- Consigned-item eligibility: see Section 7.

---

## 14. Customer-Facing / Conversion

- Product reviews & ratings, with Admin moderation before publishing
- Wishlist / save-for-later
- "Related products" / "frequently bought together" on product pages
- Recently viewed items
- Guest **browsing** may be allowed, but see Section 8: guest **checkout is not** — login is always required to complete an order.
- **[Added — legacy parity] Product Compare tool:** an "Add to Compare" action available on every product card and product page; a persistent compare-list indicator (with live count) in the header; and a dedicated comparison page showing selected products' attributes side-by-side. Available to guests and logged-in customers alike (no login required to compare).

---

## 15. Search & Discovery

- Autocomplete/typo-tolerant search suggestions
- Filter by price range, attribute (color/size/etc.), and availability, in addition to category
- Arabic/English normalization applied in the site-wide search bar (Section 3.1)
- **[Added — legacy parity] Tag browsing pages:** each product's tags/keywords (Section 5.2) are clickable and lead to a dedicated tag results page listing every product sharing that tag — not just used as internal search index terms.
- **[Added — legacy parity] Items-per-page control:** category/listing pages expose a customer-facing "show: 25 / 50 / 75 / 100 / All" control, in addition to backend pagination.
- **[Added — legacy parity] Explicit sort options:** listing pages offer, at minimum — Default, Name (A–Z / Z–A), Price (Low–High / High–Low), and Newest — as named, selectable sort options (not just an unspecified "sort controls" placeholder).

---

## 16. Technical / Platform

- Mobile responsiveness is a hard requirement (most shoppers on WhatsApp-linked mobile browsers).
- SEO basics: sitemap, meta tags, clean URLs.
- **Bilingual URL slugs:** Arabic and English product names each generate their own slug.
  - **[Decision] Slug collisions / renames:** When AR/EN names would generate the same slug, or a name changes post-publish, the system falls back to a stable identifier (**item ID**) in the URL/slug structure and issues redirects (301-style) from old slugs, so links never break.
- RTL numeral formatting and price/date layout handled beyond basic text direction (see Section 1).

---

## 17. UI/UX, Visual Design & Look and Feel [Added]

**Design north star:** the rebuild should feel *recognizably* like شبيبة ستور to a returning customer — same spirit, same core navigation logic, same general color family — while looking and behaving like a **modern 2026 e-commerce site**, not a dated OpenCart/Journal2 template. Nothing about the redesign should feel jarring or unfamiliar to existing customers; it should feel like a polished, faster, better version of what they already know, not a different store.

### 17.1 Color Palette
- **Confirmed from live-site screenshots** (superseding the earlier maroon/gold guess made before screenshots were available — that assumption is now replaced with the actual palette below):
  - **Primary — Crimson Red** (≈ `#E63950`–`#DC3E54` range): used for the top utility bar, primary buttons ("اضافة للسلة" / Add to Cart), the active tab state, section header bars ("اخترنا لكم," "دور النشر," category titles), and the "New" launch/marketing banners.
  - **Secondary — Steel Blue** (≈ `#3E7CB1`–`#4482B8` range): used for the main mega-menu navigation bar, the cart-summary widget, search button, and stepper/quantity controls (+ / − buttons) on the product page.
  - **Footer — Dark Navy/Slate** (≈ `#2C3446`–`#313A4E` range): distinct dark section for the footer, giving visual separation from the light body without leaving the warm-toned family entirely.
  - **Neutrals:** white and light gray (≈ `#F5F5F5`) for body backgrounds and content cards — clean and simple rather than richly textured.
  - **Logo accent colors:** the logo itself carries a small multicolor accent (orange, yellow, and blue in the icon mark, red in the wordmark) — this can be a source for secondary/tertiary accent colors (e.g. badge variety, chart colors in admin dashboards) without overusing the primary red everywhere.
  - **Badges:** "New" badges currently render in the secondary blue, discount badges in red/white — this red/blue pairing for status badges is worth preserving as a recognizable pattern.
  - **Semantic colors:** the old site has no distinct success/warning/info color language beyond the red/blue badge pairing above — the rebuild should add proper, clearly distinct semantic colors (stock status, form validation, alerts) that don't clash with or get lost against the primary red/blue.
- **Modernization vs. the old site:** the palette itself (crimson red + steel blue + navy + neutrals) is already a reasonably strong, distinctive identity — the rebuild's job is to apply it **consistently and intentionally** as a proper design-token system (defined shades for hover/active/disabled states, consistent use across storefront and admin) rather than the flat, single-shade-per-element usage typical of the old Journal2 template. Buttons, active states, and badges should feel like one coherent system, not isolated colored boxes.
- **Deliverable:** the palette above should be finalized against the actual logo file and exact brand hex values (extracted directly from source assets rather than eyeballed off screenshots) before frontend build begins, then documented as named tokens (e.g. `primary-600` = buttons/active states, `secondary-500` = nav bar, `neutral-900` = footer).

### 17.2 Typography
- One Arabic-optimized typeface and one complementary Latin typeface, both legible at small sizes on mobile and properly weighted for RTL (avoid fonts that were designed Latin-first and only loosely support Arabic glyphs).
- A defined type scale (headings, body, captions, price text, badges) applied consistently site-wide and in the admin panel — not per-page ad hoc sizing, which was a weakness of the old Journal2 theme.
- Numerals: per the RTL requirement already in Section 1, decide once, site-wide, whether Arabic-Indic or Western numerals are used for prices/dates, and apply it consistently — the old site's numeral handling was never confirmed and shouldn't be left inconsistent in the rebuild.

### 17.3 Layout & Navigation — What Stays Familiar
Confirmed directly from live-site screenshots, to be preserved *functionally* in the rebuild:
- Crimson-red top utility bar (account/cart/checkout shortcuts + currency indicator), steel-blue mega-menu directly beneath it, white logo mark with the multicolor icon — this three-tier header structure is the site's most recognizable visual signature and should carry over.
- Tabbed homepage carousel pattern — "وصل حديثاً" (New Arrivals) / "الأكثر مبيعاً" (Best Sellers) / "اسعار مخفّضة" (Discounted) / "الأكثر مشاهدة" (Most Viewed), which lines up directly with the curated-carousel section types already specified in Section 4.
- Publisher/manufacturer logo strip ("دور النشر") near the bottom of the homepage — already specified in Section 4/5.6, now confirmed visually as a clean logo-grid pattern worth keeping.
- Dark navy footer with the same four content blocks (About Us, Facebook, My Account shortcuts, Customer Service/WhatsApp) — same footer *purpose*, modernized in execution (Section 3.2).
- Product card anatomy — image, badge (New/discount), name, price, red "Add to Cart" button — same scan pattern customers already know.
- Product detail page structure: breadcrumb → gallery with zoom → title/meta panel (type, availability, view/purchase counts, price) → quantity stepper with +/− → Add to Cart → description/track-listing detail below — this overall top-to-bottom flow stays intact, just visually refined.
- Split-screen login/register page (returning customer on one side, new account on the other) — a familiar, low-friction pattern worth keeping rather than replacing with a single combined form.

### 17.4 Layout & Navigation — What Modernizes
- **Real photography, not placeholders:** the single biggest visual upgrade. The audit found the majority of category and product thumbnails on the live site fall back to a blank placeholder — the rebuild treats a full photography/asset pass as a launch blocker for any category being promoted on the homepage, not an optional nice-to-have.
- **Card design:** the old card treatment (thin text links for Add to Cart/Wishlist/Compare) is replaced with clearly styled, tappable buttons/icons sized properly for touch — no more plain-text action links.
- **Registration form density:** the current registration page is one long, dense, undifferentiated stack of fields (personal info → address → password, all visually identical gray-bar sections). The rebuild should break this into a clearer multi-step or visually distinguished flow, especially since it now also branches into Individual vs. Company (Section 2.5) — the old single long form doesn't scale to that added complexity.
- **Sparse/empty layout states:** some category pages (e.g. "افلام وتراتيل") render as a nearly empty page with just two tiles and a lot of unused whitespace — the rebuild's grid and section components should adapt gracefully to low-inventory categories instead of leaving large dead space.
- **Whitespace and grid discipline:** a consistent spacing/grid system instead of the dense, tightly packed Journal2 default — more breathing room, especially on mobile.
- **Filter UX:** long checkbox lists (the old Author filter had ~70+ items in one unstyled block) are replaced with searchable/collapsible/autocomplete filter controls (already required functionally in Section 15, called out here as a visual/UX requirement too).
- **Micro-interactions:** subtle, modern touches — skeleton loading states (already required in Part II §5), smooth add-to-cart confirmation feedback, hover/tap states on cards — the kind of polish the old static Journal2 theme lacks entirely.
- **Consistent iconography:** one icon set/style used throughout (cart, wishlist, compare, account, WhatsApp, etc.) instead of the mixed default icon styling typical of stock OpenCart themes.

### 17.5 Mobile-First Execution
- Since most shoppers arrive via WhatsApp-linked mobile browsers (already noted in Section 1 and 16), the design should be built **mobile-first**, then scaled up to desktop — not the reverse. The old site's layout was captured/audited only in its desktop-rendered form, so this is a genuine gap to close, not just a refinement.
- Sticky/persistent cart and search access on mobile, given how central those are to the shopping flow.
- Tap targets, spacing, and filter/sort controls specifically validated on small screens, not just responsively resized desktop components.

### 17.6 Admin Panel Look & Feel
- The admin/staff-facing panel (Access Management, order management, inventory, money boxes, consignment, reports, etc.) gets its **own clean, modern, data-dense UI** — distinct from the storefront's warm/branded look, prioritizing clarity and speed for staff doing repetitive daily tasks over brand styling.
- Consistent use of tables, filters, status badges, and the same loading/success/error state handling required in Part II across every admin screen, so staff aren't relearning UI patterns module to module.

### 17.7 Accessibility & Consistency
- Sufficient color contrast between the maroon/gold palette and text/background combinations (especially important for price text, discount badges, and form validation states).
- Consistent, predictable interaction patterns site-wide — the same button style always means the same kind of action, the same badge color always means the same status, across storefront and admin alike.

### 17.8 Process Note
The color-palette gap flagged in the earlier version of this document is now largely closed — Section 17.1's palette is grounded in actual site screenshots rather than a guess. Before high-fidelity frontend build begins, still produce and get sign-off on: (1) exact brand hex values pulled from the logo source file (screenshots give close approximations, not pixel-perfect values), (2) a finalized type scale, (3) key page mockups (homepage, category listing, product detail, cart/checkout, registration, and one admin screen), and (4) a small component library (buttons, cards, badges, form inputs) — so the "familiar but modern" balance is agreed on visually before it's built, not discovered after.

---

# Part II — Technical & Data Engineering Standards

## 1. Database & Data Modeling

- **No JSON for tabular data.** Anything representable as rows/columns lives in relational tables, not JSON blobs.
- **Engine:** SQLite for now, with schema/query design kept portable to PostgreSQL (or another SQL engine) later. Avoid SQLite-only syntax where a portable alternative exists.
- **Single source of truth.** Any value derivable from existing data must not be stored redundantly — compute it (view, query, or application layer) instead of duplicating it.
- **All aggregation happens in SQL.** Queries do the aggregation/filtering/sorting themselves — never pull raw rows into Python to aggregate there.

### Table Naming Convention

| Prefix | Type | Rules |
|---|---|---|
| `TRX_` | Transactional | Insert-only. Rows are never updated or deleted. |
| `SCD_` | Slowly Changing Dimension | Never hard-deleted; rows are closed, not removed. Must include: `SCD_ACTIVE_FLAG`, `SCD_ACTIVE_FROM`, `SCD_ACTIVE_TO`, `SCD_CHANGED_BY`. |
| `LKP_` | Lookup / mapping table | Same SCD fields as above, prefixed `LKP_` to identify it as a lookup rather than a core dimension. |

### Column Naming Convention
- `snake_case` throughout.
- Fixed suffixes so purpose is guessable from the name alone: `_ID`, `_DT` (date/datetime), `_FLAG`, `_AMT`.
- Primary keys: `PK_...` — Foreign keys: `FK_<TABLENAME>_ID`.

### SCD Handling
- Every SCD/LKP table has a documented, reusable **"as of" query pattern** — not reinvented per query.
- Explicit about when a query needs:
  - **Active-only** (`SCD_ACTIVE_FLAG = 1`), vs.
  - **Point-in-time rollback** (`SCD_ACTIVE_FROM <= :as_of AND (SCD_ACTIVE_TO > :as_of OR SCD_ACTIVE_TO IS NULL)`)

### Timestamps
- All timestamps stored in **UTC**; converted to local time only at the display/presentation layer.

### Table Documentation
- Every table has a defined **grain** (what one row represents), documented alongside the schema.

### Consistency With Business Rules
- All "never delete" business rules (staff accounts, customer accounts, audit-log references — Part I, Section 2) are implemented via the `SCD_` pattern above: rows are closed/deactivated, never hard-deleted, and referenced immutably from transactional history.

---

## 2. Query & Performance

- No N+1 queries — always batch or join.
- Any endpoint returning more than N rows supports pagination.
- Explicit rule per case: view vs. materialized table vs. raw query — never default to raw queries out of convenience.
- Index every foreign key and every column used in `WHERE`/`ORDER BY` on large tables.

---

## 3. Migrations

- All schema changes go through migration files (Alembic-style) — never manual `ALTER TABLE` in production.
- Migrations are additive/non-destructive by default. Breaking changes must be explicitly flagged and called out.

---

## 4. API & Structure Conventions

- REST endpoint naming: plural resource names, versioned (e.g., `/v1/customers`).
- Consistent file/module and folder structure — must not drift session to session.

---

## 5. Application Layer

- **Error handling:** never fail silently; consistent error shape across the whole API.
- **Logging:** structured logs, not `print()` statements.
- **Secrets:** never hardcoded — `.env` only, with a committed `.env.example`.
- **Idempotency:** any write endpoint that might be retried (e.g., bulk messaging/Twilio sends) must be idempotent to prevent duplicates.
- **UI states:** every page must explicitly handle loading (skeletons or equivalent), success, and failed states — and any other state relevant to that page.

---

## 6. Cross-Cutting Non-Functional Requirements

- **Platform:** Website + mobile website, fully responsive.
- **Data retention:** Nothing is truly deleted — all assets are handled through SCD tables (close/deactivate, not remove).
- **Concurrency:** Race conditions must be carefully handled throughout (see stock-locking rule in Part I, Section 8).
- **Security:** Full security checks required — SQL injection prevention and equivalent protections must be fully implemented across the stack.
- **RTL:** Numeral formatting and price/date layout must be handled correctly for RTL contexts, beyond just text direction.
- **[Added] Activity logging as `TRX_` tables:** login/logout, page-view, cart-activity, and session-timeout events (Part I, Section 2.8) follow the same insert-only `TRX_` convention as every other transactional table — never updated or deleted, indexed on user ID, session ID, and timestamp for dashboard performance at scale.
- **[Added] Session store:** active sessions (for the "currently logged in" dashboard and forced-logout capability) need a fast-lookup store separate from the historical `TRX_` log — e.g. a sessions table keyed by session ID with a `SCD_ACTIVE_FLAG`-style current-state field, kept lean since it only tracks *live* sessions, while the full history lives in the `TRX_` log.
- **[Added] Rate limiting & CAPTCHA infra:** rate-limit counters (per account/IP, per endpoint) should not live in the primary relational store as hot-write rows — use an appropriately fast mechanism (e.g. in-memory/cache-backed counters) so abuse checks don't become a bottleneck or bloat the transactional tables.

---

## 7. Recommended Technology Stack [Added]

This is a recommendation to guide the build, not a hard requirement in the way the rest of this document is — but it's written to be consistent with everything already specified above (SCD/TRX conventions, maker-checker, session dashboards, bilingual SEO, RTL).

### 7.1 Backend — FastAPI (over Flask)
- Both frameworks support the SQLAlchemy + Alembic setup implied elsewhere in this document equally well, so the deciding factors are what FastAPI provides out of the box:
  - **Pydantic models double as enforced schemas at the API boundary** — a natural fit for the documented-grain/data-dictionary discipline already required per table (Part II §1), and FastAPI auto-generates OpenAPI docs from them, useful for a spec this large.
  - **Native async**, which matters specifically for the stock-reservation locking requirement (Part I §8) and for I/O-heavy work like WhatsApp/Twilio sends, PDF generation, and email dispatch — these can run non-blocking without extra plumbing.
  - **Built-in background tasks** for fire-and-forget work (low-stock alerts, abandoned-cart reminders, invoice generation) without a separate task-runner just for simple cases.

### 7.2 Storefront — Server-Rendered, Not a Separate SPA
- Given the bilingual SEO requirements in Part I §16 (sitemap, meta tags, clean/bilingual slugs), a client-rendered single-page app works against that goal unless paired with SSR tooling (e.g. Next.js) — which means maintaining a second language/ecosystem alongside the Python backend.
- **Recommended:** FastAPI + **Jinja2** templates (server-rendered HTML, SEO works by default), progressively enhanced with **HTMX** (add-to-cart without reloads, live filter/sort, currency/language toggle) and **Alpine.js** (lightweight interactivity — image lightbox, quantity stepper, mega-menu), styled with **Tailwind CSS** built directly on the design tokens defined in Part I §17.
- This keeps the stack to one primary language (Python) with a minimal, purpose-limited JS surface area — appropriate for a small team maintaining both the storefront and the admin panel.

### 7.3 Admin Panel — Same Stack First, React Only Where It Earns Its Keep
- SEO doesn't apply to the admin panel, so React/Vue would be a legitimate technical fit there (maker-checker approval queues, drag-and-drop homepage builder, the live "currently logged in" session dashboard, dense reporting tables) — but starting with the **same FastAPI + Jinja2 + HTMX + Tailwind** stack keeps velocity and consistency high while the bulk of the admin panel (CRUD-heavy screens: catalog, orders, branches, promocodes) is built.
- Reach for a dedicated **React SPA** (Vite + React + Tailwind + shadcn/ui + Recharts for analytics/dashboard charts) only for the specific screens that genuinely need it — e.g. the homepage drag-and-drop section builder (Part I §4) or the real-time session-monitoring dashboard (Part I §2.8) — rather than committing to two frontend stacks for the whole system upfront.

### 7.4 Supporting Infrastructure
- **Auth/sessions:** **server-side sessions backed by Redis**, not JWT — required to support forced logout of a specific session and the "currently logged in" admin dashboard (Part I §2.3, §2.8), which is far more natural with server-held session state than with stateless tokens.
- **Search:** the Arabic-normalized, typo-tolerant autocomplete search required in Part I §15 is a poor fit for plain SQL `LIKE` queries at catalog scale — **Meilisearch** or **Typesense** are recommended, both with solid Arabic-language and typo-tolerance support.
- **PDF generation:** for invoices/receipts with correct Arabic RTL layout (Part I §9, §17), **WeasyPrint** (HTML/CSS → PDF) is the most direct path and handles RTL natively, versus a lower-level PDF library that would require manual RTL handling.
- **Background jobs / scheduling:** **Celery + Redis** for job volume at production scale (email sends, alert checks, report generation), or **APScheduler** as a lighter-weight alternative if job volume stays modest — either integrates cleanly with FastAPI's async model.
- **Database:** the existing SQLite-now/PostgreSQL-later plan (Part II §1) should still include **developing against PostgreSQL locally before launch**, not just at migration time — SQLite's locking behavior differs meaningfully from PostgreSQL's, which matters directly for the stock-reservation race-condition handling in Part I §8.

### 7.5 Why Not [Alternative]
- **Flask:** a reasonable, familiar alternative (consistent with prior projects), but would require manually adding what FastAPI provides natively — request validation, async support, and auto-generated API docs — for a spec with this many transactional/validation-heavy modules.
- **Full React/Next.js storefront:** technically capable, but doubles the tech stack (Python backend + separate JS frontend ecosystem) for a small team, and the SEO requirement is better served by server-rendered HTML than by client-side rendering plus SSR tooling layered on top.
- **Django (as a full-stack alternative):** would also work and bakes in more "batteries" (admin panel, ORM) than FastAPI — but its ORM and conventions don't map as cleanly onto the custom `TRX_`/`SCD_`/`LKP_` naming and SCD-pattern discipline already defined in Part II §1, which is easier to implement cleanly with SQLAlchemy directly.
