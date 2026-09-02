# 0009. PostgreSQL row-level security enforces organization isolation; the application enforces store isolation

Date: 2026-09-02
Status: Proposed

## Context

Invariant #1 is enforced entirely in Python today: a scope guard on `sql.Query`, refusals at
every set-operator seam, write guards on the queryset, a same-store check in `save()`, and six
startup checks. That machinery has survived roughly 110 attacks across three security-engineer
harness runs, and it took four fix rounds to get there. Five separate leaks were found *after*
the code was reviewed and believed correct, and four controls in the same slice were found
present, correctly named, documented and never executed.

The lesson is not that the code is bad — it is that a defence which lives only in the layer
that also does the work has no second opinion. A missing `WHERE`, a raw query, a
`_base_manager` path nobody enumerated, or the DRF endpoints ADR 0007 keeps the door open for,
each reintroduce the same class of leak, and only a test can tell us. Meanwhile ADR 0008 puts
the organization on the row, which gives the database a single column to compare.

Two constraints shape the answer. A user's permitted **store** set is per-membership and
varies within a single request: an owner can legitimately query store A, then store B, then
both. The **organization** is one value for the whole request. And with `CONN_MAX_AGE > 0` a
connection is reused, so a session-level `SET` of the tenant identifier would leak the
previous request's organization into the next one — a cross-tenant leak caused by the
isolation mechanism itself.

## Decision

We will enforce **organization** isolation in PostgreSQL with row-level security, and keep
**store** isolation in the application. Policies compare the row's organization to
`raporo_current_org_id()`, a `STABLE` helper reading
`NULLIF(current_setting('raporo.org_id', true), '')::bigint`, so an unset context is `NULL`,
every predicate is false, and the database fails closed to zero rows. Policies carry both
`USING` and `WITH CHECK`, cover all commands, and every table is `ENABLE`d **and** `FORCE`d.
ADR 0008's composite foreign key is what stops the row's organization and its store from
disagreeing, so the two mechanisms cannot diverge.

**Two enabling rules, both load-bearing.** First: the tenant identifier is set with
`SET LOCAL` inside a transaction, from exactly one place — `common/tenancy.py::tenant()`, a
context manager that opens the transaction, issues the parameterised `SET LOCAL` as its first
statement, sets a `ContextVar`, and resets both on exit. The request middleware, management
commands and any future Celery task are *callers* of that one door, not three implementations
of it. A source-scan test refuses a bare `SET` on a `raporo.*` GUC anywhere else in the tree.
Second: **the application process must stop connecting as the table owner and as a
superuser.** `FORCE ROW LEVEL SECURITY` subjects the owner to its own policies but nothing
subjects a superuser, so without a `raporo_app` (no ownership, no `BYPASSRLS`, no `TRUNCATE`)
and `raporo_migrator` (owner, `BYPASSRLS`) split, these policies are inert. Until that split
exists, this ADR is not implemented — it is decoration.

Rejected: **store-level RLS as well** — achievable via a second GUC holding the permitted
store ids as an array, but it relocates an authorization decision into a mechanism that can
only answer "zero rows" instead of `PermissionDenied`, and it breaks the one-value-per-request
property that makes `SET LOCAL` provably safe. **Application-only enforcement, as today** —
no second opinion, and the record shows five leaks found after review. **A policy that joins
`orgs_storeaccess`** — a correlated subquery per row, for the dimension whose breach is
intra-tenant rather than cross-tenant.

## Consequences

Easier: a forgotten pin, a raw query or a future DRF view returns nothing instead of another
tenant's rows; the per-tenant export can prove it cannot over-read; the runtime role loses
table ownership and `TRUNCATE`, which also closes the already-routed finding that a compromised
app process could wipe the append-only audit trail.

Harder: every tenant request is one transaction, so `statement_timeout` must sit below the
slowest view and `idle_in_transaction_session_timeout` above it, and no streaming response may
lazily query tenant data. Two database roles, and a second `DATABASES` alias in dev and test
(`TEST: {"MIRROR": "default"}`) so the suite can read as the unprivileged role. Registration
is an explicit carve-out: it creates the organization, so the `orgs_organization` INSERT
policy permits a no-context insert, and that appears in the denial matrix as an allowed case.
`accounts_user` gets no policy — it is a global namespace by product design — so its
protection stays the non-enumerating auth backend and the throttle.

We are committed to, and must say out loud: `raporo.org_id` is a custom GUC, so any role can
`SET` it. RLS defends against **our own bugs**, not against an attacker who already has
arbitrary SQL execution as the app role. That is the same limitation the append-only trigger
was accepted with, for the same reason, and it means an SQL-injection finding is not one notch
less severe because RLS exists. Revisit when a store's numbers become confidential *within* an
organization (separate legal entities, an investor portal) — then a store-id GUC is the
additive next step; or if PostgreSQL ceases to be the only datastore, at which point isolation
returns wholly to the application layer.
