# 0010. Every row carries a UUIDv7 public identifier, separate from its bigint primary key

Date: 2026-09-02
Status: Proposed

## Context

Raporo's UI is server-rendered Django templates with HTMX fragments (ADR 0007), which means
**every row a user can act on is addressed by a URL** — a fragment endpoint, a detail page, an
`hx-target`. Today the only identifiers we have are sequential `BigAutoField` primary keys and
`Organization.slug`. Sequential ids in URLs give any authenticated user a working enumeration
oracle: the difference between "id 400 is not yours" and "id 4000 does not exist" leaks the
size and growth rate of every competitor on the platform, and a cross-tenant identifier
reference is the shape three of slice 1's reproduced leaks took.

The slug is not a substitute. `orgs_organization_unique_live_slug` is conditioned on live rows
by design — there is a test named for the fact that a soft-deleted organization releases its
slug — so it is neither stable nor unique over time, and it is mutable and user-chosen. `Store`
has no slug at all.

Django 6.1 ships `UUID4`/`UUID7` database functions, and Python 3.14 (which this project already
runs) ships `uuid.uuid7()` in the standard library. UUIDv7 is time-ordered, so unlike UUIDv4 it
does not scatter index inserts across the whole keyspace.

## Decision

We will add a `uuid` column to a new abstract base `common.models.IdentifiedModel`, mixed into
`SoftDeleteModel` (which reaches every organization, store, role, membership and every
store-scoped row), and explicitly into `AuditLog` and `accounts.User`. It is
`UUIDField(default=uuid.uuid7, editable=False)`, non-nullable, with a **global, unconditional**
`UniqueConstraint(fields=["uuid"], name="%(app_label)s_%(class)s_uuid_uniq")` and no separate
index — on PostgreSQL the constraint is already a unique B-tree index, and a second one would
cost a write per insert. `common.E007` and `common.E008` fail startup if the field or the
constraint is missing or mis-declared, including the case where a subclass declares its own
`Meta` and silently loses the inherited constraint.

The `uuid` is the **only** identifier that crosses the process boundary: it appears in URLs
(`<uuid:sale_uuid>`), in DOM ids and in `hx-*` attributes. The bigint primary key stays, stays
internal, and stays the target of the composite foreign keys. The organization does not appear
in URLs at all — it comes from the tenant context — so paths read
`/stores/<store_uuid>/sales/<sale_uuid>/`. And the identifier is **not** an authorization
control: views never call `.get(uuid=…)` but go through one selector that applies the store
pin, so a valid uuid belonging to another tenant is a 404 with no oracle in it.

Rejected: **`db_default=UUID7()`** — it would tie the identifier to the PostgreSQL 18 bump
(Django raises `NotSupportedError` below it), and a `db_default` field's Python value before
insert is a `DatabaseDefault` sentinel, which `Model.validate_unique()` and
`UniqueConstraint.validate()` do not skip, so `full_clean()` compiles a nonsense
`WHERE uuid = UUIDV7()` lookup — and an unsaved object would carry a sentinel where a template
expects an identifier. **UUID as the primary key** — 16 bytes in every foreign key, worse join
locality, and it would force rewriting the `(id, org_id)` composite-FK targets and the
integer-typed scope machinery. **UUIDv4** — random inserts fragment the index for no gain over
a time-ordered value we get from the standard library. **Hashids / signed opaque ids** — a
secret to manage and rotate, and a decoding step in every request, to obtain what a UUID gives
for free. **Routing on `Organization.slug`** — mutable, released on soft delete, and it puts the
tenant's name in every URL.

## Consequences

Easier: no enumeration surface in any URL; a stable identifier that survives soft delete, so a
bookmarked link and an audit reference keep resolving to the same row for ever; HTMX fragment
targets and `HX-Push-Url` values become derivable from the object; the per-tenant export is
identifier-portable rather than full of internal ids; `audit.record` can record a
`target_uuid` an audit screen can link to.

Harder: two identifiers per row, and one rule to hold — the primary key never leaves the
process and the uuid never enters a `WHERE` clause without a pin. 16 bytes and a unique index
on every table. "No primary key in the DOM" is a discipline, so it becomes a Task-8 acceptance
criterion with a per-screen test rather than a note, because a control that lives in a note is
how a previous control in this project shipped inert.

We are committed to the uuid being immutable and never reissued, which is why its constraint is
deliberately *not* conditioned on live rows, the inverse of every other unique constraint in the
codebase. Revisit only to add `db_default=UUID7()` alongside the Python default once PostgreSQL
18 is confirmed everywhere and a bulk `COPY` loader or an external writer exists — Django
prefers the Python default when both are set, so that change is additive and covers only the
paths that bypass the ORM.
