# 0008. Store-scoped rows carry their organization, tied to the store by a composite foreign key

Date: 2026-09-02
Status: Proposed

## Context

Invariant #1 — a business row belongs to exactly one store, and a query may never span two
organizations — is Raporo's release-blocking correctness property. Today a store-scoped row
carries only `store`, and its organization is reached through `Store.org`. That single join
is deliberate (`apps/orgs/models.py` says so, and a test asserts no store-scoped model
declares an `org` field), and it works, but it has three costs that are now blocking.

First, the query guard cannot express "this row belongs to my organization" as a predicate:
`common/managers.py::_store_pks()` issues a `SELECT id, org_id FROM orgs_store` on every
widening set-operator merge to prove a store set does not span two organizations. The
database-engineer flagged that as an N+1 the moment a combinator appears in a loop. Second,
there is no single column for a database-level isolation policy to compare, so
organization isolation can only ever be enforced in Python. Third, indexes on business tables
cannot lead with the tenant, because the tenant is not on the row.

`StoreAccess` already solved the same problem the same way: it denormalises `org` so the
database can hold `(membership, org)` and `(store, org)` together and refuse a row that mixes
two organizations. Two gates blessed that pattern in slice 1. There are currently **no**
concrete `StoreScopedModel` subclasses in any production app, so the column is free today and
a data migration across four ledger tables tomorrow.

## Decision

We will add `organization` as a non-nullable, non-traversable (`related_name="+"`) foreign key
on `StoreScopedModel`, alongside `store`, and tie the two together with a composite foreign
key `(organization_id, store_id) → orgs_store (id, org_id)`, `DEFERRABLE INITIALLY IMMEDIATE`,
emitted by a versioned, hash-pinned helper `store_org_fk_v1()` in `common/db.py`. The pair can
therefore never disagree, and it is the database that says so.

The column is **derived, never asked for** — from the pinned queryset, a cached `Store`
instance, the active tenant context, or one lookup, in that order — the same pattern
`StoreAccess._derive_org` established. The query-layer pin becomes a pair
`(org_pk, store_pks)`, so the ownership resolution `_store_pks()` already performs is kept
instead of discarded; a widening merge then compares two integers instead of querying, which
removes a round trip from every set operator. Uniqueness rules generalise: `common.E005`
accepts a unique constraint rooted in `store` **or** `organization`, so "unique per
organization across its stores" becomes expressible. Every `Index` on a store-scoped model
must lead with `organization` or `store` (`common.E010`). No existing guard is removed:
E004, E006, the hidden-relation refusal, the compile-time pin check, the write guards and the
same-store FK check all stay exactly as they are.

Rejected: **keep reaching the organization through `Store.org`** — it is one fewer column, but
it makes database-enforced organization isolation impossible and leaves the per-combinator
query in place. **A generated column** — PostgreSQL generated columns cannot reference another
table, so it cannot be derived from `orgs_store` at all. **A trigger that fills the column** —
a second database object with its own stability contract, where a Python derivation plus the
composite FK already yields the same guarantee and a better error message.

## Consequences

Easier: organization isolation becomes a one-column predicate, which is what ADR 0009's
row-level security needs; every scoped query emits `organization_id = %s` first, which is the
shape tenant-leading composite indexes want; a widening set-operator merge costs no query;
per-organization uniqueness and a per-organization export both become a single `WHERE`.

Harder: one more column and one more constraint on every business table, and one more thing a
new model can get wrong — mitigated by declaring the column on the abstract base, so a
store-scoped model cannot be written without it, and by `common.E009` failing startup if a
subclass disarms it. `for_store(<int pk>)` now costs one resolution query where it cost none,
memoised per request. `update()` refuses `organization` outright, so re-homing is a service
operation and not a column write.

We are committed to: a store never changes organization once it has a single business row —
the composite key refuses the `UPDATE`, and that refusal is what licenses caching a store's
organization for the duration of a request. Revisit if a real requirement to move a store
between organizations appears; that would need an explicit, audited service and would
invalidate the caching, not the column.
