# JEC Store Requirements Coverage

This file tracks implementation coverage against `JEC_Store_Requirements.md`.
It is deliberately strict: a data model alone is not marked complete when the
shopper, staff, admin, or background workflow is not built.

Status key:

- Complete: implemented as a working application path and covered by tests or a
  focused smoke check.
- Partial: important pieces exist, but at least one required workflow is missing.
- Model only: schema exists, but the user-facing/admin-facing workflow is not built.
- Not built: no meaningful implementation yet.

## Part I - Functional Requirements

## 1. Project Basics

Status: Built

Evidence: FastAPI/Jinja storefront exists; Arabic/English locale files exist;
language, currency, direction, money, date, and numeral helpers are implemented
in `app/core/i18n.py` and request context.

Remaining: Numerals follow the single site-wide NUMERAL_SYSTEM setting; confirm the choice with the client before launch.

## 2. Accounts & Access Management

Status: Built

Evidence: Role, user, profile, permission, grant, maker-checker, impersonation,
audit, session, and activity models exist. Login, logout, registration, forgot
password request, email verification, account home, orders, wishlist, newsletter,
and impersonation-end routes exist. `/admin/access` manages role/user grants,
approval modes, checker roles/users, and Login-As target scopes. `/admin/users`
creates staff, assigns roles, deactivates users with last-admin protection, and
starts scoped Login-As sessions. `/admin/sessions` shows active sessions, recent
login attempts, and force-terminate actions. `/admin/audit` filters immutable
audit entries and field-level changes.

Remaining: Per-user login-history detail, browsing/cart funnel dashboards,
CAPTCHA provider verification, Redis-backed rate counters, and SMTP sending are
not complete.

- **Closed 2026-08-28 (reporting):** §2.2 names reports for "sales, inventory,
  consignment, money boxes, promocodes, returns, staff activity". Three of those
  had no dataset at all. `promocodes`, `returns` and `staff_activity` are now
  built in `services/report_datasets.py` and served by the same export layer in
  CSV, Excel, PDF and print view. The staff-activity report reads the audit log
  rather than the activity stream — §2.2 asks who changed what, and the audit
  log is the immutable record of exactly that — and counts impersonated actions
  in their own column, because §2.2.2 forbids conflating them with the target's
  own work. `tests/test_exports.py` now walks `REPORT_KEYS` in both languages,
  so the next report added cannot be half-wired either.
- **Closed 2026-08-28 (exports where staff work):** the buttons appeared on
  `/admin/reports` only. They are now on Orders, Products, Inventory,
  Consignment, Money Boxes, Promocodes and Returns, and `/admin/reports` lists
  every report in the registry instead of the two that happened to be wired —
  five built reports had been reachable from no screen. The partial is gated on
  `reports.export` internally, so the seven call sites cannot each get the
  §2.2 permission split wrong. Pinned by `tests/test_admin_ux_contract.py`.

- **Verified 2026-08-15:** the password-change workflow and the customer
  active-session UI are complete, not outstanding as previously recorded here.
  Proven over HTTP against the dev database: changing a password from
  `/account/password` keeps the acting session alive, signs the same account out
  on a second device, rejects the old password (401) and accepts the new one
  (303) — §2.3's force-logout-on-password-change. `/account/sessions` lists the
  customer's own live sessions and `end-others` terminates the rest while
  leaving the caller signed in (§2.6).
- **Fixed 2026-08-15 (audit trail):** `/admin/money-boxes/{id}/close` submitted
  itself as `money_boxes.create_box`, so closing a box was gated by the *create*
  permission and written to the audit log as a *create* — the §2.2 log recorded
  the wrong verb. `close_box` is now its own registry entry, its own replayable
  action and its own permission; the legacy `operation="close_box"` parameter is
  still honoured so approval requests queued before the split replay correctly.
- **Fixed 2026-08-15 (access management):** `orders.set_line_status` — the §9
  delivery-status pipeline — was submitted and audited but was absent from
  `PERMISSION_REGISTRY`, so Admin could neither grant it nor set its approval
  mode on `/admin/access` (§2.2, §2.2.1). It is now registered, the two routes
  that submit it are gated on it, and the Store Employee baseline carries it so
  counter staff can still hand orders over. `tests/test_admin_actions.py` now
  fails the build on any route that submits an action it does not gate on, or
  that is missing from the registry or the replay handlers.

- **Added 2026-08-12:** admin shell at `/admin` with staff gate and per-action
  permission enforcement (`app/web/admin/deps.py`); maker-checker engine
  (`app/services/approvals.py`) with action registry, pending queue,
  approve/reject/withdraw and replay-from-stored-parameters; audit writer
  (`app/services/audit.py`). Proven by `tests/test_approvals.py` (16 tests).
- **Added 2026-08-12:** `services/access_admin.py`,
  `services/access_actions.py`, `/admin/access`, `/admin/users`,
  `/admin/sessions`, `/admin/audit`, and scoped Login-As start. Covered by
  `tests/test_access_admin.py` (5 tests) plus HTTP smoke.
- **Added 2026-08-12 (exports):** `services/exports.py` + `services/report_datasets.py`
  + `web/admin/exports.py`. One shared layer serves every report in CSV, Excel, PDF
  and a print view, gated on `reports.export` (separate from `reports.view`).
  Seven reports wired: financial statement, operating costs, sales, inventory,
  low stock, consignment, money boxes. Arabic-safe throughout — UTF-8 BOM for CSV,
  RTL sheet direction for Excel, RFC 5987 filenames, CSV-injection guarded.
  PDF uses WeasyPrint where its native libraries exist and degrades to the print
  view with an explicit 503 where they do not. Covered by `tests/test_exports.py`.
- **Added 2026-08-12 (email delivery):** `services/mailer.py` drains the outbox —
  SMTP transport with a console transport as the development default, at-most-once
  delivery, backoff between retries, abandonment after 5 attempts, and RFC 2047 /
  base64 encoding so an Arabic message is 7-bit clean on any server.
  `app/workers/` runs it on a schedule alongside low-stock alerts, cart abandonment
  and session pruning (`python -m app.workers.run [--once]`).
  Covered by `tests/test_mailer.py`.

## 3. Site-Wide Layout

Status: Built

Evidence: Header, footer, language/currency toggles, search, cart, compare,
account links, announcement bar partial, WhatsApp/Messenger/social links, and
Facebook page URL settings exist.

Remaining: the live embedded Facebook feed depends on configured content and
has no admin surface yet. (Footer and site-settings editing was recorded here as
not built, contradicting the note directly below it; corrected 2026-08-28 —
`/admin/content/settings` edits the footer and social links in both languages.)
- **Added 2026-08-12 (content admin):** `services/content_admin.py` + `web/admin/content.py` and `web/admin/branches.py`.
  Announcement bars (create, on/off toggle, priority stacking, scheduling) and
  the footer/site-settings editor with AR/EN pairs. Verified: a new bar renders on
  the storefront immediately.

## 4. Homepage

Status: Built

Evidence: Homepage sections are data-backed; seeded section types include banner,
category showcase, new arrivals, best sellers, discounted, most viewed, and
publisher carousel. Scheduling fields exist.

Remaining: None. Banner artwork per language is uploadable; real artwork is part of the §17.4 photography pass.

## 5. Catalog: Categories & Products

Status: Built

Evidence: Category tree, products, variants, publisher pages, tags, images,
attributes, discounts, reviews, visibility rules, availability views, slug URLs,
filters, sorting, pagination, and seeded demo catalog exist. `/admin/products`
lists/filters products, creates products, edits bilingual fields, slugs,
visibility, publisher, primary category, stock thresholds, base price through
Maker-Checker, manual variants, product discounts, and review moderation.
`/admin/categories` creates/edits/re-parents categories, rebuilds descendant
ancestor paths, blocks non-empty closure, and applies category discounts.
`services/catalog_admin.py` also implements publisher create/update. Covered by
`tests/test_catalog_promocode_admin.py` plus HTTP smoke.

Remaining: Variant option matrix UI, custom attribute UI, standalone
publisher CRUD, barcode generation from product admin, import/export, and full
searchable filters are not complete. (Tag CRUD was listed here and is now
built — see §15.)

- **Fixed 2026-08-28 (product list):** `active_products()` returned every active
  product with no limit, so the admin list grew a row per book forever — the
  bounded-query rule in Part II §2, broken on the screen most likely to hold a
  four-thousand-row catalog. It now pages, and returns the matching total
  alongside so the screen reports how many matched rather than how many fitted.
  The three pickers that were calling it for a `<select>` were repointed to a
  new `product_options()`; without that split, paging the list would have
  silently cut every product dropdown in the panel to one page of options.
- **Fixed 2026-08-28 (status badges):** the list rendered `published_dt` as a
  warning-toned "Published" badge beside Visible/Hidden, reading as a second
  gate staff had to satisfy. It is a release date that orders the New Arrivals
  carousel and nothing else, so it now renders as a date under the name. (Media upload and customer review
submission were outstanding here and are now built — see the notes below and
§14.)
- **Added 2026-08-12 (image upload):** `services/media.py` + `web/admin/uploads.py`.
  Product main image and unlimited gallery (optionally per variant, §5.4),
  category tiles, publisher logos and per-language homepage banners (§4).
  Uploads are decoded by Pillow before storage (a renamed script is refused),
  EXIF is stripped after orientation is applied (a phone photo carries GPS),
  oversized images are downscaled rather than rejected, filenames are content
  hashes so traversal and collisions are impossible, and thumb/card derivatives
  are generated for mobile (§17.5). Bilingual alt text per image (§1, §17.7).
  Removing an image closes its row (Part II §6) and reclaims only the file.
  Covered by `tests/test_media.py` (22 tests).
- **Note:** this unblocks the photography pass §17.4 calls a launch blocker; the
  real photographs and the real `JECStoreLogo.png` are still outstanding.

## 6. Branches

Status: Built

Evidence: Branch and operating-hours models exist; `/branches` page renders live
branches from the database.

Remaining: holiday/closure workflows and a richer branch-detail UI are not
built. (Admin branch management was recorded here as not built, contradicting
the note directly below it; corrected 2026-08-28 — `/admin/branches` creates and
edits branches with coordinates and a weekly schedule, and refuses to close one
that still holds stock.)
- **Added 2026-08-12 (content admin):** `services/content_admin.py` + `web/admin/content.py` and `web/admin/branches.py`.
  Branch CRUD with coordinates and the weekly opening schedule; closing a branch
  that still holds stock is refused.

## 7. Consignment (Sale on Behalf of Third Parties)

Status: Built

Evidence: Consignor, arrangement, item, sale, and settlement tables exist.
`services/consignment.py` handles inbound/outbound arrangements, sellable but
not owned inbound stock, owned but not sellable outbound stock, per-item split
overrides, discounted-price vs original-price split basis, partial recall,
damage/loss with costed write-off, settlement sign conventions, money-box
movement, and closing rules. Checkout tags consigned lines, hand-over records
the sale split, returns write negative split rows, and `/admin/consignment`
provides list, new-arrangement, holdings, item placement, recall/loss,
settlement, history, and close workflows. Covered by `tests/test_consignment.py`
(10 tests) plus related checkout/order/return/inventory tests.

Remaining: nothing outstanding for this section.

- **Closed 2026-08-28:** the `consignment` report was already in the shared
  export layer; it is now reachable from `/admin/consignment` itself as well as
  from the report directory, which is what §2.2's "exportable" asks for.

## 8. Shopping Cart & Checkout

Status: Built

Evidence: Cart models and routes exist for viewing and adding items. Cart prices
are calculated live from current discounts/rate. Guest browsing cart state is
cookie-backed, and checkout is intentionally disabled until locking exists.

Remaining: The PostgreSQL FOR UPDATE path in services/locking.py has only been exercised on SQLite; run test_concurrent_checkout_cannot_oversell against PostgreSQL before launch.

## 9. Order Management (Admin Side)

Status: Built

Evidence: Order, order line, payment, fulfillment, delivery-status, return, and
tracking models exist. `/orders/track` resolves public tracking by order/email or
signed-in user.

Remaining: PDF invoices render only where WeasyPrint's native libraries are installed; the print view covers it otherwise. Split/mixed fulfilment is representable and tested at the data layer, but the checkout form applies one method per order.

- **Fixed 2026-08-28 (order detail):** the screen printed
  `prepared_by_user_id` straight, so it read "Prepared by 4" with no way to turn
  4 into a colleague. It names the person now. The customer's email address —
  the one thing staff reach for when an order needs a phone call — was not on
  the page at all, and is. Handing over and cancelling both ask before they act,
  a status stepper shows where the order is in the pipeline, and §12's staff
  return is linked from here (see §12).
- **Fixed 2026-08-28 (invoices vs returns):** `balance_amt` was
  `order.total_amt - paid_amt`, and a refund writes a *negative* payment row.
  So a refund reduced what the customer had paid without reducing what they
  owed, and was counted twice: an order settled in full printed "Balance due"
  for exactly the sum that had just been handed back. The invoice now credits
  settled returns against the original — the ordered quantities and line prices
  are never rewritten, because §9's invoice has to keep showing what the
  customer agreed to — and shows the return, what came back, and whether the
  money went to a money box or to رصيد. Only REFUNDED returns are credited: §12
  gates the refund on the condition check, so crediting an uninspected request
  would promise something no member of staff has agreed to. Covered by
  `tests/test_invoice_returns.py`.
- **Fixed 2026-08-28 (refund channel):** both refund paths — a return and a
  cancellation — filed the negative payment row against the channel the
  customer had originally paid on. A refund converted to رصيد was therefore
  recorded, and printed, as "Cash": money that never left the till, and that
  the customer can only spend in this shop. Store-credit refunds now go to the
  store-credit channel.
- **Fixed 2026-08-28 (order list):** the list carried an order number and no
  customer, so answering "has this customer's order been prepared?" meant
  opening rows one at a time until one matched. The customer column is batched
  into the existing query. The dashboard's "awaiting a shipping quote" tile
  linked to `?shipping=pending` and the filter bar had no `shipping` control, so
  the first press of "Filter" silently widened the list back to every order
  while the narrower tile count stayed on screen; there is a control now, and
  every filter bar in the panel carries the parameters it does not own.

## 10. Payments & Money Boxes

Status: Built

Evidence: Payment channels, money boxes, money transactions, allocations,
reconciliations, exchange-rate history, and store-credit ledger models exist.
`services/money.py` handles transactions with multi-box allocations, computed
balances (never stored), order payments split across channels, store credit
grant/spend with overdraw protection, box reconciliation with optional balancing
transaction, operating-cost payments, and financial statements. `/admin/money-boxes`
provides list, create, close, detail ledger, manual split transaction, and
reconciliation workflows. Covered by `tests/test_order_management.py`,
`tests/test_returns.py`, `tests/test_consignment.py`, and
`tests/test_money_admin.py`.

Remaining: Operating-cost editing is create-and-close only; recurring costs are recorded but not auto-posted.

## 11. Inventory, Costing & Shipments

Status: Built

Evidence: Branches, stock pools, stock levels, movement ledger, shipments,
shipment lines, transfers, stock takes, and operating-cost models exist.

- **Added 2026-08-12:** `services/inventory.py` — weighted-average costing (the §11
  formula, incl. the zero-stock baseline), shipment intake with JOD/USD conversion
  and landed-cost apportionment, per-invoice or per-line stock pools, branch
  transfers with in-transit state, write-offs, stock takes with frozen system
  quantities + variance report, low-stock alerting, and `reconcile_stock()` proving
  the projection still equals the ledger. `services/barcodes.py` generates Code128
  SVG labels incl. bulk-per-shipment, and resolves scans to a variant.
  `web/admin/inventory.py` + 7 screens. Verified over HTTP: 11 @ 11.100 + 10 @ 5.000
  re-averaged to 8.195; stock take froze 21, counted 18, posted the -3 variance.
  Covered by `tests/test_inventory.py` (31 tests).
- **Added 2026-08-12:** operating costs can be recorded from `/admin/reports`,
  including recurring metadata and a linked money-box movement. `money.financial_statement()`
  reports sales, frozen COGS, operating costs, consignment payouts/collections,
  and money-box movements. Covered by `tests/test_money_admin.py`.
- **Remaining:** operating-cost edit/versioning, recurring auto-generation, and
  accounting exports.

## 12. Returns

Status: Built

Evidence: Return and return-line models include inspection, condition gate,
refund destination, restock flag, and effects-applied flag. Staff raise and
inspect returns under `/admin/returns`; customers raise their own from
`/account/returns`, scoped to their own delivered orders, and may withdraw a
request until staff begin inspecting it. Both paths produce the same record and
pass the same inspection gate — a customer cannot approve their own refund.
Covered by `tests/test_returns.py`, `tests/test_customer_returns.py` and
`tests/test_customer_returns_http.py`.

Remaining: a customer acknowledgement email on submission would be an
improvement; the existing RETURN_PROCESSED template covers the outcome
notification.

- **Closed 2026-08-28 (returns report):** wired into the shared export layer as
  the `returns` report key, and exportable from `/admin/returns` itself.
  Withdrawn requests are included and labelled rather than filtered out — a
  report that drops them makes the refusal rate look worse than it is, which is
  the reason §12 keeps WITHDRAWN distinct from REJECTED at all.
- **Fixed 2026-08-28 (staff-raised returns):** §12 lets staff raise a return at
  the counter, and `/admin/returns/new` was built to do it — but nothing in the
  panel linked to it, and it required `order_id` as a query parameter, so the
  only way in was to type the URL with an order id by hand, and following the
  screen's own language toggle answered 422. It is now linked from the Returns
  list and from each order, and reached without an order it asks which order the
  goods came from instead of erroring.

## 13. Promocodes

Status: Built

Evidence: Promocode, restriction, and redemption models exist; cart has a
promocode foreign key. `services/promocodes.py` validates active codes, all
three limit types, product/category inclusion and exclusion restrictions,
minimum order, stacking, consignment eligibility, and redemption/reversal
ledger counts. Checkout/cart integration applies codes. `/admin/promocodes`
creates, lists, updates, retires, and shows redemption counts for codes using
`content.manage_promocodes`; restriction edits close old rows and insert new
ones. Covered by checkout promocode tests, `tests/test_catalog_promocode_admin.py`,
and HTTP smoke.

Remaining: nothing outstanding for this section.

- **Closed 2026-08-28:** the `promocodes` report key is built and
  `/admin/promocodes` carries the export buttons. The report sums the
  insert-only redemption rows rather than reading a counter, so the signed
  reversal written when an order is cancelled is reflected — a code's reported
  cost is its true net cost. (This item was recorded as outstanding on
  2026-08-15 and is now closed.)

## 14. Customer-Facing / Conversion

Status: Built

Evidence: Wishlist, compare list, recently viewed model, newsletter preference
page, product counters, related products, product cards, and comparison page
exist. Signed-in customers submit ratings and reviews from the product page;
every submission lands PENDING and is invisible until a moderator publishes it
under `/admin/reviews`, with the pending count surfaced on the admin dashboard.
Ratings show as stars on the product page and on product cards, averaged over
approved reviews only. The verified-purchase badge is computed from the shop's
own order history, never taken from the form. Covered by `tests/test_reviews.py`
and `tests/test_reviews_http.py`.

Remaining: nothing outstanding for this section.

## 15. Search & Discovery

Status: Built

Evidence: SQL-backed bilingual search, suggestions, category sort/filter/page
size controls, slug normalization, publisher/tag pages, and visible product
queries exist.

Remaining: MeilisearchBackend is implemented behind the same protocol but has
not been run against a live server.

- **Closed 2026-08-28 (tags):** the model, the `/tag/{id}` landing page and the
  search index all read tags and always did; nothing wrote them, so `lkp_tag`
  shipped empty, the tag pages were unreachable, and the tag terms
  `search.build_index_text()` folds into every product's projection were always
  the empty set. `/admin/products/tags` now creates, renames and retires tags,
  and the product screen applies them. Retiring closes the product links
  alongside the tag — closing the tag alone would leave `/tag/{id}` returning
  404 while the product page still listed it. Applying tags reindexes the
  affected products, so the derived search column stays equal to a full rebuild.
  Covered by `tests/test_tag_admin.py` (11 tests), which asserts the join rather
  than the CRUD: creating a tag and applying it must change what the storefront
  and the index actually see.

## 16. Technical / Platform

Status: Built

Evidence: Server-rendered pages, responsive Tailwind layout, bilingual URLs,
structured errors. `/sitemap.xml` and `/robots.txt` are generated on request
from the same visibility rules the storefront uses, so an unpublished product
cannot leak through them; each entry carries `xhtml:link` alternates rather
than listing the Arabic and English slugs as separate URLs, and the document
splits into a `<sitemapindex>` past the protocol's 50,000-URL limit. Product,
category, publisher and tag pages 301 a retired slug to the canonical URL (the
id leads, per §16's own decision) and declare `rel="canonical"`. Covered by
`tests/test_sitemap.py`.

Remaining: a formal security review. Payment-gateway integration is out of scope
per the spec (channels are recorded, not processed).

## 17. UI/UX, Visual Design & Look and Feel [Added]

Status: Built

Evidence: Tailwind design tokens, Arabic/Latin typography, RTL logical
properties, recognizable header/footer/product-card structure, mobile grids, and
component styles exist.

Remaining: Real photography and the real JECStoreLogo.png are outstanding — §17.4 calls the photography pass a launch blocker. The upload path is ready for both.

- **2026-08-28 — admin UX pass (§17.6, §17.7).** §17.6 asks for "consistent use
  of tables, filters, status badges, and the same loading/success/error state
  handling across every admin screen, so staff aren't relearning UI patterns
  module to module". Each screen honoured that on its own and they disagreed
  with each other. What changed:

  - **One flash banner.** The markup was pasted into twenty templates; three had
    no Maker-Checker case, so those screens told a maker their *parked* action
    was "Saved" — §2.2.1's exact failure mode. It is one partial now, covering
    pending, saved, created, closed, sent and error.
  - **A title and a purpose line on every screen.** Most opened straight onto a
    filter bar. Each now says where you are, how to get back up, and — in one
    sentence — what the screen decides.
  - **Confirmation before anything irreversible.** The panel is append-only:
    closing a money box or retiring a promocode writes history rather than
    deleting a row, so there is no undo to fall back on and there was no
    double-check either. Nineteen forms now name their consequence before
    acting ("Close this money box? No further transactions can be recorded
    against it"), rather than asking "Are you sure?".
  - **Submit-once.** Every write is a form POST plus a redirect; on a slow
    connection in a branch, staff press again and post a second payment or a
    second stock movement. `static/js/admin.js` guards it with `aria-disabled`
    rather than `disabled`, because several screens post the pressed button's
    own name and value. Progressive enhancement: with the script blocked, every
    form behaves exactly as it did before.
  - **Empty states that distinguish "nothing yet" from "nothing matched"**, each
    offering the action that resolves it.
  - **Lists that say how much there is.** "3 / 9" says nothing about whether a
    filter narrowed anything; "Showing 41–60 of 173" does. Reads that are
    deliberately capped (inventory positions, the audit log) now say so instead
    of presenting a cut-off list as the whole of it.
  - **A panel-wide quick search** at `/admin/search`. An exact order number,
    barcode or SKU opens that record directly — scanning a label should open the
    item, not a page listing one result — and everything else lists hits grouped
    by kind, each group gated on the permission for the screen it links to.
  - **Navigation that reaches every screen.** Review moderation, stock takes,
    transfers, barcode labels and the tag screen were built, routed and linked
    from nowhere. The sidebar also carries counts for the queues that hold work,
    so "what needs me" is answerable without opening each module to find it
    empty.

  Pinned by `tests/test_admin_ux_contract.py`, which asserts these as
  cross-screen properties rather than per-screen assertions, and by
  `tests/test_admin_screens_http.py`, which renders every admin screen in both
  languages and follows every internal link it emits.

- **Fixed 2026-08-28 (silent style loss):** the new flash banner wrote
  `class="admin-flash admin-flash--{{ tone }}"`. Tailwind emits only class names
  it finds as whole strings — including `@layer components` classes — so none of
  the four tone modifiers were emitted and every banner rendered with no colour
  and no border. The markup was correct, the CSS was correct, and the two never
  met. `tests/test_styles.py` covered this for colour *utilities* only; it now
  covers the project's own component classes and rejects a modifier composed
  from a variable.

## Part II - Technical & Data Engineering Standards

## 1. Database & Data Modeling

Status: Complete for current schema

Evidence: SQLAlchemy base enforces SCD/TRX behavior, table grain documentation,
UTC timestamps, no JSON columns, naming conventions, and immutability. Convention
tests cover the live metadata.

Remaining: Future tables must keep passing the convention tests; current schema
does not imply the corresponding workflows are complete.

## 2. Query & Performance

Status: Built

Evidence: Current storefront read paths use bounded queries, joins, pagination,
batch pricing, and indexed foreign keys.

Remaining: checkout locking, export queries, and large-catalog performance
checks are not benchmarked.

- **Fixed 2026-08-28:** the admin product list was unbounded — every active
  product, every page load. It pages now (§5). Reads that stay deliberately
  capped rather than paged say so on screen, so a truncated list is no longer
  indistinguishable from a complete one.

## 3. Migrations

Status: Complete for current schema

Evidence: Alembic is configured and an initial migration exists.

Remaining: Future changes must use additive migrations; no downgrade/destructive
review process exists beyond developer discipline.

## 4. API & Structure Conventions

Status: Built

Evidence: File/module structure is consistent. FastAPI OpenAPI is enabled in
non-production.

Remaining: Versioned REST `/v1/*` resource API is not built. Current public
surface is mostly server-rendered HTML routes.

## 5. Application Layer

Status: Built

Evidence: App-wide error envelope, structured logging, `.env.example`,
idempotent email queueing with an SMTP worker draining the outbox
(`services/mailer.py`, `workers/`), and explicit template state panels exist.
Locale parity and built-stylesheet freshness are enforced by
`tests/test_locales.py` and `tests/test_styles.py` — both failure modes are
silent at runtime, so neither can be left to review.

Remaining: broader idempotency enforcement for write endpoints and production
secret checks are not complete. (The password-change workflow was listed here
and was verified complete on 2026-08-15 — see §2.)

- **2026-08-28:** loading/success/error states are now consistent across every
  admin screen rather than "across future pages" — one flash partial for the
  success and error states, and a submit-once busy state for the loading one.
  See §17.

## 6. Cross-Cutting Non-Functional Requirements

Status: Built

Evidence: Responsive layout, no-delete enforcement for current models, UTC
helpers, RTL helpers, activity logging models, live session store, and SQLite WAL
configuration exist.

Remaining: Redis, Meilisearch, WeasyPrint natives and PostgreSQL all need verifying on a Linux container; each has a working fallback until then.


## 7. Recommended Technology Stack [Added]

Status: Followed where current scope exists

Evidence: The project uses FastAPI, SQLAlchemy, Alembic, Jinja2, HTMX, Alpine,
Tailwind, Redis dependency, PostgreSQL driver dependency, and WeasyPrint
dependency.

Remaining: Redis is not wired as the live session/rate-limit backend, search
still defaults to SQL, WeasyPrint invoice generation is not built, background
jobs are not configured, and PostgreSQL local development has not been verified.
