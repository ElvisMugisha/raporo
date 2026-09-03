# Raporo — architecture essentials

Load-bearing decisions only, with where each is recorded. Read this before writing code;
read [ARCHITECTURE.md](ARCHITECTURE.md) when you need the reasoning. Verdicts are imperative
where they are rules you must obey.

## Verified state, 2026-09-03 (measured, not remembered)

- Python 3.14.7 · Django **6.1** (non-negotiable, ADR 0006) · PostgreSQL 18.0006 · 574 tests.
- Frontend is **Django templates + HTMX** (ADR 0007). ADR 0006's filename says "react"; its
  frontend half is superseded. Treat every React/SPA/day-one-DRF reference here as stale.
- **Built:** `common/` bases, the query-layer scope guard, `apps/accounts`, `apps/orgs`,
  `apps/audit` (append-only by trigger + privileges), four `*_same_org_fk` composite keys,
  `public_id` on seven tables, the SQL stability contract, `common.E100`, the three-role
  database split.
- **Not built. Do not write about these in the present tense:** row-level security
  (`pg_policies` = **0**; `raporo_current_org_id()` does not exist — the commit message of
  `9a697c3` claiming "RLS scaffolding" is **false**), the `org` column on `StoreScopedModel`,
  `common/tenancy.py`, `common/middleware.py`, `common/selectors.py`, any `services/` package,
  `tests/test_tenancy_matrix.py`, `templates/base.html`, `locale/rw`, `locale/fr`,
  `compose.prod.yaml`, any CI, `store.access_all`, the one-org-per-user constraint,
  `common.E007`–`E011`, `E101`, `E102`, `E200`.
- **`common.E012` does not exist.** Registered today: `common.E001`–`E006` and `E100`. The
  `E012` in circulation is **Django's** `models.E012`.
- `StoreScopedModel` has **zero concrete subclasses**, so the `org` column still needs no data
  migration. That stops being true the day slice 2 lands.

## Naming and shape — settled, do not re-decide

| Verdict | Why it is not reopenable | Recorded |
| --- | --- | --- |
| The tenancy column is **`org` / `org_id`**, never `organization` | four SHA-256-pinned `RunSQL` statements already contain the literal `REFERENCES orgs_store (id, org_id)`, and the stability contract forbids editing shipped pinned text | schema plan §A.1; ADR 0008 still says `organization` and is stale on this point |
| The identifier is **`public_id`** on **`PublicIdModel`**: `UUIDField(default=uuid.uuid7, editable=False, unique=True)` | measured — only `clean_fields` skips a `DatabaseDefault`, so a database default puts an expression object into a `WHERE` clause and into a DOM id | ADR 0010 **Amendment**: not `uuid`, not `IdentifiedModel`, not a db default, not a named `UniqueConstraint` |
| RLS is **`ENABLE`**, never `FORCE`; **no `BYPASSRLS` anywhere** | `FORCE` plus `BYPASSRLS` is self-cancelling, and dropping `BYPASSRLS` makes backfills silently affect 0 rows | ADR 0009 **Amendment**; the pre-amendment text is retained deliberately and is **not** current |
| The composite-FK generator is `same_org_fk_v1`; keys are named `<table>_store_same_org_fk` | identical in shape to the four already shipped | schema plan §A.5 |
| Check ids: E007 = org-leading index · E008 = `public_id` present · E009 = the `org` FK · E010 = exhaustive `PRESETS` | ADR 0008 assigns E010 to the index rule and is stale; §D.2 renumbered it | tenancy design §D.2, §J.2 |

## Tenancy — invariant #1 is the release gate

- **A business row belongs to exactly one store, and a query may never span two organizations.**
  A leak here is Critical and release-blocking. (ARCHITECTURE.md §4)
- **Query a store-scoped model only through `objects.for_store(store)` / `.for_stores([...])`.**
  Anything else refuses to compile. Use `all_objects` only for audits and data migrations.
- **Never build an ORM lookup key from request data.** The literal `+` query name of a hidden
  relation is refused, but the rule is broader than the guard.
- **Store ids from request data never reach `for_store()`.** A pin enforces a scope; it does
  not authorize one. Authorization is `require_store()` / `permitted_stores()` (ADR 0011).
- **Deny with 404, never 403.** `StoreNotPermitted` must not subclass `PermissionDenied`; a 403
  confirms the row exists. (ADR 0011)
- **Never check `role.name == "Owner"`.** Role names are user-editable, translatable and
  duplicable — a rename removes the override, a decoy grants it. Use `store.access_all`.
- **`PRESETS` must become exhaustive before `store.access_all` is added.** It is subtractive
  today, so the new code would reach Manager silently on the day it lands. (ADR 0011 rule 3)
- **Keep every application guard when RLS lands.** RLS is blind inside an organization; fix
  rounds 1–3 were almost all within-org findings and RLS would have caught none of them.
  (threat model §7)
- **RLS defends against our own bugs, not against SQL injection.** `raporo.org_id` is a custom
  GUC any role can set. An injection finding is not one notch less severe because RLS exists.
- **Set the tenant GUC with `SET LOCAL` inside a transaction, from `common/tenancy.py` only.**
  Measured: a bare `SET` leaked across two transactions on one backend, and across two clients
  through PgBouncer in transaction mode. Never promote it to a session-level set "to save a
  round trip" — that is the leak, caused by the isolation mechanism itself.
- **`org` is derived from `store`, never asked for**; non-nullable (MATCH SIMPLE skips a
  composite FK when either column is NULL); `related_name="+"` (or E004 fires);
  `db_constraint=False` (measured — a real FK there makes `create_store` block every sale in
  the org), and `create_store` must use `select_for_update(no_key=True)`. (schema plan §A)
- **Every `Index` on a store-scoped model leads with `org` or `store`** (E007). `models.Index`
  names cap at 30 chars; `UniqueConstraint` names do not — do not "fix" a long constraint name.
- **`accounts_user` gets no RLS policy, deliberately** — auth resolves an identifier before any
  organization is known. Its defence is the non-enumerating backend plus throttling.

## System checks

- **A check earns its cost only when it enforces a rule over a *class* of models that new code
  can join unnoticed.** "This one model declares this one constraint" is a **test**, not a
  check. A boot-time data query is worse than useless. (schema plan §J.5)
- **Register under `Tags.security` or `Tags.models`, never `Tags.database`** — database-tagged
  checks are silently dropped unless an alias is passed. `E100` sat inert two rounds on this.
- **Drive every check's test through `django.core.checks.run_checks()`**, never by calling the
  function; a direct call passed while `E100` was broken.
- E005 allows exactly three uniqueness shapes: per-store (live-conditioned); per-org (`org`
  without `store`, live-conditioned, name ending `_per_org`); and a composite-FK target
  (`(id, org)` / `(id, store)`, unconditional — a partial unique index cannot back an FK).
  Everything else is a startup error. `public_id` is the one exemption.

## Migrations and SQL

- **Never edit a `_V1` constant or any shipped `RunSQL` text.** Add a `_V2`, add its SHA-256 to
  `tests/test_db_stability.py`, and add a **new** migration. (`common/db.py` docstring)
- **A V2 must depend on every migration that installed an earlier version**, and its
  `reverse_sql` restores the `_V1` body — never a `DROP`.
- **A migration guarding a new table carries only the trigger operation**, depending on
  `("audit", "0002_append_only_trigger")`. Copying `audit/0002` makes it irreversible.
- **Composite keys are `DEFERRABLE INITIALLY IMMEDIATE`**, never `INITIALLY DEFERRED`: deferred
  violations surface at COMMIT, which never happens inside a test transaction, so tests pass
  vacuously.
- **The migration-drift check must be clean under both `config.settings.dev` and
  `config.settings.test`.**

## Where code goes

- **`common/`** if it constrains a *class* of models. **`apps/<context>/`** if it is one
  context's fact. **`config/`** for wiring. **`tests/`** flat, named for behaviour.
- **All business logic in `apps/<app>/services/`; views stay thin** (ADR 0007). A service takes
  resolved domain objects — never a request, form or `QueryDict` — owns the transaction, the
  permission check and the audit write, and returns a domain result. That is what makes a
  future DRF API weeks rather than a rewrite. A view that writes a model directly fails review.
- Import direction: `accounts → orgs → catalog → inventory → sales → money`; `audit` imports
  none of them; `reporting` reads all and is imported by none. Inside `common/`:
  `tenancy ← managers ← models ← checks`, so `common/tenancy.py` imports nothing else there.
- **Audit `changes`: field names and IDs for anything personal, values for anything else.**
  Do not put prose in `changes` — the table refuses UPDATE and DELETE, so there is no fix
  later. (privacy ruling C2; `apps/audit/services.py`)
- **Do not create empty modules or directories.** The Dockerfile copies path by path, an empty
  `.py` is lint-clean, and an empty tree is how this project has shipped controls that never ran.

## Testing

- **A guard is unverified until you have watched it refuse something.** Recorded four times;
  four slice-1 controls were present, named, documented and never executed.
- **Precede every "returns nothing" assertion with a positive one.** Under RLS, empty is the
  designed failure mode, so an empty result and a working guard are indistinguishable.
- **Mutate the guard, show the suite go red, restore it, show it green** — and report all three.

## Unarbitrated — do not silently pick

1. **Connection strategy.** Tenancy design §D.3 wants `CONN_MAX_AGE` and no pool; schema plan
   §F.1 wants `CONN_MAX_AGE = 0` plus `psycopg[pool]`. Neither is configured today.
2. **`ATOMIC_REQUESTS`.** Schema plan §F.2 assumes `True`; tenancy design §C.3 says `False`
   because `tenant()` owns the transaction. `False` is the coherent reading.
3. **The `orgs_membership` RLS bootstrap.** Membership is read *before* any context exists, so
   a plain org policy on that table locks every user out. Threat model §4.4 names it; the
   tenancy design's middleware does the same read and does not. **Resolve before RLS ships.**
4. **`Store` has no `timezone`.** Only `Organization` has one and the period engine uses it, so
   nothing is broken. An open modelling question, not a capability.

## Pointers

ADRs `docs/adr/0001`–`0011` · specs `docs/superpowers/specs/` (five documents; §J and every
"Amendment" corrects earlier text — let them win) · `docs/superpowers/slice-1-workspace/LEDGER.md`
records every ruling with its cost-if-wrong · `docs/ROADMAP.md` is the living tracker.
