# Raporo — Architecture

**State of this document:** written 2026-09-03 against the tip of `feat/slice-1-foundation`,
with every "is built" claim verified by execution against the running stack (see
[§9 Verification log](#9-verification-log)). It consolidates five design specs and eleven
ADRs so that a new engineer reads one document instead of 5,400 lines.

**Read [ARCHITECTURE-ESSENTIALS.md](ARCHITECTURE-ESSENTIALS.md) first** if you are about to
write code. It is the index of load-bearing decisions. This document is the reference behind it.

**The one convention that matters here.** This project's most expensive recurring defect is a
control that is documented and does not execute — four such controls shipped in slice 1, and
the commit message of `9a697c3` claims "RLS scaffolding" that does not exist. So every
capability below is marked:

| Marker | Meaning |
| --- | --- |
| **BUILT** | in the source and in the live database today; verified by execution |
| **DESIGNED** | decided and specified; **no code exists** — do not rely on it |
| **OPEN** | not decided; named here so it is not discovered later |

Nothing in this document is written in the present tense unless it executes.

---

## 1. Stack and versions

Measured on the running stack, 2026-09-03 (§9):

| Component | Version | Notes |
| --- | --- | --- |
| Python | 3.14.7 | `uuid.uuid7()` is in the standard library here — that is why `public_id` needs no dependency |
| Django | 6.1 | Non-negotiable per [ADR 0006](adr/0006-stack-django-postgres-react.md). Every package must support it |
| PostgreSQL | 18.0006 | `supports_uuid7_function = True`, `supports_virtual_generated_columns = True` |
| psycopg | 3.3.5 (`[binary]`) | **not** `[pool]` — the pool extra is not installed |
| Pillow | 12.3.0 | decodes uploaded logos to verify they really are PNG/JPEG/WebP |
| argon2-cffi | 25.1.0 | `PASSWORD_HASHERS` is Argon2 only |
| pyotp / cryptography | 2.10.0 / 50.0.1 | installed for slice-1 2FA; no 2FA model exists yet |
| pytest / pytest-django / ruff | 9.1.1 / 4.14.0 / 0.16.5 | `ruff.toml` targets `py313`, line length 100 |

Frontend: **Django templates + HTMX** — server-rendered pages, HTMX fragment swaps, no JS
framework, no Node build ([ADR 0007](adr/0007-frontend-django-templates-htmx.md)).

> **Filename warning.** [ADR 0006](adr/0006-stack-django-postgres-react.md) is titled
> "…django-postgres-**react**". Its frontend half is **superseded** by ADR 0007. Any React,
> SPA, DRF-day-one or separate-frontend-service reference anywhere in this repo is stale.
> DRF returns only when a real API consumer (mobile app, third-party integration) exists.

Redis and Celery are deliberately **not** installed. They arrive in slice 6, when scheduled
report sending creates a real need. Docker compose runs `web` + `db` only.

---

## 2. Module layout

```
config/     settings (base/dev/prod/test), urls, wsgi, asgi.  Wiring only, no domain logic.
common/     the cross-cutting invariants: abstract model bases, the query-layer scope guard,
            system checks, versioned+pinned SQL for migrations, shared validators.
            An installed app with ZERO concrete models, so it owns no migrations.
apps/       one Django app per bounded context. Today: accounts, orgs, audit.
tests/      the whole suite, at the repo root — not per app. Plus `tests/testapp`, a
            throwaway app installed ONLY by config.settings.test.
```

### The rule that decides where a new thing goes

Ask **"what enforces this?"**, not "what is it about?".

1. Does it constrain a **class** of models that a future model can join without noticing?
   → `common/`. Bases in `common/models.py`, query behaviour in `common/managers.py`, the
   startup refusal in `common/checks.py`, migration SQL in `common/db.py`.
2. Is it a fact about **one** bounded context's data or behaviour? → `apps/<context>/`.
3. Is it environment wiring or a route? → `config/`.
4. Is it a test? → `tests/`, named for the behaviour, never for the module.

`common` has no concrete models on purpose. It is installed so `common/checks.py` runs; if it
gained a concrete model it would gain migrations, and the abstract bases every app inherits
would then have a migration graph of their own to sequence against.

### The service layer ([ADR 0007](adr/0007-frontend-django-templates-htmx.md))

**A service is the only place a state change happens.** `apps/<app>/services/<topic>.py`, a
plain function taking the actor and domain objects and returning a domain result:

```
create_store(org, actor, name=...) -> Store
record_sale(store, actor, items, payment, ...) -> Sale
```

A service owns: input validation, the transaction boundary, permission checks, the audit
write, and any ledger maintenance the change implies.

A service may **not**:

- take an `HttpRequest`, a form, or a `QueryDict` — it takes resolved domain objects, so a
  future DRF endpoint calls the same function unchanged;
- render, redirect, or return an `HttpResponse`;
- raise `PermissionDenied` for a store the actor may not reach — see the 404 rule in §4.11;
- be bypassed. A view that writes a model directly fails code review.

A **view** parses input, calls exactly one service, and renders a template. That thinness is
the entire reason a mobile API later is weeks of work rather than a rewrite.

**Status: DESIGNED.** No `services/` package exists in `apps/orgs` or `apps/accounts` today.
The one service-shaped module that exists is `apps/audit/services.py` (`record()`), and it
is the pattern to copy: a boundary that validates, redacts and refuses before writing.

### Dependency direction

Imports point one way only (original architecture spec §2):

```
accounts -> orgs -> catalog -> inventory -> sales -> money
audit         importable by all, imports none of them
reporting     reads all, imported by none
notifications reads inventory + sales
```

Inside `common/`, the direction is `tenancy <- managers <- models <- checks`. `common/models.py`
imports from `common/managers.py`; `common/checks.py` imports from both. `common/tenancy.py`
(DESIGNED) must import nothing else from `common/`, which is what keeps that chain acyclic.

---

## 3. The data model

### The seven tables that exist today

All seven carry `public_id` (UUIDv7). Verified: 16 tables in `public`, of which seven are
first-party, two are `PermissionsMixin` M2M join tables, and the rest are Django's own.

```
                        ┌───────────────────────┐
                        │ orgs_organization     │  the tenant root
                        │  name, slug*, logo,   │  * unique among LIVE rows only
                        │  brand, base_currency,│
                        │  timezone             │  <- the ONLY timezone in the schema
                        └───────────┬───────────┘
              ┌─────────────────────┼─────────────────────┐
              │ org                 │ org                 │ org
      ┌───────▼───────┐     ┌───────▼───────┐     ┌───────▼────────┐
      │ orgs_store    │     │ orgs_role     │     │ orgs_membership│
      │ name          │     │ name          │     │ user ──────────┼──► accounts_user
      │ brand         │     │ permissions   │     │ role ══════════╡    (PROTECT)
      │ use_own_      │     │  (JSON list   │     │                │
      │  branding     │     │   of codes)   │     └───────┬────────┘
      │               │     │ is_preset     │             │ membership
      │ UNIQUE(id,org)│     │ UNIQUE(id,org)│             │ UNIQUE(id,org)
      └───┬───────────┘     └───────────────┘             │
          │                                     ┌─────────▼──────────┐
          │ store ══════════════════════════════╡ orgs_storeaccess   │
          │                                     │ org (denormalised) │
          │                                     └────────────────────┘
          │ store ═══════════════╗
   ┌──────▼──────────────────────╨──┐        ┌──────────────────────────┐
   │ audit_auditlog  APPEND-ONLY    │        │ accounts_user            │
   │  org (nullable), store (null.),│        │  username* email* phone* │
   │  actor, action, target_type,   │        │  (* all three globally   │
   │  target_id, changes (JSONB),   │        │   unique; all three are  │
   │  ip, at                        │        │   login identifiers)     │
   └────────────────────────────────┘        │  language, is_active     │
                                             └──────────────────────────┘

   ═══  a composite (child, org) -> parent (id, org) foreign key.
        Four exist, all DEFERRABLE INITIALLY IMMEDIATE:
          orgs_membership_role_same_org_fk        (role_id, org_id)
          orgs_storeaccess_membership_same_org_fk (membership_id, org_id)
          orgs_storeaccess_store_same_org_fk      (store_id, org_id)
          audit_auditlog_store_same_org_fk        (store_id, org_id)
```

**Abstract bases** (in `common/models.py`, no tables of their own):

| Base | Adds | Inherited by |
| --- | --- | --- |
| `PublicIdModel` | `public_id` UUIDv7, `unique=True`, `editable=False`, Python `default=uuid.uuid7` | everything, directly or through `SoftDeleteModel` |
| `AuditedModel` | `created_at/by`, `updated_at/by` (actors nullable + `PROTECT`) | the four `orgs` models |
| `SoftDeleteModel(PublicIdModel)` | `deleted_at/by`; `objects` = live only, `all_objects` = everything; neither can delete | the four `orgs` models |
| `StoreScopedModel(SoftDeleteModel, AuditedModel)` | `store` FK (`related_name="+"`, `PROTECT`) + the scope-guarded manager | **nothing yet** — zero concrete subclasses |

`PublicIdModel` is mixed into `SoftDeleteModel` and **not** into `AuditedModel`. That is not
style: `Organization(SoftDeleteModel, AuditedModel)` would then collect the same field twice
and Django raises `FieldError`. `accounts.User` and `audit.AuditLog` are neither
soft-deletable nor audited, so they mix `PublicIdModel` in directly.

### What slices 2–6 add

Shapes from [the architecture spec §4](superpowers/specs/2026-09-01-raporo-architecture-and-schema-design.md).
Roughly fifteen tables, and **almost every one is a `StoreScopedModel`** — which is exactly
why the `org` column has to land before slice 2 (§4.4).

```
slice 2  catalog    Product, Variant                          StoreScoped
         inventory  StockMovement (append-only), StockLevel   StoreScoped
slice 3  sales      Customer, Sale, SaleItem, Order,
                    OrderItem, Payment (append-only)          StoreScoped
slice 4  reporting  GeneratedReport                           org-level, Store nullable
slice 5  money      Expense, Cycle, CycleInvestor,
                    CapitalEntry (append-only),
                    Payout (append-only)                      StoreScoped
                    Investor                                  org-level
slice 6  notifications AlertEvent                             StoreScoped
```

Four of those are **append-only ledgers** (`StockMovement`, `Payment`, `CapitalEntry`,
`Payout`). They reuse `common/db.py::append_only_triggers_v1(table)` — the function is
installed once by `audit/0002`, and each new table's migration carries **only the trigger
operation** plus a dependency on `("audit", "0002_append_only_trigger")`. The module
docstring spells out the four ways to get that wrong; read it before writing the migration.

Three more slice-1 tables are specified but unbuilt: `accounts.TwoFactor`,
`accounts.RecoveryCode`, `orgs.Invite`.

### `MoneyFields` (slice 3+, DESIGNED)

`amount DECIMAL(14,2)` · `currency CHAR(3)` (default = store base) · `exchange_rate
DECIMAL(20,10) NULL` · `amount_base DECIMAL(14,2)`. If `currency == base`, the rate is NULL
and `amount_base = amount`; otherwise the rate is **mandatory** and `> 0`. Line prices
(`SaleItem`, `OrderItem`) are always in store base currency — foreign currency enters only at
`Payment` / `CapitalEntry` / `Payout` / `Expense`. Base currency is Rwf, 0 decimals.

---

## 4. Tenancy — invariant #1

> **Invariant #1.** A business row belongs to exactly one store, and a query may never span
> two organizations. A cross-tenant leak is Critical and release-blocking.

This is the heart of the system and the reason most of `common/` exists. The machinery has
survived roughly 110 attacks across three security harness runs and four fix rounds; five
leaks were found *after* the code was reviewed and believed correct. Treat every guard below
as load-bearing, and read §4.10 before removing one.

### 4.1 The layers, and which of them execute

| Layer | What it stops | Status |
| --- | --- | --- |
| Query-layer scope guard (`common/managers.py`) | an unpinned or cross-store read/write, through any ORM path | **BUILT** |
| Model-layer same-store assertion (`StoreScopedModel.save`) | an IDOR through a foreign key | **BUILT** |
| Startup checks E001–E006 (`common/checks.py`) | a *model shape* that would leak before any query runs | **BUILT** |
| Composite `(child, org) -> parent (id, org)` keys | a row that mixes two organizations | **BUILT** — four of them |
| Append-only audit trail (trigger + privileges) | rewriting or erasing history | **BUILT** |
| Three-role database split | the app process being able to disable any of the above | **BUILT** |
| `org` column on `StoreScopedModel` | — | **DESIGNED — the column does not exist** |
| Row-level security | the whole class "a query that forgot its tenant filter" | **DESIGNED — entirely absent** |
| Tenant context (`common/tenancy.py`) | a request with no tenant; context leaking between requests | **DESIGNED — the module does not exist** |
| Permitted-store resolver (`permitted_stores`) | reaching a sibling store you were not granted | **DESIGNED** |

### 4.2 The query-layer guard stack — **BUILT**

`common/managers.py`. Three managers, and choosing between them is the whole API:

- **`objects`** on a store-scoped model is a `StoreScopedManager`. It returns live rows and
  **refuses to compile at all** until a store is pinned with `for_store(store)` or
  `for_stores([...])`. `UnscopedQueryError` otherwise.
- **`all_objects`** is the complete, unfiltered view — tombstones included. It is for audits
  and data migrations. It cannot delete, and it cannot traverse a hidden relation.
- **`objects`** on a soft-deletable-but-not-store-scoped model (the `orgs` spine) is a
  `SoftDeleteManager`: live rows, no delete.

Six decisions in that file are the product of specific failures, and each is a trap for
anyone refactoring it:

1. **The scope guard hooks `sql.Query.get_compiler`, not `QuerySet._fetch_all`.** Every read
   — `count()`, `exists()`, `aggregate()`, `iterator()`, `values_list()`, `explain()`, and a
   queryset used as a **subquery inside someone else's query** — has to build a compiler. A
   `_fetch_all` override silently misses most of those.
2. **Set operators are guarded at the seam, never by a list of method names.** `|`, `&` and
   `^` all reach `sql.Query.combine`; `union()`, `intersection()` and `difference()` all
   reach `QuerySet._combinator_query`. Both are overridden, so an operator Django adds later
   is covered by construction. Enumerating dunders is exactly how `^` shipped unguarded.
3. **`union()` is additionally overridden**, because it can skip the seam entirely: it drops
   an `EmptyQuerySet` `self` and, with one non-empty leg left, hands that leg back without
   building a combined query. `for_store(A).none().union(all_objects.all())` returned every
   organization's rows.
4. **A widening merge re-resolves ownership against the database.** `for_stores([A, RIVAL])`
   is refused, so `for_store(A) | for_store(RIVAL)` is its synonym and has to go through the
   same resolver (`merge_scope_pks`). Narrowing is only sound for `&` and `intersection()` —
   hence a `narrow` flag rather than a connector string, because the two call sites speak
   different vocabularies and a string compared against one is dead code on the other path.
5. **`|` or `^` on a *sliced* store-scoped queryset is refused by name.** Django rebuilds a
   sliced operand through `_base_manager`, which is `all_objects` — neither store-pinned nor
   live-only. Combine first, then slice.
6. **`related_name="+"` is not enough, so the literal `+` query name is refused too.** Django
   still resolves `filter(**{"category__+__name": ...})` as a path, which is an existence
   oracle for another tenant's rows. `GuardedQuery.names_to_path` raises. **Standing rule
   regardless: never build an ORM lookup key from request data.**

Write-side refusals on `ScopedQuerySet`: `create` / `get_or_create` / `update_or_create` fill
or verify `store`; `bulk_create` re-runs the same-store check per row because it never calls
`save()`; `update()` refuses outright to rewrite `store` (re-parenting must move children,
which a bulk column write cannot do) and refuses a cross-store FK value, a query expression
in a store-scoped FK, and any multi-store pin where no single value can be proven correct for
every row it would hit. `StoreScopedManager.raw()` is refused — use `all_objects.raw()`,
which is greppable and reviewable.

**Note for `bulk_update()` callers:** it wraps its `update()` calls in
`transaction.atomic(savepoint=False)`, so a `CrossStoreReferenceError` raised inside marks
the *surrounding* transaction for rollback. Catching it and carrying on gives
`TransactionManagementError`. Validate before the write.

### 4.3 The store-scoping pin — **BUILT**, with one arbitrated residual

`for_store(store)` and `for_stores(stores)` pin a queryset. `_store_pk` refuses `None`
because `WHERE store_id IS NULL` looks scoped and returns nothing, which is how scoping bugs
hide. `_store_pks` resolves the set against `orgs_store` and refuses a set spanning two
organizations.

**Neither primitive authorizes a store against a caller, and that is correct.** A pin
enforces a scope; deciding which scope is the service layer's job. The controller arbitrated
this: the only genuine residue is a **diagnostic asymmetry** — an unknown store id raises
`ValueError` from `for_stores()` and returns **silently empty** from `for_store()`. Routing
`for_store()` through the shared resolver fixes the diagnostic. It closes no leak; do not
write a task that says it does.

Authorization is [ADR 0011](adr/0011-org-wide-store-access-is-a-permission-code.md)'s
`permitted_stores(membership)` plus `require_store()`, which turns a URL identifier into a
store the actor may reach, or a 404. **Store ids from request data never reach `for_store()`.**

### 4.4 The `org` column — **DESIGNED, and the clock is running**

[ADR 0008](adr/0008-denormalised-organization-on-store-scoped-rows.md) +
[schema plan §A](superpowers/specs/2026-09-02-schema-hardening-plan.md).

Today a store-scoped row reaches its organization only through `Store.org`. Verified today,
`StoreScopedModel`'s fields are exactly `public_id, created_at, updated_at, created_by,
updated_by, deleted_at, deleted_by, store` — **there is no `org` column** — and there are
**zero concrete subclasses** in the production apps.

That last fact is the whole scheduling argument: **the column is free right now and a data
migration across ~15 tables the moment slice 2 lands.**

The design, in the form the controller settled:

```python
org = models.ForeignKey(
    "orgs.Organization", verbose_name=_("organization"),
    on_delete=models.PROTECT, related_name="+",
    editable=False, db_index=False, db_constraint=False,
)
```

- **`org`, not `organization`** (controller arbitration). Four SHA-256-pinned `RunSQL`
  statements already contain the literal `REFERENCES orgs_store (id, org_id)`, and the
  stability contract forbids editing shipped pinned text. `organization` would buy a schema
  where the same concept is `org_id` on five tables and `organization_id` on fifteen, and
  every new composite key would read `FOREIGN KEY (organization_id, store_id) REFERENCES
  orgs_store (id, org_id)` — which reads like a bug on every future review.
- **`related_name="+"` is mandatory**, or `common.E004` fails startup, correctly.
- **Non-nullable.** PostgreSQL MATCH SIMPLE leaves a composite FK *unchecked* when either
  column is NULL. Nullable here is a bypass of both the key and any future policy. (That is
  correct and deliberate for `AuditLog`, which has neither org nor store on a system row; on
  a business table it is a hole.)
- **`db_constraint=False`** — measured, not preferred. The composite FK already proves the
  organization exists transitively, since `orgs_store.org_id` carries its own FK. A separate
  `org_id -> orgs_organization` FK would take a `FOR KEY SHARE` row lock on the organization
  row for **every insert into every business table**, and `create_store` holds
  `SELECT ... FOR UPDATE` on that row — so creating a store would block every sale in that
  organization. Paired requirement: **`create_store` uses `select_for_update(no_key=True)`.**
  Belt and braces; either alone works, both mean neither can be undone by accident.
- **`db_index=False`** — a standalone `org_id` btree is redundant with every org-leading
  composite index and is pure write cost on the hottest tables in the product.
- **Derived, never asked for**, following `StoreAccess._derive_org`: from the pinned
  queryset, a cached `Store`, the tenant context, or one lookup. Three write paths need it —
  `save()`, the manager (because `bulk_create` never calls `save()`), and everything else,
  which `NOT NULL` plus the composite key simply refuse.

The pin then becomes a pair `ScopePin(org_pk, store_pks)`, and a widening merge compares two
integers instead of issuing a query.

### 4.5 The composite foreign keys — **BUILT** (four), **DESIGNED** (the generator)

```sql
ALTER TABLE <table> ADD CONSTRAINT <table>_store_same_org_fk
FOREIGN KEY (store_id, org_id) REFERENCES orgs_store (id, org_id)
DEFERRABLE INITIALLY IMMEDIATE;
```

The target `orgs_store_id_org_uniq` exists and is deliberately **unconditioned**: measured,
PostgreSQL refuses a partial unique index as a foreign-key target (`ERROR: there is no unique
constraint matching given keys for referenced table`). `common.E005` turns that into an
explicit rule rather than a surprise at `ADD FOREIGN KEY` time.

`DEFERRABLE INITIALLY IMMEDIATE`, never `INITIALLY DEFERRED`: deferred violations surface only
at `COMMIT`, which never happens inside a test transaction, so the tests would pass vacuously.

**What the key buys beyond tidiness.** It makes "a store never changes organization" a
*database* fact — the `UPDATE` that would change `orgs_store.org_id` is refused for any store
with a single business row. That refusal is what licenses caching a store's organization for
the duration of a request. If the key is ever dropped, that reasoning goes with it. It also
closes a measured leak RLS alone does not: **referential-integrity triggers bypass RLS**, so a
single-column FK to a store-scoped table *accepted* a cross-tenant child row; the composite
key refuses it and makes the two error messages identical, killing the oracle.

`common/db.py::same_org_fk_v1(table)` — a versioned, hash-pinned generator so slice 2's four
tables do not hand-roll it — is **DESIGNED**. `common/db.py` today carries only the
append-only helpers.

### 4.6 The append-only audit trail — **BUILT**

Enforced at four independent levels, which is the point:

1. `AuditLog.save()` forces `force_insert` and refuses a non-adding instance. `_state.adding`
   alone is not enough — it is still `True` on an instance constructed with an explicit `pk`,
   and Django's `_save_table` then takes the UPDATE branch, silently rewriting a row.
2. `AppendOnlyQuerySet` refuses `update()`, `delete()`, `_raw_delete()` and
   `bulk_create(update_conflicts=True)` — an UPDATE wearing an INSERT's clothes.
   `Meta.base_manager_name = "objects"`, or Django's own internal `_base_manager` paths can
   erase the trail.
3. A plpgsql trigger (`raporo_append_only`) raises `restrict_violation` on UPDATE and DELETE
   unconditionally, plus a separate **statement** trigger for TRUNCATE, which never fires row
   triggers. TRUNCATE is waived for a database named `test_*` (so `TransactionTestCase`
   teardown works) or a session that set `raporo.allow_truncate`;
   `raporo.enforce_truncate_guard = 'on'` turns both waivers off, which is how the suite
   proves the trigger really refuses. `common.E100` refuses to boot production on a database
   named `test_*` so it cannot inherit the waiver silently.
4. Privileges: `raporo_app` holds only `INSERT, SELECT` on `audit_auditlog`, so the attempt
   fails with `42501` *before* reaching any of our code — and keeps failing if a future
   migration drops the trigger by accident.

**PII redaction in `changes` is key-based**, in `apps/audit/services.py::record()`. The
standing policy is one sentence: **field names and IDs for anything personal, values for
anything else.** Three mechanisms, each chosen for a measured reason:

- **substring** match for terms unambiguous anywhere in a key (`email`, `phone`, `username`,
  `contact`, `address`, `customer`, `investor`);
- **whole-segment** match for short ones — `ip` as a substring would redact `description`,
  `membership`, `receipt` and `relationship`;
- a **person-qualifier** rule for `name`: `owner_name` and bare `name` redact, while
  `store_name`, `role_name` and `filename` keep their values, or a rename audit records that
  something was renamed to something.

Credentials are matched **first**, so the `_id` / `_count` carve-out that honours "IDs are
fine" cannot launder `session_id` or `api_key_id`.

Why this is structural and not stylistic: erasure operates on **referents**, never on the
trail (`erase_user()` anonymizes the `User`). A row holding foreign keys, a verb, a class
label and a timestamp stops identifying anyone the moment its referent is anonymized. A row
holding a quoted email does not, and the trigger means no migration-free fix exists. **Known
residuals, accepted:** key-based redaction cannot see PII inside a *value* (a list item has
no key to match), and `store_name` / `org_name` persist, which for a sole trader is often a
person's name. Hence: **do not put prose in `changes`.**

### 4.7 The database role split — **BUILT**

[ADR 0009](adr/0009-row-level-security-for-organization-isolation.md) §C.5 /
[threat model §1](superpowers/specs/2026-09-02-rls-threat-model.md). Verified in the live
cluster today.

| Role | super | BYPASSRLS | Owns | Holds |
| --- | --- | --- | --- | --- |
| `raporo_owner` | no | **no** | schema, every table, function, trigger | runs migrations; never serves a request; `CREATEDB` only under `RAPORO_DB_DEV_EXTRAS=1` |
| `raporo_app` | no | **no** | nothing | `SELECT, INSERT, UPDATE, DELETE` and nothing else. No TRUNCATE, no TEMPORARY, no CREATE on `public`, `USAGE` (not `SELECT`) on sequences, minus `UPDATE`/`DELETE` on `audit_auditlog`, `SELECT`-only on `django_migrations` |
| `raporo_backup` | no | **yes** | nothing | `SELECT` only. Required *today*: measured, a dump taken as `raporo_app` exits 1 |
| `raporo` | yes | yes | — | the postgres image's bootstrap superuser, phase 1 only |

Two withheld privileges are worth naming because they are not obvious. `raporo_app` has no
`CREATE` on `public` **and** no `TEMPORARY` on the database, so it has nowhere to define a
`SECURITY DEFINER` function — the most direct way for a runtime role to launder reads past a
policy. And it holds `USAGE`, not `SELECT`, on sequences, because reading a sequence's
`last_value` is a cross-tenant volume oracle with no policy in the way; `USAGE` is all an
`INSERT ... RETURNING id` needs.

**Two identities, two connection aliases, never one alias with a swappable `USER`.**
`DATABASES["default"]` is `raporo_app`; `DATABASES["migrator"]` is `raporo_owner` and exists
**only where `RAPORO_MIGRATE_USER` and `RAPORO_MIGRATE_PASSWORD` are both injected**. A
serving production workload gets neither, so the alias is simply absent there and a
`--database=migrator` invocation fails on Django's own argument parsing before opening a
connection. A mutable `USER` key is a variable one mistake can flip; an alias makes the
elevated identity a *place*. "Connect as the owner then `SET ROLE`" is not an alternative:
`RESET ROLE` climbs straight back out, so `SET ROLE` is a convenience, not a boundary.

**The two-phase grant model, and why it is two phases.** Phase 1 (`scripts/db/roles.sql`, as
superuser, at `initdb`) does everything possible before a table exists: roles, attributes,
CONNECT, schema ownership, default privileges. Phase 2
(`scripts/db/runtime-privileges.sql`, as the owner, **after every migration**, wired into
`docker/entrypoint.sh` next to it) does everything that names a table. This was measured the
hard way: with the table-level statements in phase 1, a wiped-volume boot produced a database
where `raporo_app` still held UPDATE and DELETE on `audit_auditlog` — and deleting from
Django's migration-history table as the app role removed all 24 rows. The statements had run
at `initdb`, before either table existed, and were silent no-ops. **A step that can be skipped
once produces that state**, which is why phase 2 is wired into the entrypoint rather than
written into a runbook.

Both scripts are **idempotent and convergent**: every grant is preceded by a `REVOKE ALL`, so
each file is the complete statement of what a role holds rather than an addition to it — a
`GRANT ALL` applied by hand under deploy pressure is removed by the next run. Phase 1 also
removes `raporo_app`'s membership in `raporo_owner` on every run, with a `NOTICE` naming it as
an escalation path.

**Default privileges carry grants forward but NOT row-level security.** A table created after
phase 1 arrives with DML granted and `relrowsecurity = f`. That is silent, and once policies
exist it is a cross-tenant leak. Boot-time conformance (`common.E102`) is the only thing that
can make it loud; no SQL file can do it.

The suite runs as `raporo_owner` — not for convenience. `pytest-django` creates, migrates and
drops `test_raporo`, which needs `CREATEDB` and ownership, which `raporo_app` must never
have. `config/settings/test.py` raises `ImproperlyConfigured` with the variable names rather
than falling back.

**Verified absent:** the grant that would make `raporo_owner` a member of `raporo_app` — zero
rows in `pg_auth_members` for the raporo roles. It is deliberately deferred to whoever writes
the `as_tenant` fixture that needs to take the app role inside a transaction, together with
the test proving the fixture changes `current_user`. Note also that `test_raporo` gets **no**
phase-2 grants (phase 2 runs against the `migrator` alias), which is harmless while the suite
is the owner and will produce `permission denied for table` — not a policy denial — the day
that fixture lands.

### 4.8 Row-level security — **DESIGNED. ENTIRELY ABSENT.**

Verified today: `SELECT count(*) FROM pg_policies` returns **0**. No table has
`relrowsecurity`. `raporo_current_org_id()` does not exist; the only `raporo*` function in the
database is `raporo_append_only`. **The commit message of `9a697c3` claims "RLS scaffolding" —
that claim is false. Do not repeat it.**

The role split was the hard prerequisite (three agents reached that independently) and it is
now built, so RLS is unblocked. When it lands, this is the settled shape:

- **Organization isolation in the database; store isolation in the application.** RLS's
  practical unit is a value that is constant for the whole request and cheap to compare. The
  organization is that; a permitted store *set* is not — one request can legitimately query
  store A, then B, then both, and a GUC that changes mid-transaction is the class of bug the
  tenant context exists to eliminate.
- **`ENABLE ROW LEVEL SECURITY` only. No `FORCE`. No `BYPASSRLS` anywhere.**
  ([ADR 0009 Amendment](adr/0009-row-level-security-for-organization-isolation.md), controller
  arbitration.) `FORCE` plus `BYPASSRLS` is self-cancelling; and dropping `BYPASSRLS` with
  `FORCE` in place makes `UPDATE` and `DELETE` affect **0 rows without error**, so
  data-migration backfills silently no-op and the whole suite returns zero rows. Measured, not
  reasoned. The owner bypasses policy by *being the owner*, and `common.E101` asserts the
  runtime identity at boot instead — a check that can actually fail loudly.
  > ADR 0009's original `FORCE` + `BYPASSRLS` text is retained above its amendment on
  > purpose, because the reasoning was sound about Postgres semantics and wrong about their
  > interaction. It is **not** current. Read the amendment.
- **The context accessor, and why every character of it matters.** A `STABLE PARALLEL SAFE`
  SQL function `raporo_current_org_id() RETURNS bigint`, whose whole body is

  ```
  SELECT NULLIF(current_setting('raporo.org_id', true), '')::bigint
  ```

  Without the `true`, a session that never set the GUC **raises** `unrecognized configuration
  parameter`. Without the `NULLIF`, a session that set and then cleared it returns the empty
  string, and casting that to `bigint` raises `22P02`. Two different errors for one state. The
  helper makes that state one value — `NULL` — and a `NULL` comparison makes every predicate
  false, so the database fails closed to zero rows. `STABLE`, never `IMMUTABLE`; pinned
  `search_path`; installed through a versioned, hash-pinned constant in `common/db.py`.
- **Policies carry both `USING` and `WITH CHECK`, and no `FOR` clause.** A `USING`-only policy
  leaves a tenant-hopping `INSERT`/`UPDATE` wide open. Plus a `RESTRICTIVE` floor, because one
  future permissive `USING (true)` policy **ORs the boundary open** — measured, 2 rows became
  the whole table; a restrictive floor held it at 2.
- **Coverage:** every `StoreScopedModel` table, `orgs_organization` (its own `id` is the key),
  `orgs_store`, `orgs_role`, `orgs_membership`, `orgs_storeaccess`, `audit_auditlog`.
  **`accounts_user` gets no policy** — a decision, not an omission: username, email and phone
  are unique installation-wide and the multi-identifier auth backend must resolve an
  identifier to a user *before any organization is known*, so there is nothing to key a
  policy on, and an "or when the context is NULL" escape hatch on the credential table is
  worse than no policy. Compensating controls: the non-enumerating backend, per-identifier and
  per-IP throttling, uniform error responses.
- **Two carve-outs that must be written as carve-outs.** Registration creates the
  organization, so `orgs_organization` must permit an `INSERT` when the current org id is
  `NULL`; that appears in the denial matrix as an explicitly **allowed** case, so nobody later
  "fixes" it and breaks registration. And `audit_auditlog.org_id` is nullable for system rows,
  so its policy text is security-engineer's: a tenant must never *read* a NULL-org row, the
  app must be able to *write* one, and if it can, that is a record-hiding primitive worth
  ruling on.

**The honest limitation, which belongs here and not in a footnote.** `raporo.org_id` is a
*custom* GUC, so any role can set it. **RLS defends against our own bugs** — a missing
`WHERE`, a raw query, `all_objects` in a hurry, a future DRF view that forgets the pin — not
against an attacker with arbitrary SQL execution as the app role. An SQL-injection finding is
**not** one notch less severe because RLS exists. That is the same limitation the append-only
trigger was accepted with, for the same reason.

What it does buy is large: it converts the *entire class* "a query that forgot its tenant
filter" from a leak into zero rows, across the ORM, raw SQL, `all_objects`, reverse relations,
joins, aggregates, set operators, the admin, `dbshell`, and every reporting query nobody has
written yet. That class produced two Critical and five High findings in slice 1 alone.

And the cost, stated plainly: **fail-closed produces a plausible empty report, not an error.**
Measured with no context, a `count()` plus `sum()` over a sales table returns `0, NULL`. For a
reporting product that is a correctness hazard, and it is why the positive assertion in every
denial test is not optional.

### 4.9 The tenant context — **DESIGNED.** `common/tenancy.py` does not exist.

One door: `tenant(org_pk, *, source)` in `common/tenancy.py`, a context manager that

1. asserts `org_pk` is a positive `int` — no `Organization` instance, no string, the same
   normalisation discipline `_store_pk` uses;
2. enters `transaction.atomic()` and asserts `connection.in_atomic_block`, because
   `SET LOCAL` outside a transaction is a no-op that emits only a `WARNING`;
3. issues **`SET LOCAL` on `raporo.org_id`** as that transaction's **first** statement,
   parameterised, never interpolated;
4. sets a `ContextVar` and **resets it with the token in a `finally`**.

**`SET LOCAL`, never a bare `SET`.** Measured, one backend, two transactions: with a bare
`SET`, the second transaction with no context of its own read the rival's row *and* inserted
into the rival's org. Also measured through PgBouncer 1.25.2 in `pool_mode=transaction` with
the default config: a bare `SET` leaked across two *separate clients* on one server pid,
because `server_reset_query = DISCARD ALL` is **not applied on release in transaction mode**.
`SET LOCAL` leaked in neither case, and being the first statement of the *outermost*
transaction is what makes a savepoint rollback unable to unset it. So: **do not promote
`raporo.org_id` to a session-level setting at connection creation to save a round trip.** That
is the leak, and it is the leak caused *by* the isolation mechanism. A source-scan test is
designed to make it unfixable by a well-meaning optimisation: a bare `SET ` adjacent to a
`raporo.` GUC may appear in exactly two files.

`contextvars`, not thread-locals, so the same primitive works unchanged under ASGI and across
`sync_to_async`. The token reset is mandatory, not hygiene: a WSGI worker thread reuses its
context, so a `set()` without a matching `reset()` leaks the previous request's org into the
**next** request on that thread — the application-layer twin of the connection-reuse leak.

Three callers, never three implementations: `common/middleware.py::TenantMiddleware` (placed
after `AuthenticationMiddleware`, before `MessageMiddleware`), `TenantCommand` for management
commands (`--org <public_id>`), and a `@tenant_task` decorator when Celery arrives in slice 6.
Task context is **never** inherited implicitly from the enqueuing request — a delayed task can
run after the membership was revoked, so the org is re-resolved and re-authorised at execution
time.

Org resolution in the request path: anonymous → **no context** (login, registration, password
reset, `/healthz` and static must not need one). Authenticated → one
`select_related("role")` membership query, re-read **every** request so a revoked membership
drops the context on the next one. Nothing in the session participates: a user belongs to
exactly one organization, so there is no current-org selection, no session key and no org
switcher. `DoesNotExist` → no context. `MultipleObjectsReturned` → a violated database
constraint, therefore a bug: a 500 with an audit row, **never** resolved by picking one
membership, because `.first()` would convert a constraint violation into a silent arbitrary
choice of tenant.

Consequences of the middleware owning a transaction, which belong in `DEVELOPMENT.md` rather
than in a bug report:

- Every tenant request is one transaction. `ATOMIC_REQUESTS` stays **False** — one mechanism,
  not two.
- Template rendering happens *inside* the middleware chain, so lazy querysets evaluated during
  rendering are inside the transaction and inside the GUC. That property is what makes this
  design work at all in a server-rendered app, and it deserves its own test.
- A `StreamingHttpResponse` is consumed **after** the middleware returns, so a lazy queryset in
  one would evaluate with no GUC and yield nothing. **Rule: no streaming response may lazily
  query tenant data.** The per-tenant export is a management command for this reason.
- `statement_timeout` < the slowest tenant view < `idle_in_transaction_session_timeout`.

**Fail-closed, in two layers with deliberately different failure modes:**

| Layer | No context | Wrong context |
| --- | --- | --- |
| Database (RLS, app role) | reads return zero rows; `INSERT` violates `WITH CHECK` and raises | same |
| Application | `require_org_id()` raises `NoTenantContextError` | `get_compiler` raises `TenantContextMismatch` |

The `get_compiler` cross-check is a **diagnostic, not a control**, and the design says so
explicitly so the next reviewer does not over-trust it. The control is RLS; the check turns a
silent empty result into a named error carrying both org ids.

### 4.10 What RLS does *not* cover — the sentence to rely on

> With org-level RLS in place, an application bug can no longer show one organization another
> organization's data. It can still show one **store's** data to a user entitled only to a
> **sibling store in the same organization**, because store entitlement is a *set* per user
> and the database is only told the org. **Inside an organization, RLS is blind and the
> application-layer guards are the entire defence.**

So: **keep every guard in §4.2.** Fix rounds 1–3 spent most of their effort on within-org and
scope-mixing findings, and RLS would have caught **none** of them. Removing an application
guard on the strength of RLS is unjustified.

Also outside RLS's reach: same-org privilege escalation (the `PRESETS["Manager"]`
self-promotion shape), anything reachable via arbitrary SQL, `accounts_user` and session data,
existence oracles via unique constraints, and everything above the database — CSRF, XSS,
session fixation, rate limiting, uploads, TLS.

### 4.11 Permitted stores, and the store-access rule — **DESIGNED**

[ADR 0011](adr/0011-org-wide-store-access-is-a-permission-code.md). Elvis's rule: a user
reaches only the stores they were granted, **except** the org owner, who reaches any store in
their org.

One permission code, **`store.access_all`**, and one resolver,
`apps/orgs/services/access.py::permitted_stores(membership)`. Nothing else in the codebase
reads `StoreAccess` or decides which stores an actor may reach.

**Never `role.name == "Owner"`.** `Role.name` is a user-editable `CharField`, unique only
among live rows and only within one organization. An org can rename its Owner role (Raporo
ships EN/RW/FR) which would silently *remove* the override, or create a decoy role called
"Owner" with no real power, which would silently *grant* it. That is a live vulnerability, not
a style problem — and the canonical test fixture therefore contains `a_decoy`, a role literally
named `"Owner"` holding only `sale.record`, with the real owner role named `Nyiricyubahiro`,
because a matrix that names the powerful role "Owner" would pass under a name-based
implementation.

Four rules make it safe: owner memberships get **no** `StoreAccess` rows (a row that does not
control access is a decoy for anyone auditing "who can reach A2"); `store.access_all` widens
*reach* and grants no *rights* (which stores and which actions stay orthogonal axes, so a
custom role may hold `store.access_all` with only `report.generate` — an accountant who reads
every branch and writes nowhere; views must pass both gates); presets become exhaustive and
checked at startup; and the set is **never cached** across requests or in the session, because
revocation must take effect immediately. At the product cap of five stores that is two indexed
queries returning at most five rows.

**Verified absent today:** `store.access_all` appears nowhere in `apps/`, `common/`, `config/`
or `tests/`, and `PRESETS["Manager"]` is still the **subtractive**
`PERMISSIONS - {ROLE_MANAGE, STORE_MANAGE}`. That subtraction is not cosmetic: adding
`store.access_all` to the catalog would grant it to Manager **silently, on the day the code is
introduced**, breaking Elvis's rule. Exhaustive presets plus `common.E010` are part of that
change's minimum, not a follow-up.

**The blast radius, because it is the sentence to rely on:** if `permitted_stores()` has a bug,
every store in that **one** organization becomes readable and writable by every member of it,
and nothing below Python will stop it — RLS checks the organization and the organization is
correct; the composite key checks the organization and the organization is correct; the store
predicate the query carries is the one the buggy function produced. Not cross-tenant. The only
detection is a test, which is why `tests/test_tenancy_matrix.py` is a release gate for that
change and not a follow-up.

**Denials are 404, never 403.** A 403 confirms the row exists and turns the override's
complement into an existence oracle across sibling stores. `StoreNotPermitted` must therefore
**not** subclass `PermissionDenied`, whose default Django handler renders 403. Denials are
audited under the **actor's** org — recording org B's owner's attempt under org A would be a
cross-tenant write RLS refuses anyway.

### 4.12 One organization per user — **DESIGNED**

[Schema plan §J](superpowers/specs/2026-09-02-schema-hardening-plan.md). Elvis's ruling. Lands
as `orgs_membership_unique_live_user` — a `UniqueConstraint` on `user` alone, conditioned on
live rows, with `violation_error_code="unique"`.

**Built today:** `Membership` carries `(user, org)` live-unique only. The one-org constraint is
**not** built.

Three details that are not guessable. The per-org constraint **stays** (measured, it fires
first in the more common failure mode, and its message is the better one there).
`violation_error_code="unique"` is what makes the error land on the `user` field instead of in
`NON_FIELD_ERRORS`, because `Model.validate_constraints()` only re-files a constraint error
onto a field when the code is `"unique"` **and** the constraint names exactly one field. And
`violation_error_message` never appears in an `IntegrityError` at all — a conditional unique
constraint is a partial index, so PostgreSQL reports the **name**. Two artefacts, two
audiences: the name carries the meaning for the operator, the message is written for the
person filling in the invite form.

Also worth knowing: the join table **stays**, so allowing multi-org later is a
`RemoveConstraint` rather than a data migration. And the one thing the database cannot help
with — a read path over `Membership.all_objects` that treats a soft-deleted row as
authorization. The constraint deliberately ignores dead rows, so a user *can* have a live
membership in B and a dead one in A; a resolver reaching for `all_objects` and taking
`.first()` grants access to A. One resolver, live manager, `.get()`.

Consequence: **there is no self-service path to a second organization.** A second business is a
second store in one org (`MAX_STORES_PER_ORG = 5`, and the branding chain already lets two
stores present as two businesses on their reports). Worth writing into the signup copy rather
than discovering in support.

### 4.13 Indexing under tenancy — **DESIGNED**

[Schema plan §D](superpowers/specs/2026-09-02-schema-hardening-plan.md). The direction is
counter-intuitive: **remove 19 indexes, create 6, defer 7 with named triggers.** Net −13, plus
one `public_id` unique index per table, which is a correctness requirement and not a
performance guess.

Django creates a single-column btree on every FK, including the
`created_by`/`updated_by`/`deleted_by` actors the abstract bases contribute to every table —
three per table, on five tables, that no query in the product will ever use. They are declared
`related_name="+"`, so they are not even traversable. The usual reason to index an FK (a parent
`DELETE` scans children) does not apply: hard delete is structurally forbidden. And
`db_index=True` on a `CharField` costs **two** indexes, because Django adds a
`varchar_pattern_ops` twin.

The rule that replaces them: **every `Index` on a store-scoped model leads with `org` or
`store`** (`common.E007`, no escape hatch). After the pin change every scoped query filters on
the org first and the store set second, so a tenant-leading composite is the shape the planner
wants and a non-leading index is a promise the query shape cannot keep. Unique constraints are
not `Index` objects, so E007 does not see them and the global `public_id` unique index is
untouched — which is correct, and looks like an inconsistency unless you know why.

`models.Index` names are capped at **30 characters** by Django (`models.E034` over it);
`UniqueConstraint` has **no** such cap, which is why existing constraint names are longer. Do
not "fix" a constraint name to fit 30. Convention: `<app>_<model>_<cols>`, with `scope`
standing for the `(org, store)` pair, and a one-line `# reason:` comment naming the query each
index serves, or it does not go in.

### 4.14 Connections and timeouts — **OPEN** (see §8)

The two specs contradict each other. `config/settings/base.py` today sets neither
`CONN_MAX_AGE` nor `OPTIONS`, so the implicit `CONN_MAX_AGE = 0` is in force and no connection
is reused. Whichever way it is settled, the RLS-relevant conclusion is identical and already
settled: **`SET LOCAL` inside a transaction, always** — it is safe under persistent
connections, under a psycopg pool, and under PgBouncer in transaction mode, and nothing else
is.

---

## 5. The intended directory tree

This is the **target** shape, including the frontend. Only unmarked lines exist today.

```
raporo/
├── manage.py · requirements.txt · pytest.ini · ruff.toml · compose.yaml
├── compose.prod.yaml                                  [DESIGNED — absent]
├── .env.example                       (DJANGO_MEDIA_ROOT still missing from it)
├── docker/
│   ├── Dockerfile                     multi-stage: base -> dev -> runtime
│   └── entrypoint.sh                  pre-boot check; migrate + phase-2 grants
├── scripts/
│   ├── setup.sh
│   └── db/  bootstrap-roles.sh · roles.sql (phase 1) · runtime-privileges.sql (phase 2)
│
├── config/
│   ├── settings/  __init__ · base · dev · prod · test
│   ├── urls.py                        admin, /healthz, i18n  (no app routes yet)
│   └── wsgi.py · asgi.py
│
├── common/                            cross-cutting invariants only
│   ├── models.py                      PublicIdModel, AuditedModel,
│   │                                  SoftDeleteModel, StoreScopedModel
│   ├── managers.py                    the scope-guard stack
│   ├── checks.py                      common.E001-E006, E100
│   ├── db.py                          versioned + hash-pinned migration SQL
│   ├── validators.py                  phone, currency, timezone, image
│   ├── apps.py · __init__.py
│   ├── management/commands/grant_runtime_privileges.py
│   ├── tenancy.py                     [DESIGNED] tenant(), the GUC, the ContextVar
│   ├── middleware.py                  [DESIGNED] TenantMiddleware
│   ├── selectors.py                   [DESIGNED] get_scoped() — public_id -> row + pin
│   ├── money.py                       [DESIGNED, slice 3] MoneyFields + the FX rules
│   └── management/base.py             [DESIGNED] TenantCommand
│
├── apps/
│   ├── accounts/                      User (username/email/phone), PhoneField
│   │   ├── models.py · managers.py · apps.py · migrations/ 0001 0002 0003
│   │   ├── backends.py                [DESIGNED] non-enumerating multi-identifier auth
│   │   ├── services/                  [DESIGNED] register.py · login.py · twofactor.py
│   │   ├── forms.py · views.py · urls.py            [DESIGNED]
│   │   └── templates/accounts/                      [DESIGNED]
│   │       ├── login.html · register.html
│   │       └── partials/              HTMX fragments
│   ├── orgs/                          Organization, Store, Role, Membership, StoreAccess
│   │   ├── models.py · permissions.py · apps.py · migrations/ 0001 0002
│   │   ├── services/                  [DESIGNED]
│   │   │   ├── access.py              permitted_stores(), require_store()
│   │   │   ├── orgs.py                register_owner()
│   │   │   ├── stores.py              create_store()  (select_for_update(no_key=True))
│   │   │   └── roles.py · members.py · invites.py
│   │   ├── management/commands/export_org.py        [DESIGNED] TenantCommand, NDJSON
│   │   ├── views.py · urls.py · forms.py            [DESIGNED]
│   │   └── templates/orgs/ + partials/              [DESIGNED]
│   ├── audit/                         AuditLog + record()
│   │   └── models.py · services.py · apps.py · migrations/ 0001 0002 0003
│   ├── catalog/       [slice 2]  Product, Variant
│   ├── inventory/     [slice 2]  StockMovement (append-only), StockLevel
│   ├── sales/         [slice 3]  Customer, Sale, SaleItem, Order, OrderItem, Payment
│   ├── money/         [slice 5]  Expense, Investor, Cycle, CycleInvestor,
│   │                             CapitalEntry, Payout
│   ├── reporting/     [slice 4]  GeneratedReport + periods.py (the period engine)
│   └── notifications/ [slice 6]  AlertEvent
│
├── templates/                         EXISTS but holds only .gitkeep
│   ├── base.html                      [DESIGNED] header: language switcher + store picker
│   ├── partials/                      [DESIGNED] shared fragments
│   └── 404.html · 500.html · 403.html [DESIGNED]
│
├── static/                            EXISTS but holds only .gitkeep
│   ├── vendor/htmx.min.js             [DESIGNED] vendored + version-pinned
│   └── css/ · js/ · img/              [DESIGNED]
│
├── locale/                            EXISTS but holds only .gitkeep
│   ├── rw/LC_MESSAGES/django.po       [DESIGNED]
│   └── fr/LC_MESSAGES/django.po       [DESIGNED]
│                                      (en is the source language — no catalogue)
│
├── tests/                             one flat suite, not per app
│   ├── conftest.py · fixtures/*.json
│   ├── testapp/                       concrete stand-ins for the abstract bases;
│   │                                  installed ONLY by config.settings.test
│   ├── test_common_bases.py · test_common_checks.py · test_db_stability.py
│   ├── test_orgs_models.py · test_user_model.py · test_audit.py
│   ├── test_phone_identity.py · test_phone_normalization.py
│   ├── test_public_id.py · test_fixture_loading.py · test_healthz.py
│   ├── test_tenancy_matrix.py         [DESIGNED] generated denial matrix — release gate
│   ├── test_tenancy_context.py        [DESIGNED] two requests, one reused connection
│   └── test_rls_denials.py            [DESIGNED] reads through the `app` alias
│
├── docs/
│   ├── ARCHITECTURE.md · ARCHITECTURE-ESSENTIALS.md   <- you are here
│   ├── PRODUCT.md · PRD.md · PROJECT-DESCRIPTION.md · ROADMAP.md
│   ├── DEVELOPMENT.md · SETUP.md
│   ├── adr/0001 … 0011
│   └── superpowers/  specs/ · plans/ · slice-1-workspace/
│
└── .github/workflows/ci.yml           [DESIGNED — no CI exists at all]
```

**Three reasons this is a diagram and not a scaffold.**

1. `docker/Dockerfile` copies **path by path** — `apps/`, `common/`, `config/`, `locale/`,
   `static/`, `templates/` are each named. A new top-level directory not added there is simply
   not in the image.
2. An empty `.py` is ruff-clean, `check`-clean and collects zero tests. **An empty tree is
   precisely how this project has previously shipped controls that never ran.**
3. `templates/`, `static/` and `locale/` already exist holding only `.gitkeep`, and
   `TEMPLATES["DIRS"]`, `STATICFILES_DIRS` and `LOCALE_PATHS` already point at them — so the
   frontend needs no new wiring, only files.

---

## 6. The startup-check family

### When a check earns its cost

The criterion, established by database-engineer and adopted by the controller:

> **A system check earns its cost when it enforces a rule over a *class* of models that new
> code can join without noticing.** "This one model declares this one constraint" is a
> **test**, not a check.

A corollary already paid for: a *data* check — a boot-time `GROUP BY … HAVING count(*) > 1` —
is worse than useless. It runs a growing query on every container boot and fails in the one way
you least want.

And the tag lesson, which cost two review rounds: **register under `Tags.security` or
`Tags.models`, never `Tags.database`.** `CheckRegistry.run_checks` silently drops every
database-tagged check unless an alias is passed explicitly, and a plain check run passes none.
`common.E100` sat inert for two rounds because of exactly this. The repair is the tag, not
"make the entrypoint pass an alias" — that would make a connection-free guard depend on a
reachable database at boot.

Standing coverage rule: every check's test drives it through
`django.core.checks.run_checks()` or the real command, **never** by calling the function
directly. A direct call passed while `E100` was broken.

### Registered today — verified by execution

Two functions, seven ids, both in `common/checks.py`.

| id | Refuses | Why it is a check and not a test |
| --- | --- | --- |
| `common.E001` | `objects` on a store-scoped model is not a `StoreScopedManager` | an overridden `objects` makes unscoped queries run silently, on any model |
| `common.E002` | `_default_manager` is not `all_objects` | Django uses the default manager for unique validation, forms and the admin. A scope-guarded one makes `full_clean()` **raise**; a bespoke one silently changes what all three see. Declaring *any* local manager makes Django skip the inherited default entirely |
| `common.E003` | `store` is missing, nullable, or points somewhere other than `orgs.store` | the base declares it, so this catches the model that redeclares it wrongly |
| `common.E004` | any traversable relation reaching a store-scoped model, in either direction | a reverse accessor such as `category.products` hands out rows with **neither** the store filter nor the soft-delete filter |
| `common.E005` | any uniqueness shape other than the three legal ones | a constraint naming no tenant column turns `full_clean()` into a cross-tenant existence oracle — "this code already exists" for a row the caller cannot see |
| `common.E006` | a non-store-scoped model pointing at a store-scoped one | only a store-scoped source has a store for `save()` to compare against |
| `common.E100` | a production database named `test_*` | the append-only TRUNCATE guard waives itself for such names, so a mis-named production database would inherit the waiver silently. Gated on `ENFORCE_NON_TEST_DATABASE`, set only by `prod.py`. Pure string inspection — opens no connection |

**E005's three legal shapes**, because this is the one with real subtlety:

1. **Per store** (the default) — names `store`, and the condition insists **at AND level** on
   `deleted_at IS NULL`.
2. **Per organization, across its stores** — names `org`, **excludes** `store`, still insists
   on live rows, and **its name ends in `_per_org`**. The suffix is required rather than
   inferred, because a constraint on `(org, name)` is exactly what a developer types by habit
   when they meant per store, and inferring intent turns that typo into a constraint that
   rejects a legitimate row in the second shop, in production, months later. The name is also
   what an operator reads out of an `IntegrityError`.
3. **A composite-FK target** — exactly `(id, org)` or `(id, store)`, named
   `<table>_id_org_uniq` / `_id_store_uniq`, and **exempt from the live-rows rule** because
   PostgreSQL refuses a partial unique index as a foreign-key target. Conversely a
   *conditioned* one is an **error**, not a tolerated oddity: it disarms the constraint's only
   purpose and fails far away, inside a later migration at `ADD FOREIGN KEY` time.

The single exemption to "no non-primary-key `unique=True`" is `is_public_id_surrogate()`,
deliberately narrow on all five counts: the **name** must be `public_id` (a `token` column with
the same type and the same `editable=False` is still the oracle E005 exists for), the **type**
must be a `UUIDField`, it must be `editable=False` (so no `ModelForm` can report "already
taken" for another tenant's value, and nothing can rewrite an identifier a URL already names),
it must have a default, and it must be `NOT NULL` (PostgreSQL treats NULLs as distinct in a
unique index, so a nullable identifier column is unique in name only). E005 also rejects a
`Meta` constraint on `public_id`, so there is exactly one way to declare the surrogate.

E005 deliberately does **not** adjudicate whether a given business key *should* be per store or
per organization. That is a business question; a check that guesses intent would be wrong in
both directions.

**E005 is already correct for the day the `org` column lands**, and this is worth knowing
because it looks like a gap: classification is purely over the column names the *constraint
references*, never over the model's field set. Today shape 2 and the `(id, org)` target are
simply unreachable on a real model, and if someone writes one, Django's own `models.E012`
reports the missing field. The day the column arrives, both shapes become reachable with
**zero change** in `common/checks.py`.

> **`common.E012` does not exist.** Verified: `common/checks.py` emits exactly `E001`–`E006`
> and `E100`. The `E012` you may see referenced is **Django's** `models.E012` — the built-in
> that reports a constraint naming a nonexistent field, quoted in E005's docstring as the
> mechanism covering the pre-`org` case. There is no `common.E012` and no plan for one.

### Designed, absent — verified

None of these exist. `E011` is unassigned by any current spec.

| id | Rule | Source |
| --- | --- | --- |
| `common.E007` | every `Index` on a store-scoped model leads with `org` or `store` | schema plan §D.3 |
| `common.E008` | every first-party model has a `public_id` | schema plan §C.1 |
| `common.E009` | `org` is a non-nullable FK to `orgs.Organization` with `related_name="+"` | tenancy design §D.2 |
| `common.E010` | `PRESETS` is exhaustive — every catalog code is in a preset or in a declared `UNASSIGNED` set | ADR 0011 rule 3 / tenancy design §I.9 |
| `common.E101` | the `default` connection's role owns no application table, has no `BYPASSRLS` or `SUPERUSER`, and holds no `TRUNCATE` | ADR 0009 amendment / threat model §1.6 |
| `common.E102` | every table with an org-bearing column has `relrowsecurity = t` and at least one policy | threat model §1.6 / schema plan §F.5 |
| `common.E200` | every model holding personal data declares an `ERASURE_PLAN` | privacy ruling C8 |

E101 and E102 **open a connection**, unlike E100, so they run from the entrypoint's pre-boot
step with an explicit alias, or as a dedicated command. E101 should also assert that
`raporo_app` holds no `UPDATE`/`DELETE` on `audit_auditlog` and no write privilege on
`django_migrations` — phase 2 is a step, and steps get skipped; this is what makes a skipped one
loud at boot rather than at the tampering incident.

---

## 7. Testing and verification posture

**574 tests, and the number is a fact about the suite, not a target.** `pytest.ini` pins
`--ds=config.settings.test`, so container environment variables cannot change which settings
the suite runs on.

### The standing rule

> **A guard is unverified until you have watched it refuse something.**

Recorded four times in the ledger, because four controls in slice 1 were present, correctly
named, documented — and never executed. Three concrete obligations:

1. **Drive a check through the registry**, never by calling the function. `E100`'s tests passed
   while the check was inert under the wrong tag.
2. **Precede every "returns nothing" assertion with a positive one.** An empty result and a
   working guard are otherwise indistinguishable — and under RLS, "empty" is the *designed*
   failure mode, so this is not pedantry.
3. **Mutate the guard and watch the suite go red.** Reverting the phone-canonicalisation
   wiring turns 41 tests red; that is what proves the invariant test bites on the *wiring* and
   not just on the function. Report the mutation, its result, and the restoration.

### The SQL stability contract — `tests/test_db_stability.py`

Django tracks a migration by **name**, never by content. A migration that imports SQL from
`common/db.py` therefore applies whatever text is current when a *fresh* database runs it,
while a database migrated last year keeps the body it installed then. Editing shipped SQL
silently forks the two. Two complementary mechanisms:

- **Structural.** Nothing a migration may import is unversioned —
  `CREATE_APPEND_ONLY_FUNCTION_V1`, `append_only_triggers_v1`. There is no unversioned alias to
  grab, so an in-place edit is visibly the wrong move, and a test keeps that true for helpers
  added later.
- **Tripwire.** A SHA-256 of every versioned string **and of every `RunSQL` statement in every
  migration** is pinned. Any edit — even whitespace — fails the suite with instructions. The
  discovery walks `MigrationLoader`, so it is "enforced by construction, cannot be made green
  by omission".

**The rule: never edit a `_V1` constant.** Add a `_V2` alongside it, add its hash to the pin
test, and add a **new** migration that runs it. Three ways to get a V2 wrong, all of which the
module docstring spells out and all of which have a cost:

- **Order it explicitly. Django will not.** A V2 shipped in a new app has no implicit edge to
  `audit/0002`, so it can be applied *before* it — leaving fresh databases on the V1 body while
  already-migrated ones end on V2: precisely the fork the mechanism exists to prevent, arriving
  through the one door left open.
- **A V2's `reverse_sql` is the `_V1` body, never a `DROP`.** Dropping the shared function is
  refused by Postgres for as long as one guarded table still has a trigger on it, so a DROP
  reverse makes the migration unreversible the moment there is more than one guarded table.
- **A migration guarding a *new* table carries only the trigger operation** and depends on
  `("audit", "0002_append_only_trigger")`. Copying `audit/0002` wholesale installs a second
  lifecycle for one shared object and makes the new migration irreversible as soon as two
  tables are guarded.

### The suite's shape

`tests/testapp` holds concrete stand-ins for the abstract bases and is installed **only** by
`config.settings.test`, so its tables never reach a real database and the migration-drift check
stays clean under `config.settings.dev`. **Both settings modules must be drift-free** — the
check is run under each.

Designed and absent: `tests/test_tenancy_matrix.py`, the cross-tenant denial matrix,
**generated from the model registry and never enumerated**, so a model added in slice 2
acquires coverage without anyone remembering. Its anti-rot mechanism is a `TENANCY_FACTORIES`
registry plus a premise test that fails when a concrete store-scoped model has no factory,
printing the label and the signature to paste. **Allowed cases are in the matrix too** — the
no-context `Organization` INSERT and `all_objects` returning tombstones — because a matrix
listing only refusals invites someone to "fix" a legitimate path.

### Known test-side residuals

- `grant_runtime_privileges` has **no test**, deliberately (adding one would have moved a
  pinned suite count). The RLS task owes it four assertions.
- `loaddata` can **reissue** a `public_id`: a fixture that omits the field and names an
  existing pk performs an UPDATE with the Python default re-evaluated, breaking "immutable,
  never reissued". Test-only today, and `tests/fixtures/` is exactly where it would bite. Fix:
  fixtures carry `public_id` explicitly, plus a re-load test.
- `public_id` immutability is **documented, not enforced** — nothing refuses assigning a new
  value and saving. `editable=False` covers forms and admin only.
- `common/selectors.py::get_scoped` does not exist, so `public_id` is a URL key **with no
  reader**. ADR 0010's "not an authorization control" sentence is a promise about unwritten
  code until that selector lands.

---

## 8. Open questions and unresolved contradictions

Named here so they are decided rather than discovered.

1. **Connection strategy — the two specs contradict each other. Not arbitrated.**
   [Tenancy design §D.3](superpowers/specs/2026-09-02-tenancy-hardening-design.md) says
   `CONN_MAX_AGE` from the environment with `CONN_HEALTH_CHECKS = True`, and psycopg pooling
   "deliberately **not** enabled" — one connection lifecycle to reason about in the round that
   introduces the GUC. [Schema plan §F.1](superpowers/specs/2026-09-02-schema-hardening-plan.md)
   says the opposite: `CONN_MAX_AGE = 0` with `OPTIONS["pool"]`, `psycopg[binary,pool]`, and a
   threaded gunicorn worker — bounded connections, an acquire timeout, recycling, and pool
   statistics. Today neither is configured (implicit `CONN_MAX_AGE = 0`; the pool extra is not
   installed). Either is safe **because of `SET LOCAL`**.
2. **`ATOMIC_REQUESTS`.** Schema plan §F.2 assumes `True` and flags it as a dependency;
   tenancy design §C.3 says it stays `False` because `tenant()` owns the transaction. The
   tenancy design owns `common/tenancy.py`, so **`False` is the coherent reading** — one
   mechanism, not two — but it is stated here rather than assumed.
3. **The `orgs_membership` RLS bootstrap. Unresolved, and it will lock everyone out.** Threat
   model §4.4 names it: login resolves which org a user belongs to *before* any context exists,
   so a plain `org_id = raporo_current_org_id()` policy on `orgs_membership` returns zero rows
   and the context can never be established. Its recommendation is a `SECURITY DEFINER`
   function owned by `raporo_owner` returning ids only, with a pinned `search_path` (a
   `SECURITY DEFINER` function without one is a privilege-escalation primitive). **The tenancy
   design's §C.3 middleware performs exactly that read and does not mention the problem.** This
   must be resolved before RLS ships.
4. **`Store` has no `timezone` field.** Only `Organization` has one, and the period engine
   computes every boundary in `Organization.timezone` (architecture spec §6) — so the schema and
   the engine agree today and nothing is broken. Rwanda is a single zone with no DST, so the
   question is dormant. It becomes real the moment one organization runs stores in two zones, at
   which point "which zone does a period start in" is a **correctness** question for
   `data-reporting-engineer`, not a modelling preference. Recorded as an open modelling
   question, **not** as a capability.
5. **`public_id` immutability is unenforced** (§7). An `update_fields` guard or a trigger
   belongs with the service layer.
6. **The `PRESETS["Manager"]` self-promotion path** — `member.manage` without `role.manage`
   lets a Manager move a member (including themselves) into the Owner role. `common.E010` does
   **not** close it. Routed to `security-engineer`; ADR 0011 raises its severity because the
   Owner role will carry `store.access_all`.
7. **No CI exists.** `.github/workflows/` is absent; every green result in this repo has been
   produced by hand.
8. **Two `.env` items are blocked on Elvis.** `RAPORO_APP_PASSWORD`,
   `RAPORO_MIGRATE_PASSWORD` and `RAPORO_BACKUP_PASSWORD` must be present or the stack will not
   start, and `DJANGO_MEDIA_ROOT` is still missing from `.env.example`; agents are denied writes
   to `.env*`.

---

## 9. Verification log

Every "BUILT" and "absent" claim above was measured on 2026-09-03 against the running dev
stack, read-only. Queries and output:

```
pg_policies                                       -> 0 rows
tables in `public` with relrowsecurity            -> 0
functions in `public` named raporo*               -> raporo_append_only  (only one;
                                                     raporo_current_org_id absent)
pg_roles LIKE 'raporo%'  (super / bypassrls)      -> raporo        t / t
                                                     raporo_app    f / f
                                                     raporo_backup f / t
                                                     raporo_owner  f / f
pg_auth_members rows involving a raporo role      -> 0   (the owner is not a member of
                                                          raporo_app; that grant is absent)
pg_constraint LIKE '%same_org_fk%'                -> 4 rows, all DEFERRABLE
pg_trigger WHERE NOT tgisinternal                 -> audit_auditlog_append_only
                                                     audit_auditlog_no_truncate
base tables in `public`                           -> 16 (7 first-party + 2 M2M + Django's)
```

Django-side introspection inside the web container:

```
python 3.14.7 | django 6.1
pg 180006 | supports_uuid7_function True | supports_virtual_generated_columns True
StoreScopedModel fields: ['public_id', 'created_at', 'updated_at', 'created_by',
                          'updated_by', 'deleted_at', 'deleted_by', 'store']
concrete StoreScopedModel subclasses: []
common.E ids emitted by common/checks.py:
  ['common.E001', 'common.E002', 'common.E003', 'common.E004', 'common.E005',
   'common.E006', 'common.E100']
pytest --collect-only -> 574 tests collected
```

Absent in the source tree, each checked individually:

```
common/tenancy.py · common/middleware.py · common/selectors.py
apps/orgs/services (and services.py) · apps/accounts/services (and services.py)
tests/test_tenancy_matrix.py · tests/test_tenancy_context.py
templates/base.html · locale/rw · locale/fr · compose.prod.yaml
.github/workflows
"access_all"  -> 0 hits across apps/ common/ config/ tests/
```

---

## 10. Index of ADRs

| ADR | Decides |
| --- | --- |
| [0001](adr/0001-portable-ai-team-in-repo.md) | The AI team, skills and rules live in the repo, project-scoped; machine tools come from `scripts/setup.sh`. |
| [0002](adr/0002-tooling-selection.md) | claude-mem is the single memory system; design skills are vendored rather than installed as plugins; no OmniRoute. |
| [0003](adr/0003-browser-automation-playwright-cli.md) | Browser automation via the Playwright **CLI**, not the MCP server — roughly 4× fewer tokens for the same task. |
| [0004](adr/0004-senior-team-roster-and-pipeline.md) | Nineteen specialist roles and a five-phase pipeline; every production concern has a named owner and a blocking checkpoint. |
| [0005](adr/0005-rules-live-with-the-owning-role.md) | Engineering rules live inside the agent that enforces them, so they load only when that role runs. |
| [0006](adr/0006-stack-django-postgres-react.md) | Django 6.1 + PostgreSQL + Docker everywhere; Redis and Celery only on a real need. **Its React/DRF half is superseded by 0007** — mind the filename. |
| [0007](adr/0007-frontend-django-templates-htmx.md) | The frontend is Django templates + HTMX; **all business logic in a service layer, views thin**, which is what keeps a future DRF API cheap. |
| [0008](adr/0008-denormalised-organization-on-store-scoped-rows.md) | Store-scoped rows carry their organization, tied to the store by a composite FK. Proposed; the column is **not built**. Note it says `organization` — the settled name is **`org`**. |
| [0009](adr/0009-row-level-security-for-organization-isolation.md) | PostgreSQL RLS enforces **organization** isolation; the application enforces **store** isolation. **Read the amendment:** `ENABLE` without `FORCE`, no `BYPASSRLS`, `common.E101` instead. Nothing is built. |
| [0010](adr/0010-uuidv7-public-identifiers.md) | Every row carries a UUIDv7 public identifier separate from its bigint pk. **Read the amendment:** the field is **`public_id`** on **`PublicIdModel`**, with a **Python `default=uuid.uuid7`** and **`unique=True` on the field** — not `uuid`, not `IdentifiedModel`, not a database default, not a named `UniqueConstraint`. **Built.** |
| [0011](adr/0011-org-wide-store-access-is-a-permission-code.md) | Org-wide store access is the permission code **`store.access_all`**, resolved in exactly one function — never a role-name check. Proposed; nothing is built. |

## 11. Index of design specs

All under `docs/superpowers/specs/`. Later documents and every "Amendment" section correct
earlier text — let them win.

| Spec | What it settles |
| --- | --- |
| [2026-09-01-raporo-architecture-and-schema-design.md](superpowers/specs/2026-09-01-raporo-architecture-and-schema-design.md) (122 ln) | The original whole-product architecture, app layout, the full slice-2-to-6 schema, auth flows, the period engine and the testing strategy. Partly superseded on tenancy and identifiers; **still the source of truth for the slice 2–6 table shapes, the branding chain, the FX rule and the period boundaries.** |
| [2026-09-02-tenancy-hardening-design.md](superpowers/specs/2026-09-02-tenancy-hardening-design.md) (1834 ln) | The tenancy round. §A the guard changes and `ScopePin`; §B the RLS/application division; §C the tenant context and `SET LOCAL`; §D checks, indexes, export, the denial matrix; §E identifiers; §F sequencing; §I the permitted store set and the canonical fixture; **§J revision 2, which corrects §A–§H. Read §I then §J first.** |
| [2026-09-02-schema-hardening-plan.md](superpowers/specs/2026-09-02-schema-hardening-plan.md) (1708 ln) | §A the `org` column (naming ruling **`org`**, and `db_constraint=False` measured); §B the kinded `common.E005`; §C the identifier; §D the index set (−19, +6, 7 deferred); §E migration safety and lock rules; §F connections and timeouts; §J one organization per user. |
| [2026-09-02-rls-threat-model.md](superpowers/specs/2026-09-02-rls-threat-model.md) (1268 ln) | The role model (§1, now built); the `NULLIF` fail-closed defect (§5); the connection-reuse leak proven with pids, PgBouncer included (§6); policy shapes and which tables get one (§4); **§7, what RLS does and does not cover** — the sentence quoted in §4.10; findings R1–R11. |
| [2026-09-02-privacy-law-058-2021-ruling.md](superpowers/specs/2026-09-02-privacy-law-058-2021-ruling.md) (432 ln) | Rwanda Law No. 058/2021 against the slice-1 model. Verdict: proceed, conditional on C1 + C4. The Critical finding P-1 — the audit trail was **not** PII-free — and the standing `changes` policy C2–C7 now implemented in `apps/audit/services.py`. Erasure operates on **referents**, not on the trail. |

Two more documents worth knowing:
[`superpowers/plans/2026-09-01-slice-1-foundation.md`](superpowers/plans/2026-09-01-slice-1-foundation.md)
is the 14-task slice-1 plan, and
[`superpowers/slice-1-workspace/LEDGER.md`](superpowers/slice-1-workspace/LEDGER.md) (776 ln)
records **every ruling with its cost-if-wrong**. Read the ledger before re-deciding anything;
it will stop you re-litigating decisions that were expensive the first time.
