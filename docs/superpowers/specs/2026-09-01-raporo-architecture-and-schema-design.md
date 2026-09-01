# Raporo — Architecture & Schema Design

Date: 2026-09-01 · Status: awaiting Elvis's review
Sources of truth this spec serializes: docs/PRODUCT.md (decisions), docs/PROJECT-DESCRIPTION.md (domain), ADR 0006 (stack), ADR 0007 (HTMX + service layer). Approved in chat: architecture Step 1 (4 points), schema Step 2 (D1/D2/D3 + mandatory-FX rule).

## 1. Goals and non-goals

Goals: replace manual WhatsApp daily reports for Rwandan multi-store businesses with recorded events and derived, always-consistent reports; investor cycles with exact igishoro/inyungu; boss-ready branded report output. Non-goals (v1): DRF API (service layer preserves it), social login, SMS OTP, offline entry, >5 stores, POS integrations, USD as base.

## 2. Architecture

Modular Django 6.1 monolith at repo root. Frontend = Django templates + HTMX fragments (ADR 0007); htmx.min.js vendored in `static/`, version-pinned. Session auth. PostgreSQL. Docker compose: `web`, `db` (redis/worker only when Celery arrives, slice 6).

Layout: `config/` (settings base/dev/prod, urls, asgi) · `common/` (abstract bases + Money helpers) · `apps/` = accounts, orgs, audit, catalog, inventory, sales, money, reporting, notifications · root `templates/` (base layout, header with language switcher + store picker) + per-app templates with `partials/` for HTMX fragments · `static/` · `locale/` (en, rw, fr) · `docker/`.

App dependency direction (imports only point left→right): accounts → orgs → catalog → inventory → sales → money; audit importable by all; reporting reads all, imported by none; notifications reads inventory/sales.

**Service-layer rule (load-bearing, ADR 0007):** every state change goes through a service function in `apps/<app>/services.py` (e.g. `record_sale(store, actor, items, payment, ...)`). Views parse input, call one service, render a template. Services own validation, transactions, audit writes, ledger maintenance. A future mobile API = DRF views over these same services. Business logic in a view fails code review (integration-engineer guards this as contract drift).

**HTMX conventions:** every fragment endpoint also answers a full-page GET (progressive fallback); fragments live in `templates/<app>/partials/`; POST responses return the updated fragment; errors render the form fragment with field errors (HTTP 422); `HX-Trigger` responses fire follow-up updates (e.g., stock badge after a sale).

## 3. Cross-cutting invariants (enforced in `common/`)

1. **Tenancy (invariant #1).** `Store.org` is the only org pointer; all business tables FK the store. `StoreScopedModel` (abstract): `store` FK + `ScopedManager` whose `for_store(store)` is the only query entry point — plain `.objects.all()` raises. Cross-store/org leak = Critical, release-blocking.
2. **Soft delete.** `SoftDeleteModel`: `deleted_at`, `deleted_by`; default manager filters live rows; `all_objects` for audits. No model exposes hard delete; admin delete is disabled.
3. **Audit.** `AuditedModel`: `created_at/by`, `updated_at/by`. Every service writes an `AuditLog` row (actor, verb, target, JSON diff).
4. **Ledger immutability.** Movement tables (StockMovement, Payment, CapitalEntry, Payout) are append-only: no UPDATE path in services; corrections are reversing rows referencing the original (`reverses` FK). DB trigger raises on UPDATE of frozen columns (belt and braces).
5. **Frozen facts + mandatory FX (Elvis, 2026-09-01).** `MoneyFields` mixin: `amount DECIMAL(14,2)`, `currency CHAR(3)` (default = store base), `exchange_rate DECIMAL(20,10) NULL`, `amount_base DECIMAL(14,2)`. Validation: currency == base → rate NULL, amount_base = amount; currency != base → **rate mandatory** (> 0), amount_base = amount × rate rounded to base precision (RWF = 0 decimals). The form detects foreign currency, requires the rate, previews the converted amount before submit; submission without rate is rejected server-side too. Line prices (SaleItem, OrderItem) are ALWAYS in store base currency — foreign currency enters only at Payment/CapitalEntry/Payout/Expense. Rates manual in v1.

## 4. Schema (by app)

Types abbreviated; all tables get SoftDelete + Audited unless noted. PKs are BigAuto; FKs `on_delete=PROTECT` (ledger integrity) unless noted.

### accounts

- **User** (AbstractBaseUser): `username` citext UNIQUE · `email` citext UNIQUE **required** (decided 2026-09-01: email is the password-reset channel for everyone, including phone-first users) · `phone` VARCHAR(15) UNIQUE (digits, country code, no `+`; validated E.164-without-plus) · `password` (argon2id) · `language` CHAR(2) in {en, rw, fr} · `is_active`. Login accepts any of the three identifiers via one auth backend; responses never reveal which part failed (no enumeration).
- **TwoFactor**: OneToOne(User) · `totp_secret` (encrypted at rest with app key) · `confirmed_at`. Login is two-stage from slice 1: stage 2 renders only when the row exists.
- **RecoveryCode**: FK User · `code_hash` · `used_at`. 10 per enablement.

### orgs

- **Organization**: `name` · `slug` UNIQUE · `logo` · `brand` JSONB (color/typography tokens) · `base_currency` CHAR(3) default RWF · `timezone` default Africa/Kigali.
- **Store**: FK Organization · `name`; UNIQUE(org, name). 1–5 stores enforced in `create_store` service under `SELECT … FOR UPDATE` on the org row.
- **Role**: FK Organization · `name` · `permissions` JSONB array of codes from the in-code catalog (e.g. `sale.record`, `sale.below_floor_override`, `stock.write_off`, `invite.create`, `report.generate`, `cycle.manage`, `member.manage`, `expense.record`) · `is_preset`. UNIQUE(org, name).
- **Membership**: FK User · FK Organization · FK Role · UNIQUE(user, org).
- **StoreAccess**: FK Membership · FK Store · UNIQUE(membership, store). Org-wide roles (Owner) get all stores via service, still materialized here (explicit > implicit).
- **Invite** (no soft-delete; revocation is its lifecycle): FK Organization · FK Role · `stores` M2M · `token_hash` (SHA-256 of a 256-bit URL token; raw token shown once) · `invited_name/contact` optional · `expires_at` (default 7 days) · `used_at`, `used_by` · `revoked_at/by`. Single-use enforced by UNIQUE partial index on used invites' token_hash + service check-and-set in one transaction.

### audit

- **AuditLog** (append-only, no soft-delete): FK Organization NULL · FK Store NULL · FK actor(User) NULL (system) · `action` slug · `target_type` VARCHAR · `target_id` BIGINT · `changes` JSONB · `ip` INET NULL · `at`. Index (org, at), (target_type, target_id).

### catalog (D1: store-scoped)

- **Product**: StoreScoped · `name` · `is_active` · UNIQUE(store, name) among live rows.
- **Variant**: FK Product · `name` (default variant auto-created, name = "default", hidden in UI when alone) · `reference_price` DECIMAL(14,2) (base currency) · `cycle` FK money.Cycle NULL (D3: cycle-owned variant — all its movements belong to that cycle) · UNIQUE(product, name) live. Floor price is DERIVED: `max(latest_cost, weighted avg per costing policy)` → v1 policy: **latest_cost** (Elvis's "most recent cost" instinct; pinned here). Selling below floor requires `sale.below_floor_override`.

### inventory

- **StockMovement** (append-only, StoreScoped): FK Variant · `type` in {RESTOCK, SALE, REFUND_IN, WRITE_OFF, ADJUST} · `quantity` DECIMAL(12,2) signed, CHECK ≠ 0 (sign must match type) · `unit_cost` DECIMAL(14,2) NULL (required for RESTOCK; base currency) · `expiry_date` NULL (RESTOCK only) · `reason` enum {DAMAGED, LOST, STOLEN, PERSONAL_USE, COUNT_CORRECTION} + `note` (required for WRITE_OFF/ADJUST) · FK `sale_item` NULL · FK `reverses` (self) NULL · actor · `at`. Index (store, variant, at), (store, at). Partial index on (expiry_date) WHERE expiry_date NOT NULL.
- **StockLevel**: UNIQUE(store, variant) · `quantity` · `avg_cost` · `latest_cost` · `low_stock_threshold` NULL. Updated in the same transaction as every movement (row-locked); nightly job recomputes from the ledger and alerts on drift (consistency self-check).

### sales (D2: store-scoped customers)

- **Customer**: StoreScoped · `name` · `phone` NULL · UNIQUE(store, name, phone) soft.
- **Sale**: StoreScoped · FK seller(Membership) · FK Customer NULL · `at` · `note`. `total` = SUM(items); `paid` = SUM(payments IN linked); `outstanding` = total − paid (debt when > 0). Index (store, at).
- **SaleItem**: FK Sale · FK Variant · `quantity` · `unit_price` (base currency; service enforces ≥ floor unless override permission; below-floor use is audited) · `unit_cost_snapshot` (frozen igishoro: variant's avg_cost — or exact batch cost for cycle variants — at sale moment) · creates the SALE StockMovement in-transaction.
- **Order**: StoreScoped · FK Customer · `status` {OPEN, DELIVERED, COMPLETED, CANCELLED} · `at`. Completed = payments cover agreed total (the ✅). Delivery creates SALE movements for linked variants.
- **OrderItem**: FK Order · FK Variant NULL · `name` (free text for custom work; required if variant NULL) · `agreed_price` · `quantity`.
- **Payment** (append-only, StoreScoped, MoneyFields): `direction` {IN, OUT} · `method` {CASH, MOMO, BANK, OTHER} · exactly-one-of FKs: `sale` / `order` / `customer` (general debt payment) / `restock_movement` / `expense` / `payout` — enforced by CHECK (num_nonnulls(...) = 1) · FK `reverses` NULL · actor · `at`. Index (store, at), (direction, method).

### money

- **Expense**: StoreScoped, MoneyFields via its Payment(OUT) · `category` {RENT, TRANSPORT, SUPPLIES, SALARY, OTHER} + `note` · `at`.
- **Investor**: FK Organization · `name` · `phone/email` NULL · FK linked_user(User) NULL (read-only portal later).
- **Cycle**: StoreScoped · `name` · `status` {ACTIVE, CLOSED} · `operator_share_pct` DECIMAL(5,2) · `opened_at`, `closed_at`. Computeds: revenue, COGS (exact, from cycle-variant movements), inyungu, capital in, capital resting (remaining qty × batch cost), ROI, duration.
- **CycleInvestor**: FK Cycle · FK Investor · `share_pct` DECIMAL(5,2) · UNIQUE(cycle, investor) · service enforces Σ share_pct = 100 per cycle.
- **CapitalEntry** (append-only, MoneyFields): FK Investor · FK Cycle · `kind` {INITIAL, TOP_UP} · actor · `at`.
- **Payout** (append-only, MoneyFields): FK Cycle · FK Investor · actor · `at` · paired Payment(OUT).

### reporting

- **GeneratedReport**: FK Organization · FK Store NULL (NULL = consolidated) · `period_type` {DAY, WEEK, BIWEEK, MONTH, CUSTOM} · `range_start`, `range_end` (org-timezone dates) · `language` · `file` (PDF) + `image` (share card) · FK generated_by · `at`. No soft-delete (it's history).

### notifications (slice 6, sketch)

- **AlertEvent**: StoreScoped · FK Variant NULL · `kind` {LOW_STOCK, EXPIRY} · `payload` JSONB · `seen_at`, `resolved_at`.

## 5. Auth flows

- **Register** → creates User + Organization + first Store + Owner membership (one transaction). Phone mandatory (country code, no `+`), language captured, email optional.
- **Login**: identifier (username|email|phone) + password → constant-time lookup across the three, argon2id verify, uniform error. Rate limit: per-identifier and per-IP counters (cache), lockout with backoff; all attempts audited. Stage 2 = TOTP (or recovery code) when TwoFactor confirmed. Session cookie: Secure, HttpOnly, SameSite=Lax; rotation on login; CSRF everywhere.
- **Invite accept**: raw token → hash lookup; valid = not used, not revoked, not expired; new or existing user binds Membership + StoreAccess in the invite's transaction; token consumed atomically.
- **Password reset** (2026-09-01): everyone resets via an emailed link — single-use, expiring (1h), non-enumerating (the response is identical whether the email exists or not). Email is therefore required at registration. Dev: console email backend; prod: SMTP via env vars.

## 6. Period engine (reporting)

All timestamps stored UTC. Boundaries computed in `Organization.timezone`: DAY = local midnight→midnight; WEEK = Monday-start; BIWEEK = 1st–15th and 16th–month-end (confirmed 2026-09-01); MONTH = calendar; CUSTOM = inclusive local dates. One module `reporting/periods.py` owns this math; property-tested (DST-free zone today, but the code never assumes it). Data-reporting-engineer gates any change here.

## 7. i18n

Django translation only. EN default; RW + FR complete before any feature ships (localization gate). Per-user `language` persisted; header switcher (works pre-login via session). Reports render in the org's chosen report language. All strings via `gettext`; no concatenation; pseudo-locale test in CI catches unwrapped strings.

## 8. Error handling & UX states

Every screen defines empty/loading/error/denied/success (ux-designer spec). Service errors are typed exceptions → views map to fragment re-renders with field errors (422) or page-level messages; never a bare 500 to a user (custom 404/500 pages). Denials (permission, floor-price, store limit, invariant #1) are explicit, translated messages — and each has a denial test.

## 9. Testing strategy (qa-engineer owns)

- Unit: services (happy, boundary, denial); period math property tests; money/FX validation table tests.
- Ledger integrity: for random operation sequences, StockLevel == Σ movements (property test).
- Isolation: fixture with 2 orgs × 2 stores; every listing/report endpoint asserted to never return the other tenant's rows (invariant #1 suite — release gate).
- E2E (playwright-cli): register → invite → restock → sell (incl. USD payment with rate) → daily report renders correct totals; cycle happy path mirroring the Silver Rice numbers exactly (350,000 / 340,500 / 280,000 / 60,500 / 2 sacks).

## 10. Open items deliberately deferred to their slice

Report rendering tech (HTML→PDF/image) — ADR during slice 4 · alert delivery mechanics — slice 6 · Celery/Redis entry — slice 6 · costing policy revisit if a client disputes latest-cost floors.

## Self-review (done before handing to Elvis)

Placeholders: none. Contradictions: checked — FX rule consistent across §3.5/§4 (line prices base-only); D1–D3 reflected in catalog/sales/money; 2FA staged flow consistent between §4 accounts and §5. Scope: this spec covers architecture + schema + flows; per-slice implementation plans come next and stay separate. Ambiguities resolved by naming them: floor = latest_cost (v1 policy, revisit trigger stated), password reset via emailed link for all users (email required at registration — decided 2026-09-01).
