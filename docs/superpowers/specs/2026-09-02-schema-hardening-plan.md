# Schema, migration and indexing plan — tenancy-hardening round

Date: 2026-09-02 · Owner: `database-engineer` · Branch: `feat/slice-1-foundation`
Status: plan only. No code, model or migration was changed producing this document.

Binding inputs: `docs/superpowers/slice-1-workspace/LEDGER.md` (every ruling below that
says "measured" or "ruled" is recorded there), `docs/PRODUCT.md`,
`docs/superpowers/specs/2026-09-01-raporo-architecture-and-schema-design.md`, ADR 0006/0007.

Parallel documents this one defers to, with my assumption stated where I could not wait:

| Owner | Owns | My assumption |
| --- | --- | --- |
| `security-engineer` | RLS policy text, GUC name, who sets it | GUC is `raporo.org_id`; set with `SET LOCAL` inside `transaction.atomic()`; `ATOMIC_REQUESTS = True` or a middleware inside it |
| `architect` | app layout for slices 2–5, whether `common` may own migrations | `common` stays migration-free (its docstring says so); shared database objects are owned by the first app in the graph that needs them, the `audit/0002` precedent |
| `devops-engineer` | Python 3.14 + PostgreSQL 18, role provisioning, gunicorn shape | PG18 lands; if it does not, §C.5 is the fallback. VPS is 2 vCPU / 4 GB |
| `privacy-compliance` | Law 058/2021 | `public_id` is a random surrogate, not personal data; no ruling needed for it |

Everything marked **measured** was run against `postgres:18.6` in an isolated compose
project (`-p raporo-plan-db`, torn down with `-v`). Everything marked **verified** was
read out of the installed `Django==6.1` source in `env/`.

---

## 0. The scheduling fact that reshapes this round

**There is no concrete `StoreScopedModel` subclass anywhere in the shipped code.**
`grep -rn StoreScopedModel apps/ common/ tests/` returns the abstract base, the checks,
and nothing else outside `tests/testapp/` and `tests/test_common_checks.py` — and
`tests/testapp` is installed by `config.settings.test` only and is never migrated into a
real database.

Consequences, and they are the spine of this plan:

1. **§A (the `org` column, its composite FK, its index) produces no migration in this
   round.** It is a change to `common/models.py`, `common/managers.py`,
   `common/checks.py` and the tests. The columns, constraints and indexes land inside
   `catalog/0001_initial`, `inventory/0001_initial`, `sales/0001_initial`,
   `money/0001_initial` in slice 2, as `CreateModel` fields on fresh empty tables — the
   cheapest possible way to land any of it.
2. **What *does* migrate now** is `public_id` on the six existing tables, the index
   trimming (§D.1), and the RLS scaffolding on the existing tables. All on empty tables.
3. So the round is: **get the base, the checks and the pinned helper right, because
   slice 2 multiplies them by eight tables.** Every mistake here ships eight times.

Do not read this as "less work". Read it as: the leverage is entirely in `common/` and
in `tests/`, and the migration surface is small enough to be reviewed line by line.

---

## A. The `org` column on `StoreScopedModel`

### A.1 The naming call: `org`, not `organization`

**Decision: `org`. The field is `org`, the column is `org_id`.**

The five existing models that carry an organization pointer — `Store`, `Role`,
`Membership`, `StoreAccess`, `AuditLog` — all call it `org`. So do the constraint names
(`orgs_store_unique_live_name_per_org`), the app label (`orgs`), the module constant
(`MAX_STORES_PER_ORG`), the composite-key names (`*_same_org_fk`), and
`common/managers.py::_store_pks`, which reads `values_list("pk", "org_id")`.

The deciding argument is not head-count, it is the **frozen SQL**. Four `RunSQL`
statements, each pinned by SHA-256 in `tests/test_db_stability.py`, contain the literal
text `REFERENCES orgs_store (id, org_id)`. Choosing `organization` for the new column
does not rename those — renaming `orgs_store.org_id` would mean editing four already
shipped, already pinned statements, which the stability contract forbids outright. So
`organization` buys a database in which the same concept is `org_id` on five tables and
`organization_id` on fifteen, and in which every new composite FK reads:

```sql
FOREIGN KEY (organization_id, store_id) REFERENCES orgs_store (id, org_id)
```

A constraint whose two sides name the same column differently reads like a bug on every
future review, and "which one is it on this table?" is precisely the class of mistake a
denormalised tenant column exists to eliminate. With `org`:

```sql
FOREIGN KEY (store_id, org_id) REFERENCES orgs_store (id, org_id)
```

— identical on both sides, identical in shape to the four already shipped, and it also
matches the RLS GUC name (`raporo.org_id`), which makes the policy text read directly.

The only argument for `organization` is that it is the longer word. That loses to four
SHA-256 pins.

Test to add: `tests/test_common_bases.py::test_no_store_scoped_model_carries_its_own_org_pointer`
(line 125) is **inverted**, not deleted — it becomes
`test_every_store_scoped_model_carries_its_org_pointer`, asserting for each concrete
subclass that `_meta.get_field("org")` exists, targets `orgs.Organization`, is
non-nullable, and that no model declares a field named `organization` (so the two
spellings cannot co-exist). The docstring paragraph in `apps/orgs/models.py` lines 3–9
("Business data never carries an organization pointer of its own") is now false and must
be rewritten in the same change; leaving it is how a reviewer three months from now
"fixes" the column back out.

### A.2 Field definition

On `common/models.py::StoreScopedModel`, declared immediately above `store`:

```python
org = models.ForeignKey(
    "orgs.Organization",
    verbose_name=_("organization"),
    on_delete=models.PROTECT,
    related_name="+",
    editable=False,
    db_index=False,
    db_constraint=False,
)
```

Each keyword is load-bearing:

- **`on_delete=models.PROTECT`** — matches `store` and every other FK in the repo. Hard
  delete is structurally forbidden (`HardDeleteForbidden`), so PROTECT never fires; it
  is the honest declaration that nothing here cascades.
- **`related_name="+"`** — mandatory. Without it `common.E004` fails startup, correctly:
  `organization.product_set` would hand out rows with neither the store filter nor the
  soft-delete filter, which is exactly the leak E004 exists for.
- **`editable=False`** — the value is derived from `store`, never entered. Keeps it out
  of every `ModelForm` and out of the admin, following the `StoreAccess._derive_org`
  precedent.
- **NOT nullable.** `store` is non-null, so `org` is always derivable. A nullable
  denormalised tenant column is a hole in the RLS policy *and* in the composite FK:
  Postgres MATCH SIMPLE leaves a composite FK **unchecked** when either column is NULL.
  That is deliberate and correct for `AuditLog` (a system row has neither org nor store);
  on a business table it is a bypass.
- **`db_index=False`** — no single-column index. See §D.2: a standalone `org_id` btree is
  redundant with every org-leading composite, and it is a write cost on the hottest
  tables in the product. `common.E007` (§D.3) guarantees each table has at least one
  org-leading index, so RLS never falls back to a sequential scan.
- **`db_constraint=False`** — this is the one that needs the measurement.

### A.3 Why `db_constraint=False` on `org` (measured)

The composite FK `(store_id, org_id) → orgs_store (id, org_id)` already proves the
organization exists: `orgs_store.org_id` carries its own FK to `orgs_organization`, so
validity is transitive. A separate `org_id → orgs_organization (id)` constraint adds no
guarantee — and it adds a **second row lock on the organization row for every single
insert into every store-scoped table.**

Measured on PG 18.6. A child table with a plain FK to a parent; session A holds
`SELECT ... FROM parent WHERE id=1 FOR UPDATE`; session B inserts a child row with
`lock_timeout = 2s`:

```
ERROR:  canceling statement due to lock timeout
CONTEXT:  while locking tuple (0,1) in relation "lk_org"
SQL statement "SELECT 1 FROM ONLY "public"."lk_org" x WHERE "id" OPERATOR(pg_catalog.=) $1 FOR KEY SHARE OF x"
```

With session A holding `FOR NO KEY UPDATE` instead, the same insert succeeded.

Now read that against the architecture spec, §4-orgs: *"1–5 stores enforced in
`create_store` service under `SELECT … FOR UPDATE` on the org row."* With a real
`org_id → orgs_organization` FK on every store-scoped table, **`create_store` would block
every sale, every stock movement and every payment in that organization for the duration
of its transaction**, and vice versa. Dropping the redundant constraint removes the
interaction entirely.

Paired requirement, because `orgs_store.org_id`, `orgs_role.org_id`,
`orgs_membership.org_id` and `orgs_storeaccess.org_id` still carry real FKs to
`orgs_organization`: **`create_store` must use `select_for_update(no_key=True)`**
(verified present in Django 6.1 at `db/models/query.py:1740`). Belt and braces — either
fix alone works, both together mean neither can be undone by accident.

Test: a `TransactionTestCase` that holds `Organization.objects.select_for_update(no_key=True)`
on an org row in one connection and creates a store-scoped row in another, asserting it
does not block. Without `no_key=True` in `create_store`, and without
`db_constraint=False`, that test deadlocks or times out — which is the point.

### A.4 Deriving the value

Three write paths, three answers:

1. **`instance.save()`** — extend `StoreScopedModel.save()`, which already hooks
   `_assert_related_stores_match`. Add `_derive_org()` before it: if `org_id is None` and
   `store_id is not None`, read `Store.all_objects.filter(pk=store_id).values_list("org_id", flat=True).first()`.
   Mirror `StoreAccess._derive_org` / `clean_fields` exactly, including the
   `clean_fields(exclude=...)` override, or `full_clean()` reports a non-null `org` as
   missing on a perfectly valid row (that bug is already fixed once, in `StoreAccess`).
2. **`objects.create()` / `objects.bulk_create()`** — `bulk_create` never calls `save()`.
   The derivation therefore also lives in `StoreScopedManager`, where the store is already
   pinned: stamp `org_id` from the pinned store's organization in the same place that
   pins `store_id`. `common/managers.py:563` and `:602` already handle `store_id` /
   `STORE_FIELD` for exactly this reason and are the right seam.
3. **Anything else** — a data migration, `psql`, `COPY`. Refused by `NOT NULL` and then
   by the composite FK. That is the answer to "what if someone forgets": they cannot,
   because the database refuses the row.

`_assert_related_stores_match` needs one extra line: skip `org` the way it already skips
`store` (`field.name == STORE_FIELD`), or the loop will try to resolve `Organization` as
a store-scoped relation.

### A.5 The composite FK

Name: `<table>_store_same_org_fk` — extending the shipped convention
(`orgs_storeaccess_store_same_org_fk`, `audit_auditlog_store_same_org_fk`) verbatim.

Forward:

```sql
ALTER TABLE <table> ADD CONSTRAINT <table>_store_same_org_fk
FOREIGN KEY (store_id, org_id) REFERENCES orgs_store (id, org_id)
DEFERRABLE INITIALLY IMMEDIATE;
```

Reverse:

```sql
ALTER TABLE <table> DROP CONSTRAINT IF EXISTS <table>_store_same_org_fk;
```

Byte-identical in shape to the four already shipped. Measured: Postgres accepts the
referenced columns in either order relative to the unique constraint that backs them
(`FOREIGN KEY (org_id, parent_id) REFERENCES parent (org_id, id)` was accepted against
`UNIQUE (id, org_id)`), so the column order is a *style* choice — keep the shipped order
so all statements read identically in review and in `psql \d`.

The target, `orgs_store_id_org_uniq`, already exists and is deliberately **unconditioned**.
Measured, and it is the reason: a partial unique index cannot back a foreign key —
`CREATE UNIQUE INDEX ... WHERE deleted_at IS NULL` then `ADD FOREIGN KEY` gives
`ERROR: there is no unique constraint matching given keys for referenced table`. §B.3
turns that into an explicit E005 rule instead of an accident.

### A.6 Generation for every future store-scoped table

Add to `common/db.py`:

```python
def same_org_fk_v1(table: str) -> tuple[str, str]:
    """Return (forward_sql, reverse_sql) tying `table`'s (store_id, org_id) to
    orgs_store (id, org_id). FROZEN for every table name a shipped migration
    passes in; add same_org_fk_v2 instead of editing (see module docstring)."""
```

The `_v1` suffix is not decoration: `test_every_sql_helper_in_common_db_is_versioned`
matches `re.search(r"_v\d+$", name.lower())` on every non-underscore name in
`vars(common.db)` and fails startup of the suite otherwise. The bare-`_v` loophole was
tightened in round 4, so `same_org_fkv` will not pass either.

**Refuse the loop in the migration.** The reference checklist asks for a loop over
`StoreScopedModel` subclasses inside the migration. Do not write it. A migration that
enumerates the model registry at *apply* time emits different SQL depending on which
models happen to exist when it runs — which is the exact fork `common/db.py` exists to
prevent, arriving through a door the pin cannot close, because the pinned text would
itself become a function of the registry. Instead: **one `RunSQL` per table, with a
literal table name**, the shape `common/db.py` already documents for
`append_only_triggers_v1`:

```python
FORWARD, REVERSE = same_org_fk_v1("catalog_product")
operations = [migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE)]
```

The loop belongs in the test.

### A.7 The test: extend, do not replace

`tests/test_orgs_models.py::test_the_same_org_composite_keys_exist_in_postgres` (line 365)
**stays exactly as it is.** It hard-codes four names, which is honest for the four that
exist, and it is the premise assertion that stops the new enumerating test from passing
vacuously. This ledger has recorded a vacuous-pass twice; do not remove the one test that
cannot have one.

Add, next to it, `test_every_store_scoped_table_has_its_same_org_key`:

- Enumerate concrete `StoreScopedModel` subclasses from the app registry.
- **Premise assertion first:** `assert len(models) >= <n>`, where `<n>` is the count the
  test app declares. An enumeration that silently finds nothing must fail, not pass.
- For each, query `pg_constraint` for `conname = f"{model._meta.db_table}_store_same_org_fk"`
  and assert `contype = 'f'`, `condeferrable = true`, `condeferred = false`,
  `confmatchtype = 's'`, and that `conkey`/`confkey` resolve through `pg_attribute` to
  `(store_id, org_id) → (id, org_id)`.

**`pg_constraint`, not `information_schema`.** Two reasons, and both are the difference
between a test and a decoration. `information_schema.table_constraints` and
`.referential_constraints` expose no deferrability columns, so an `information_schema`
test cannot see the one property §A.8 is about. And `information_schema` views are
filtered by the privileges of `current_user` — under the non-owner app role that RLS
requires (§F.4), they would silently return fewer rows, which is a vacuous pass wearing
a green tick.

**PG18 hazard, measured:** PostgreSQL 18 records `NOT NULL` as real `pg_constraint` rows
with `contype = 'n'`. A 300k-row table with one `NOT NULL` column and a `CHECK` showed
four rows where PG17 shows two. The existing test already filters `contype = 'f'` and is
safe; **every new `pg_constraint` query must filter `contype`**, or the PG18 bump turns
it red for the wrong reason.

### A.8 `DEFERRABLE INITIALLY IMMEDIATE` — restated, so nobody "fixes" it

**`DEFERRABLE INITIALLY IMMEDIATE` is correct. Keep it. The reference checklist's
`DEFERRABLE INITIALLY DEFERRED` is a regression and must not be applied.**

This verdict has survived one self-correction against the installed Django source and two
independent gate reviews. Restating it in full because a schema-hardening round is
exactly when someone reaches for the "more standard" spelling.

1. **`INITIALLY DEFERRED` makes the tests that matter pass vacuously.** `IMMEDIATE`
   checks at statement end; `DEFERRED` checks at COMMIT. Inside a `TestCase` there is no
   COMMIT — the outer atomic block rolls back. So `pytest.raises(IntegrityError)` around
   a cross-organization write would **never fire**, and the test asserting that the
   single most important invariant in the product is enforced would go green because
   nothing was ever checked. Measured on this schema, not reasoned about.
2. **`IMMEDIATE` is strictly stronger, in one direction only.** `DEFERRABLE` is still
   declared, so anyone who genuinely needs a deferred window issues
   `SET CONSTRAINTS ALL DEFERRED` for one transaction. `INITIALLY DEFERRED` removes the
   strict default and offers no way back to it inside a transaction that has begun.
3. **The `loaddata` objection is real, bounded, and does not argue for DEFERRED.**
   Adjudicated against the installed source: `postgresql.DatabaseWrapper` does **not**
   override `disable_constraint_checking()`; the base returns `False` and emits no SQL,
   so `loaddata`'s `constraint_checks_disabled()` is a no-op on Postgres. Django survives
   this only because its own FKs are `INITIALLY DEFERRED` and ours are not. Consequence:
   a hand-written **child-first** fixture is genuinely refused, reproduced and pinned in
   `test_django_does_not_defer_constraints_for_loaddata`. `dumpdata` emits dependency
   order, so real fixtures load. The operational rule is "fixtures are parent-first".
4. **The landmine that actually needs care runs the other way.** Postgres'
   `check_constraints()` ends with a transaction-scoped `SET CONSTRAINTS ALL DEFERRED`, so
   any `loaddata` inside a `TestCase` leaves **all** `*_same_org_fk` keys deferred for the
   rest of that transaction. Measured: a cross-org UPDATE refused at baseline was
   *accepted* after a `loaddata` in the same test, surfacing at teardown and attributed to
   the wrong test. `tests/conftest.py::load_fixture` loads and then re-arms with
   `SET CONSTRAINTS ALL IMMEDIATE`. **Every new composite FK inherits this landmine.**
   Slice 2's ledger fixtures must go through `load_fixture`, never raw `loaddata`, and the
   new `same_org_fk_v1` docstring must say so.

One-line instruction for the implementer: *the string `DEFERRABLE INITIALLY IMMEDIATE` is
load-bearing; if you change it, `tests/test_orgs_models.py` and
`tests/test_fixture_loading.py` will both still pass and the product will be wrong.*

---

## B. The new `common.E005`

### B.1 The rule, in one paragraph

On a store-scoped model, a `UniqueConstraint` is valid in exactly three shapes and every
other shape is a startup error. **(1) Per-store** — the default: the constraint's
referenced names include `store` or `store_id`, and `condition` insists at AND level on
`deleted_at__isnull=True`. **(2) Per-organization across its stores** — the new,
legitimate shape (an invoice number unique across an org's five shops): referenced names
include `org` or `org_id` and **exclude** `store`/`store_id`, the condition still insists
on live rows, and the constraint's `name` ends in `_per_org`; a constraint whose name ends
in `_per_org` while including `store` is an error too, because the name lies about what
the database enforces, and an operator reads that name out of an `IntegrityError` and out
of `psql \d`. **(3) A composite-FK target** — referenced names are exactly `{"id"}` plus
one tenant column (`org`/`org_id` or `store`/`store_id`), the name ends in `_id_org_uniq`
or `_id_store_uniq`, and it is **exempt from the live-rows requirement**, because
PostgreSQL refuses a partial unique index as a foreign-key target (measured) and because
the constraint carries no information anyway: `id` is already globally unique through the
primary key, so `(id, tenant)` adds no existence oracle that the PK did not already grant.
Still errors, unchanged: any non-primary-key local field with `unique=True` — except a
non-editable `UUIDField` carrying a `default`/`db_default`, which is the `public_id`
surrogate of §C and reveals nothing, since the value is machine-generated and unguessable;
`Meta.unique_together`, which cannot be conditioned on `deleted_at`; a `UniqueConstraint`
referencing neither `store` nor `org`, which is enforced across every tenant; and any of
the three valid shapes missing its condition, except shape (3).

### B.2 Decision table for the implementer

| Shape | `store` in fields | `org` in fields | `condition` live | Name suffix | Verdict |
| --- | --- | --- | --- | --- | --- |
| per store (default) | yes | either | required | none required | OK |
| per org across stores | **no** | yes | required | `_per_org` | OK |
| per org, name says `_per_org`, but includes `store` | yes | yes | required | `_per_org` | **E005** — the name lies |
| per org, correct shape, no `_per_org` suffix | no | yes | required | missing | **E005** — must be declared, not inferred |
| composite-FK target | exactly `{id, store}` | or exactly `{id, org}` | **not required** | `_id_store_uniq` / `_id_org_uniq` | OK |
| `(id, X)` where X is not a tenant column | — | — | — | — | **E005** — a redundant index, not a constraint |
| any shape, no condition | — | — | missing | — | **E005** (except FK target) |
| neither `store` nor `org` | no | no | — | — | **E005** |
| `unique=True` on a non-pk field | — | — | — | — | **E005** |
| `unique=True` on non-editable `UUIDField` with a default | — | — | — | — | OK (`public_id`) |
| `unique_together` | — | — | — | — | **E005** |

### B.3 Why a name suffix and not an inference

Once `org` sits on the base, `UniqueConstraint(fields=["org", "name"], ...)` becomes
something a developer types **by habit** when they meant per-store. An inferring rule
("org is in there, so it must be intentional") turns that typo into a silently org-wide
constraint that will reject a legitimate row in the second shop, in production, months
later. The suffix is one extra statement of the same intent, it lives on the database
object itself, it appears in `pg_constraint`, in `psql \d`, and in the error message an
operator reads — and, critically, the check verifies that the **name and the shape agree
in both directions**, so a mismatch is an error whichever half is wrong. One declaration,
self-checking. A second Python-side allowlist would be a second thing to keep in step,
which is the argument this schema already uses against a second org pointer.

`_expression_names` (`common/checks.py:44`) already walks `F()` / `Lower()` trees, so
`UniqueConstraint(Lower("number"), "org", ...)` resolves correctly with no change. The
FK-target detection must use `constraint.fields` only (an expression-based constraint
cannot back a foreign key at all).

### B.4 Tests

`tests/test_common_checks.py` already has an E005 block at lines 228–290 with the right
shape (a throwaway `StoreScopedModel` subclass per case, asserted through
`audit_store_scoped_models`). Extend it to one test per row of the table above — eleven
rows, eleven tests, each asserting `"common.E005" in ids(...)` or `== []`. Two of them
are the ones a reviewer should look for: the `_per_org` name that includes `store`, and
the `(id, org)` FK target with no condition. Both are new behaviour, and the second one
is a *relaxation* — relaxations get the same denial coverage as restrictions, or the next
refactor quietly widens them.

---

## C. The UUIDv7 public identifier

### C.1 Which base carries it

**A new abstract base, `common.models.PublicIdModel`, carrying only the column** — mixed
in explicitly, exactly once, per concrete model.

Not `AuditedModel` and not `SoftDeleteModel`: they are mixed independently
(`Organization(SoftDeleteModel, AuditedModel)`), so a field on either would arrive twice
through the MRO on models that inherit both, and Django raises `FieldError` on a clashing
inherited field. Not `StoreScopedModel` either — the requirement is *every* row, and
`accounts.User`, `AuditLog`, `Organization` and `Store` are not store-scoped.

The mechanism that makes "explicitly, exactly once" hold is **`common.E008`**: a startup
error for any concrete first-party model (app label in `{accounts, orgs, audit, catalog,
inventory, sales, money, reporting, notifications}`) that does not inherit
`PublicIdModel`. Django's own `contenttypes` / `auth` / `sessions` tables are excluded by
app label. Without the check, "remember to add it" is the plan, and this repo's whole
posture is that remembering is not a mechanism.

`AuditLog` gets it too, and that is deliberate: an audit row is linkable from a UI, and
`target_id` staying a `BigIntegerField` (a raw internal id) is fine because it is never a
URL — but the audit row's own identity is.

### C.2 The field

```python
class PublicIdModel(models.Model):
    public_id = models.UUIDField(
        _("public id"),
        db_default=UUID7(),          # or Func("raporo_uuidv7") — see C.5
        editable=False,
        unique=True,
    )

    class Meta:
        abstract = True
```

### C.3 `db_default` over a Python default, and `unique=True` over a separate index

**`db_default`, three reasons:**

1. It is generated where the row is created, so `bulk_create`, `COPY`, a data migration
   and a hand-typed `INSERT` in `psql` all get one. A Python `default=uuid.uuid7` covers
   only the ORM path. Python 3.14 does ship `uuid.uuid7()` (verified, `3.14.6`), so the
   Python route is available — and it is the wrong one here for the same reason
   `audit/0002` exists: the ORM is the first line, not the last.
2. Verified in the installed source: `Field.db_returning` returns `has_db_default()`
   (`django/db/models/fields/__init__.py:973`). So Postgres returns the value on the same
   `INSERT ... RETURNING` and `obj.public_id` is populated after `create()` — no extra
   round trip, no `refresh_from_db()`.
3. Verified: `Func.allowed_default` is `all(...)` over source expressions, which is `True`
   for a zero-arity `Func` (`expressions.py:1143`), and `UUID7.__init__` strips its `None`
   shift, so both `UUID7()` and a custom zero-arity `Func` satisfy `fields.E012`.

**`unique=True` suffices; add no separate index.** In PostgreSQL `unique=True` *is* a
btree unique index, and it is the index the URL lookup uses — measured:
`Index Scan using sale_public_id_uniq on sale`. A second index on the same column would
be pure write cost. §B.1 carries the matching E005 exemption.

**Cost on write, measured** (300k rows, PG 18.6, one table per variant, otherwise
identical):

| | insert time | unique index size |
| --- | --- | --- |
| baseline (bigint pk only) | 3.54 s | — |
| `+ public_id uuid` v7, unique | 5.93 s | 9 264 kB |
| `+ public_id uuid` v4, unique | 6.36 s | 11 MB |

So: **+8 µs and ~32 bytes of index per row**, and v7's time-ordering makes the index 16 %
smaller and the insert 7 % faster than v4 would. Against a single-row `INSERT` through
Django (a network round trip alone is ~200 µs) this is unmeasurable. Quote the number,
then stop worrying about it.

### C.4 Not the primary key

Keep `BigAutoField`. A UUID primary key would double the width of every foreign-key
column, every `(id, org_id)` composite-FK target index, and every index that includes the
pk as a heap pointer — for a URL-cosmetics benefit that a separate `public_id` already
delivers. Slice 2 adds roughly fifteen tables with two to four FKs each; this is the
single most expensive reversible mistake available in this round.

### C.5 The fallback, if PostgreSQL 18 is not viable

Verified: `django.db.models.functions.UUID7.as_postgresql` raises
`NotSupportedError("UUID7 requires PostgreSQL version 18 or later.")`
(`functions/uuid.py:68`), gated on `features.supports_uuid7_function → is_postgresql_18`.
Measured: `uuidv7()` exists in the PG 18.6 catalog (two overloads, `provolatile = 'v'`)
and does not exist before 18.

**Recommendation: install our own `raporo_uuidv7()` function and use it unconditionally,
on both platforms, so nothing in this round waits on devops.** The PG18 bump then becomes
`CREATE_UUIDV7_FUNCTION_V2` with body `SELECT uuidv7()` plus a new migration — the exact
mechanism `common/db.py` was built for, used a second time for the purpose it was built
for. The schema shape, the column, the default, the index, the tests and the pins are
identical on PG17 and PG18, and no application code knows which one it is on.

The V1 body, **verified correct on PG 18.6** against the native function:

```sql
CREATE OR REPLACE FUNCTION raporo_uuidv7() RETURNS uuid
LANGUAGE sql VOLATILE PARALLEL SAFE AS $$
  SELECT encode(
    set_bit(
      set_bit(
        overlay(uuid_send(gen_random_uuid())
                placing substring(int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint) from 3)
                from 1 for 6),
        52, 1),
      53, 1),
    'hex')::uuid
$$;
```

Measured: `uuid_extract_version() = 7`, variant nibble `a` (RFC 9562 `0b10xx`),
`uuid_extract_timestamp()` returns the generation time, 500 000 values all distinct, and
zero ms-granularity ordering violations over 20 000 sequential calls. The bit positions
are the whole trick and are easy to get wrong: the obvious-looking `set_bit(.., 50, 1)`
variant produces **version 4** — it was tested and rejected, so do not "simplify" the
constants.

One honest caveat, measured. Within a single millisecond the low 74 bits are random, so
the fallback's btree locality is ms-granular where native PG18 `uuidv7()` packs sub-ms
bits into `rand_a`. For interactive traffic (a busy shop: single-digit writes per second)
the two are indistinguishable. For a **bulk historical import** — which `docs/PRODUCT.md`
explicitly allows, "any age" — 300 000 rows landing inside a few milliseconds gave the
fallback an 11 MB index against native's 9 264 kB, i.e. v4-like scatter. That is a real
reason to want PG18 and not a reason to block on it.

Rejected alternatives, with reasons, so they are not re-proposed:

- **`default=uuid.uuid7` in Python.** Moves the guarantee out of the database, which is
  the one thing this schema does not do. Every non-ORM insert path would need `public_id`
  to be nullable, and a nullable public identifier breaks the URL contract.
- **Defer `public_id` until PG18 lands.** Measured, and this is the expensive one:
  `ALTER TABLE ADD COLUMN pub uuid NOT NULL DEFAULT uuidv7()` on a 200 000-row table
  **rewrote the table** (`pg_relation_filenode` changed) while holding
  `AccessExclusiveLock`. A constant default on the same table did *not* rewrite. Adding
  this column is free today and an announced outage later; that asymmetry is the whole
  argument for landing it in this round on whichever Postgres we are on.

---

## D. The index set

Baseline, read from the running dev database (read-only): **64 indexes** exist across
`accounts_*`, `orgs_*`, `audit_*`. Exactly **two** are hand-designed for an access
pattern (`audit_org_at_idx`, `audit_target_idx`). The other 62 are automatic: primary
keys, unique constraints, and Django's default single-column index on every
`ForeignKey`.

That last group is where the work is, and it goes in the unexpected direction.

### D.1 Remove 19 indexes. This is the largest single win available.

**Django creates a single-column btree on every FK column**, including the actor columns
the abstract bases contribute to every table. From the live database:

```
orgs_membership_created_by_id_7caea482    ON orgs_membership (created_by_id)
orgs_membership_updated_by_id_0335556c    ON orgs_membership (updated_by_id)
orgs_membership_deleted_by_id_07182330    ON orgs_membership (deleted_by_id)
```

Three indexes per table, on five orgs tables, that **no query in the product will ever
use**. `AuditedModel` and `SoftDeleteModel` declare those FKs with `related_name="+"`, so
they are not even traversable; "everything this user created" is an audit question
answered from `audit_auditlog`, and it can afford a sequential scan once a year.

The usual reason to index an FK column — a `DELETE` on the parent forces a scan of every
child — does not apply: hard delete is structurally forbidden (`HardDeleteForbidden`, no
admin delete), and `migrate accounts zero` issues `DROP TABLE`, not `DELETE`.

Also from the live database: `db_index=True` on a `CharField` costs **two** indexes,
because Django adds a `varchar_pattern_ops` twin:

```
audit_auditlog_action_dc562e21        ON audit_auditlog (action)
audit_auditlog_action_dc562e21_like   ON audit_auditlog (action varchar_pattern_ops)
audit_auditlog_at_55383e01            ON audit_auditlog (at)
```

`audit_auditlog` is the highest-write table in the product — every service writes a row —
and it carries three indexes nobody queries. `audit_org_at_idx` already leads with `org_id`
and serves every tenant-scoped audit read; a global `ORDER BY -at` with no org filter does
not exist in a tenant application, and the ten-year retention sweep runs annually.

| Change | Indexes removed |
| --- | --- |
| `db_index=False` on `AuditedModel.created_by`, `.updated_by` | 10 (5 tables × 2) |
| `db_index=False` on `SoftDeleteModel.deleted_by` | 5 |
| `db_index=False` on `AuditLog.actor` | 1 |
| `db_index=False` on `AuditLog.action` (drops the `_like` twin too) | 2 |
| `db_index=False` on `AuditLog.at` | 1 |
| **Total** | **19** |

And, more importantly, **not created** on the fifteen-odd tables slices 2–5 will add:
roughly 45 indexes never built, on the tables that carry the write load.

`AuditLog.action` and `.at` keep their `Meta.ordering` and their `audit_org_at_idx`; if
the audit-view screen (already the ledger's deferred minor) needs
`(org_id, action, at DESC)`, that is one composite added then, not three singles kept now.

### D.2 The RLS measurement that decides every index shape

Measured on PG 18.6, 500 000 sales rows, 40 organizations × 5 stores, RLS enabled and
forced, policy `USING (org_id = NULLIF(current_setting('raporo.org_id', true), '')::bigint)`,
queried as a non-superuser role.

**With a `(store_id, at DESC)` index — no leading `org_id`:**

```
Index Scan using sale_store_at_live on sale (actual rows=0.00 loops=1)
  Index Cond: (store_id = 33)
  Filter: (org_id = (NULLIF(current_setting('raporo.org_id'::text, true), ''::text))::bigint)
```

The tenant predicate is a **per-row `Filter`**, which forces heap access for every
candidate row and makes an index-only scan impossible.

**With `(org_id, store_id, at DESC)`:**

```
Index Cond: ((org_id = (NULLIF(current_setting('raporo.org_id'::text, true), ''::text))::bigint) AND (store_id = 33))
Execution Time: 1.045 ms
```

The tenant predicate becomes an **`Index Cond`**. `current_setting` is `STABLE`
(`provolatile = 's'`, verified in `pg_proc`), which is what makes it usable as a scankey.

**And with the application *also* filtering `org_id = 7` explicitly:**

```
One-Time Filter: ((NULLIF(current_setting('raporo.org_id'::text, true), ''::text))::bigint = 7)
  ->  Index Scan using sale_org_store_at_live on sale
        Index Cond: ((org_id = 7) AND (store_id = 33))
Execution Time: 0.146 ms
```

Postgres collapses the policy to a **one-time filter** evaluated once per query, and the
scankey becomes a literal — better selectivity estimates and no per-row work. On the
org-wide period query the same shape produced an **Index Only Scan with `Heap Fetches: 0`**
where the implicit form did a bitmap heap scan.

**1.045 ms → 0.146 ms, for adding a WHERE clause that RLS already guarantees.**

Two rules follow, and they are the two most consequential lines in this section:

1. **Every store-scoped table must have at least one index whose first column is
   `org_id`.** Enforced by `common.E007` (§D.3).
2. **`StoreScopedManager.for_store()` / `for_stores()` must add `org_id` to the WHERE
   clause**, even though RLS makes it redundant. This is a `common/managers.py` change
   that this plan is asking for, with a measured 7× reason. It also means the guard and
   the policy are two independent enforcements of the same fact, which is the posture
   `StoreAccess.org` already established.

### D.3 `common.E007` — the org-leading index rule

A startup error for any concrete `StoreScopedModel` subclass with no entry in
`Meta.indexes` (or `Meta.constraints`) whose first referenced column is `org`/`org_id`.
Without it, a table added in slice 3 with no index gets a sequential scan under RLS on
every read, and nothing says so until someone runs EXPLAIN.

Numbering note: model-tag checks continue at `E007`/`E008`. `E100` is taken (test-named
database); `E101` is reserved for the `/app`-write invariant that round 4 routed to
devops; §F.5 proposes `E102`.

### D.4 Access patterns, derived from PRODUCT.md and the architecture spec

There are no business tables yet, so these are the queries slices 2–4 will issue,
written down before the indexes so the indexes can be traced to them.

| # | Query | Slice | Frequency |
| --- | --- | --- | --- |
| Q1 | product picker for a store: `org, store, live ORDER BY name` | 2 | hottest read in the app — every sale-entry keystroke |
| Q2 | product typeahead: Q1 + `name ILIKE %term%` | 2 | per keystroke |
| Q3 | stock level for a variant in a store | 2 | per sale line |
| Q4 | stock ledger for a variant: `org, store, variant ORDER BY at DESC` | 2 | product detail page |
| Q5 | low-stock list: `store, quantity <= threshold` | 6 | scheduled |
| Q6 | expiry alerts: `org, expiry_date <= ?` | 2/6 | scheduled |
| Q7 | sales list / period: `org, store, at ∈ [a,b), live ORDER BY at DESC` | 3 | the report workhorse |
| Q8 | detail page by `public_id` | 2–5 | every navigation |
| Q9 | lines of one sale / items of one order | 3 | per detail page |
| Q10 | credit book: customers with outstanding balance in a store | 3 | daily |
| Q11 | customer search by name / phone in a store | 3 | per keystroke |
| Q12 | payments in a period, by direction and method | 3 | per report |
| Q13 | consolidated org report across all stores, one period | 4 | per report |
| Q14 | per-store period aggregation | 4 | per report |
| Q15 | composite-FK probe on every store-scoped insert | all | every write |
| Q16 | the RLS predicate itself | all | every query |
| Q17 | store access for a membership at login | 1 | per session |

Q3 and Q9 are served by constraints and Django's own FK indexes. Q15 is served by the
existing `orgs_store_id_org_uniq`.

**Q15 clarification, because the checklist asks about "FKs pointing out of a row":**
Postgres does not index the referencing side of a foreign key, and Django does — for
every FK it declares. Our four `RunSQL` composite keys are the exception: no index backs
their referencing columns. **They do not need one.** A referencing-side index matters only
when the *parent* row is deleted or its referenced key is updated, so that Postgres can
find the children. `orgs_store.id` and `orgs_store.org_id` never change, and stores are
never hard-deleted. The enforcement direction — checking a child insert against the parent
— uses the *referenced* side's unique index, which exists. So the honest answer is that
this concern reduces the index count rather than raising it.

### D.5 Six indexes to create now

Every one is declared in `Meta.indexes` on its model, so it lands inside that app's
`0001_initial` on an empty table — no `CONCURRENTLY`, no `atomic = False`, no lock risk.

**Naming.** `models.Index` names are capped at **30 characters** by Django
(`Index.max_name_length = 30`, verified; over it is `models.E034` at startup — note that
`UniqueConstraint` has no such cap, which is why the existing constraint names are
longer). Convention: `<app>_<model>_<cols>`, with `scope` standing for
`(org_id, store_id)`. Every index carries a one-line `# reason:` comment naming the query
it serves, or it does not go in.

| Name | Definition | Serves | Reason |
| --- | --- | --- | --- |
| `catalog_product_scope_name` | `(org_id, store_id, name) WHERE deleted_at IS NULL` | Q1, Q2 | the product picker; measured to beat a trigram GIN for search at our cardinality (§D.6) |
| `catalog_variant_scope_prod` | `(org_id, store_id, product_id) WHERE deleted_at IS NULL` | variant list per product | product detail page; also the org-leading index E007 requires |
| `inventory_move_scope_at` | `(org_id, store_id, variant_id, at DESC)` | Q4, Q13, Q14 | stock ledger per variant, and both period aggregations via PG18 skip scan. No `deleted_at` predicate: an append-only ledger has no soft delete |
| `inventory_move_expiry` | `(org_id, expiry_date) WHERE expiry_date IS NOT NULL` | Q6 | only RESTOCK rows carry an expiry, so the partial predicate keeps this a fraction of the table |
| `sales_sale_scope_at` | `(org_id, store_id, at DESC) WHERE deleted_at IS NULL` | Q7, Q13, Q14 | the sales list and every period report |
| `sales_payment_scope_at` | `(org_id, store_id, at DESC)` | Q12 | revenue and paid-amount aggregation per period; append-only, so no partial |

Plus, not discretionary: the `public_id` unique index per table (§C.3) and the
`(id, org_id)` / `(id, store_id)` composite-FK targets on any store-scoped table that is
itself the parent of a same-tenant composite FK — `sales_sale` for `sales_saleitem`,
`sales_order` for `sales_orderitem`, `catalog_product` for `catalog_variant`. Those are
constraints, counted under §B.1 shape (3), not indexes chosen for a query.

`inventory_move_scope_at` doing double duty for Q13/Q14 is measured, not assumed — see
§D.6, X2.

### D.6 Seven deferred, each with a named trigger

The checklist is right that covering indexes come after measurement. Here is what
measurement already says, so these are deferrals with evidence rather than shrugs.

**X1 — `pg_trgm` GIN on product and customer names. Do not create it.**
Measured, 200 000 products, 40 orgs × 5 stores × 1 000 products, RLS on. Three variants
tried:

| Index set | Plan | Time |
| --- | --- | --- |
| `gin (name gin_trgm_ops)` | Bitmap Index Scan on the GIN, 1 111 candidates, 849 heap blocks, tenant predicate as a Filter | 6.3 ms |
| `gin (store_id, name gin_trgm_ops)` (btree_gin) | **identical plan** — the scalar key was never used as an index cond | 13.4 ms |
| `gin (name gin_trgm_ops) WHERE live` + `btree (org_id, store_id) WHERE live` | planner chose the **btree alone**, 968 candidates, ILIKE in the heap filter | **2.9 ms** |

Two findings. The tenant-leading btree wins outright at our cardinality, because one
store's product list is small and the trigram index has to consider every tenant's
matches. And the composite `btree_gin` index is **wasted bytes**: forced with
`enable_seqscan=off` and `enable_indexscan=off` and a pure equality predicate, the planner
still refused to use the scalar keys and took a parallel sequential scan instead. 8 224 kB
for zero plan improvement. Do not build it.

*Trigger to revisit:* a single store's live product count exceeds 10 000, **or** measured
p95 on the typeahead endpoint exceeds 150 ms. Then the shape to test first is
`gin (name gin_trgm_ops) WHERE deleted_at IS NULL`, plain, alongside the existing btree —
never the composite.

**X2 — a dedicated `(org_id, at DESC)` report index. Do not create it.**
Measured on the 500 000-row sales table, org-wide 15-day aggregation:

| Index used | Plan | Time |
| --- | --- | --- |
| dedicated `(org_id, at DESC) WHERE live` | Index Only Scan, `Heap Fetches: 0`, 1 index search | 0.270 ms |
| `(org_id, store_id, at DESC) WHERE live` via PG18 skip scan | Index Only Scan, `Heap Fetches: 0`, 5 index searches | 0.389 ms |

0.12 ms, for 15 MB and a write on every insert into the hottest table in the product.
PostgreSQL 18's B-tree skip scan is what makes the composite serve both shapes, and it is
one of the two concrete reasons to want the platform bump (the other is native `uuidv7()`).
*Trigger:* the consolidated report's EXPLAIN shows the skip scan exceeding 5 ms, or index
searches growing past the store count.

**X3 — covering (`INCLUDE`) columns on the period indexes.** Both measured plans above
already achieve `Heap Fetches: 0` on the aggregation, so there is nothing to cover.
*Trigger:* a period aggregate whose EXPLAIN shows heap fetches dominating.

**X4 — `audit_auditlog (org_id, action, at DESC)`.** Already the ledger's deferred minor.
*Trigger:* the audit-view screen ships.

**X5 — credit-book index, `sales_sale (org_id, store_id, customer_id) WHERE live`.**
Q10's shape depends on whether `outstanding` is materialised or computed, which is an open
slice-3 design question. *Trigger:* the "who owes us" query lands and EXPLAIN shows a
sequential scan.

**X6 — low-stock partial index on `inventory_stocklevel`.** This is the one place a
**generated column** beats app code: `is_low BOOLEAN GENERATED ALWAYS AS (low_stock_threshold IS NOT NULL AND quantity <= low_stock_threshold) STORED`
(Django 5.0+ `GeneratedField`), then `(org_id, store_id) WHERE is_low`. It cannot drift
from `quantity`, and the alert sweep reads a tiny index. *Trigger:* slice 6.
For contrast, and stated so it is not proposed by analogy: **`amount_base` must not be a
generated column.** The architecture spec calls it a *frozen fact* — amount × the rate at
record time, rounded to base precision. A generated column recomputes, which is the
opposite of frozen.

**X7 — BRIN on `at` for the ten-year retention archive.** *Trigger:* a table past roughly
50 M rows.

### D.7 The count

**6 created now. 7 deferred with named triggers. 19 removed.**
Net change to the index count in this round and slice 2 combined: **−13**, plus one
`public_id` unique index per table (a correctness requirement, not a performance guess).

Every hot query in §D.4 has an EXPLAIN in this document or a named trigger. Nothing here
is speculative. That is the trade you asked for, and it came out better than five.

---

## E. Migration safety

### E.1 What is free today, stated plainly

Every table in the schema is empty in every environment that exists. There is no
production. So all of the following are instantaneous and lock-free-in-practice **now**,
and each one is a different cost later:

| Operation | Free today because | Cost on a populated table (measured on PG 18.6) |
| --- | --- | --- |
| `ADD COLUMN org_id bigint NOT NULL` with no default | nothing to backfill | impossible without the four-step dance |
| `ADD COLUMN public_id uuid NOT NULL DEFAULT <volatile>` | nothing to rewrite | **full table rewrite** under `AccessExclusiveLock` — `pg_relation_filenode` changed on 200 000 rows; a constant default did **not** rewrite |
| `ADD CONSTRAINT ... FOREIGN KEY` (validating) | nothing to validate | full scan under `ShareRowExclusiveLock` on both tables |
| `CREATE INDEX` (non-concurrent) | nothing to build | `ShareLock` — blocks writes for the whole build |
| `SET NOT NULL` | nothing to verify | full scan under `AccessExclusiveLock` |
| `DROP INDEX` (the 19 of §D.1) | — | `AccessExclusiveLock`, but instant |

**Therefore: do all of it non-concurrently, in ordinary atomic migrations, in this round.**
Do not reach for `AddIndexConcurrently` or `NOT VALID` now. They require `atomic = False`
(verified: `django/contrib/postgres/operations.py:126`, and
`CREATE INDEX CONCURRENTLY cannot run inside a transaction block` measured), which means a
failure leaves the database half-migrated with no rollback — a real cost, bought for zero
benefit against empty tables, in a repo whose contract is reversibility.

### E.2 The lock-safety rules for slice 2 onward

Write these into `docs/DEVELOPMENT.md`. Every migration's module docstring carries exactly
one of two labels, and a migration without one fails review:

- **SAFE ONLINE** — nothing rewrites a table, and no lock stronger than
  `ShareUpdateExclusiveLock` is held longer than `lock_timeout`. Deployable during traffic.
- **NEEDS A WINDOW** — anything that rewrites a table, holds `AccessExclusiveLock` past a
  moment, or validates under a write-blocking lock. Requires: a `pg_dump -Fc` backup whose
  restorability was *verified*, an announced window, and a rehearsed rollback (with
  `devops-engineer` — an untested backup is a hope).

Recipes, lock levels measured on PG 18.6:

| Operation | Lock held | Class | Online recipe |
| --- | --- | --- | --- |
| `CREATE INDEX` | `ShareLock` (blocks writes) | NEEDS A WINDOW | `AddIndexConcurrently` + `atomic = False`. A failed CIC leaves an **INVALID** index behind: the rollback is `RemoveIndexConcurrently`, and the migration must be re-runnable |
| `ADD COLUMN` nullable, no default | `AccessExclusive`, instant | SAFE ONLINE | as written |
| `ADD COLUMN NOT NULL DEFAULT <constant>` | `AccessExclusive`, instant, **no rewrite** (measured) | SAFE ONLINE | as written |
| `ADD COLUMN NOT NULL DEFAULT <volatile>` | `AccessExclusive` + **rewrite** (measured) | NEEDS A WINDOW | four steps: (1) add nullable, no default; (2) backfill in batches with `RunPython` + `schema_editor` off the hot path; (3) `ADD CHECK (col IS NOT NULL) NOT VALID` → `VALIDATE CONSTRAINT` → `SET NOT NULL` → drop the CHECK; (4) `ALTER COLUMN SET DEFAULT` separately. **Measured: `SET NOT NULL` took 2.8 ms on 300 000 rows after a validated CHECK**, versus a full scan without one |
| `ADD FOREIGN KEY` | `ShareRowExclusive` on both + full scan | NEEDS A WINDOW | `... NOT VALID` first — measured `ShareRowExclusiveLock`, no scan — commit, then `VALIDATE CONSTRAINT` in a **second migration**: measured `ShareUpdateExclusiveLock` on the child and `RowShareLock` on the parent, which blocks neither reads nor writes. The result is `convalidated = t, condeferrable = t, condeferred = f` — identical to a one-shot add |
| `SET NOT NULL` bare | `AccessExclusive` + full scan | NEEDS A WINDOW | validated CHECK first, as above |
| `DROP COLUMN` | `AccessExclusive`, instant | SAFE ONLINE, but two releases: stop writing it in N, drop in N+1 |
| `ALTER COLUMN TYPE` | `AccessExclusive` + rewrite | NEEDS A WINDOW; prefer a new column + backfill |
| `DROP INDEX` | `AccessExclusive`, instant | SAFE ONLINE (`RemoveIndexConcurrently` if even a moment matters) |

### E.3 `lock_timeout` and `statement_timeout` on the migration connection

The values live **per role** (§F.6), because the migration role is a distinct role anyway
— it must be the non-superuser schema owner while the app role must not be — and because
`pg_roles.rolconfig` is assertable in a test (measured: `ALTER ROLE raporo_owner SET
lock_timeout = '5s'` shows up as `{statement_timeout=0,lock_timeout=5s,...}`).

`raporo_owner`: `lock_timeout = '5s'`, `statement_timeout = 0`,
`idle_in_transaction_session_timeout = '60s'`.

- **`statement_timeout = 0` is deliberate.** A legitimate `CREATE INDEX CONCURRENTLY` on a
  large table may run for an hour, and killing it halfway leaves an INVALID index that a
  later migration will trip over.
- **`lock_timeout = '5s'` is the one that matters.** It makes a migration that would queue
  behind a long-running read fail fast, instead of forming a lock queue in which *every
  subsequent query on that table* waits behind the `ALTER` — the classic mechanism by
  which one migration takes a site down.

For an individual NEEDS-A-WINDOW migration that wants tighter than the role default, the
first operation is:

```python
migrations.RunSQL("SET LOCAL lock_timeout = '2s';", reverse_sql=migrations.RunSQL.noop)
```

Two notes the implementer will otherwise get wrong: `SET LOCAL` scopes to that migration's
transaction, which is exactly the wanted scope — and it does **nothing** in an
`atomic = False` migration, where there is no surrounding transaction. There, rely on the
role default. And this is a new `RunSQL` statement, so it needs a pin (§G).

### E.4 Rollback

Every migration in this round is reversible by construction: `RunSQL` with matching
`reverse_sql`, and `AddField` / `AddIndex` / `AlterField` reverse themselves. The
documented rollback is `migrate <app> <previous>` per migration.

**Never `migrate orgs zero`.** Per `apps/orgs/migrations/0001_initial.py`'s own docstring
and verified against `--plan`, Django's backwards plan unapplies `audit.0002` and
`audit.0001` first, so it **erases the entire audit log** — the one table the append-only
trigger exists to make un-erasable. It is a development reset, never an operation.

The backup step for this round: `pg_dump -Fc` before, and a `pg_restore --list` against
the dump after, so the backup is verified rather than assumed. For slice 2 onward, every
NEEDS-A-WINDOW migration requires a restore rehearsal with `devops-engineer`. I am naming
that as a dependency; I cannot assert it has happened.

### E.5 Migration order

Existing graph: `accounts.0001 → orgs.0001 → audit.0001 → audit.0002`.

| # | Migration | Contents | Deps beyond its own app | Class |
| --- | --- | --- | --- | --- |
| 1 | `orgs/0002_uuidv7_function` | `RunSQL(CREATE_UUIDV7_FUNCTION_V1, DROP_UUIDV7_FUNCTION_V1)` | — | SAFE ONLINE |
| 2 | `orgs/0003_public_id` | `AddField public_id` × 5 (Organization, Store, Role, Membership, StoreAccess) | — | free now |
| 3 | `accounts/0002_public_id` | `AddField public_id` on User | `("orgs", "0002_uuidv7_function")` | free now |
| 4 | `audit/0003_public_id` | `AddField public_id` on AuditLog | `("orgs", "0002_uuidv7_function")`, `("audit", "0002_append_only_trigger")` | free now |
| 5 | `orgs/0004_trim_actor_indexes` | `AlterField` `created_by`/`updated_by`/`deleted_by` with `db_index=False` × 5 models (−15) | — | SAFE ONLINE |
| 6 | `audit/0004_trim_indexes` | `AlterField` `actor`, `action`, `at` with `db_index=False` (−4) | — | SAFE ONLINE |
| 7 | `accounts/0003_trim_actor_indexes` | same for accounts models carrying the audited base | — | SAFE ONLINE |
| 8 | `orgs/0005_rls_enable` | `ENABLE` + `FORCE ROW LEVEL SECURITY` on the five orgs tables; **policy text owned by `security-engineer`** | — | SAFE ONLINE (`AccessExclusive`, instant) |

Three ordering facts that are easy to get wrong:

- **`accounts.0001` precedes `orgs.0001`**, so `accounts/0002` carries a *backward-looking*
  cross-app edge to `orgs/0002`. The chain `accounts.0001 → orgs.0001 → orgs.0002 →
  accounts.0002` is acyclic. Verify with `migrate --plan` before committing, the way the
  graph was verified in round 3.
- **`orgs` owns `raporo_uuidv7()`, and later apps depend on that migration.** `common`
  declares no migrations by design (its module docstring states why), and cross-app
  ordering is explicit or it does not happen — `common/db.py`'s docstring names this exact
  trap. The precedent is `audit/0002` owning `raporo_append_only()`: whichever app first
  needs a shared database object owns its lifecycle, and every later user depends on that
  migration and carries **only** the per-table operation.
- **`audit/0003` must depend on `audit/0002`.** `audit_auditlog` is append-only by trigger;
  `AddField` on it is DDL, not DML, so the trigger does not fire — but the dependency
  keeps the graph honest and matches the documented shape.

If devops confirms PG18 before this lands, migration 1 disappears and `db_default=UUID7()`
is used directly. That is the only fork in the plan, and it is one migration wide.

---

## F. Connections and timeouts

### F.1 The pick: psycopg3 pool, not `CONN_MAX_AGE`

```python
DATABASES["default"]["CONN_MAX_AGE"] = 0          # mandatory with a pool
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DATABASES["default"]["OPTIONS"] = {
    "pool": {
        "min_size": 1,
        "max_size": 4,          # == gunicorn threads per worker
        "timeout": 5,           # acquire timeout, seconds
        "max_lifetime": 1800,
        "max_idle": 300,
        "num_workers": 2,
    },
}
```

`requirements.txt`: `psycopg[binary]==3.3.5` → `psycopg[binary,pool]==3.3.5` (verified:
`psycopg_pool` is not installed today, and Django raises
`ImproperlyConfigured("Error loading psycopg_pool module. Did you install psycopg[pool]?")`
without it).

Why the pool over persistent connections:

1. **It is bounded.** `max_size` caps connections per process, so §F.7's arithmetic is
   *enforced* rather than aspirational. `CONN_MAX_AGE` gives you exactly workers × threads
   connections with no ceiling and no queue: a burst opens them all and Postgres'
   `max_connections` becomes a hard error, not a wait.
2. **Acquire timeout.** `timeout: 5` turns pool exhaustion into a clean, alertable
   application error instead of a raw `FATAL: too many connections for role`.
3. **Recycling.** `max_lifetime` retires connections on a schedule — bloat, stale plans, a
   failover that left half-dead sockets. `CONN_MAX_AGE` only approximates this.
4. **Observability.** `pool.get_stats()` exposes `connections_num`, `requests_waiting`,
   `requests_errors`, `usage_ms`. There is no equivalent for `CONN_MAX_AGE`.

Honest cost: the pool is **per process**, so N gunicorn workers means N pools of
`max_size` each. That is in the arithmetic. And it only pays off with a threaded worker
class — with sync workers a pool of 4 per single-threaded process is mostly waste. So the
pick is inseparable from the worker model: **gunicorn `gthread`**.

Verified in the installed backend source (`postgresql/base.py:199`): Django raises
`ImproperlyConfigured("Pooling doesn't support persistent connections.")` if
`CONN_MAX_AGE != 0`, and passes `CONN_HEALTH_CHECKS` through as the pool's
`check=ConnectionPool.check_connection` (line 221–225), so a connection killed by a
failover is replaced rather than handed to a request.

### F.2 The RLS interaction — why connection reuse makes a bare `SET` dangerous

Measured on PG 18.6, and verified in the Django source:

- **`grep -rn "DISCARD ALL\|RESET ALL" env/.../django/` returns zero hits.** Django never
  resets session state. Its `_close()` under a pool calls `pool.putconn(...)`
  (`postgresql/base.py:404`), and psycopg_pool's return path rolls back an open transaction
  — it does not reset GUCs.
- Measured: `BEGIN; SET raporo.org_id='2'; COMMIT;` then `current_setting(...)` → `'2'`.
  `BEGIN; SET LOCAL raporo.org_id='2'; COMMIT;` then `current_setting(...)` → the previous
  value.

So a tenant GUC set with a bare `SET` **survives into the next request that receives that
connection** — under a pool *and* under `CONN_MAX_AGE > 0`. That is a cross-tenant read,
delivered by connection reuse, with no bug in any query.

**Therefore: `SET LOCAL`, inside `transaction.atomic()`, always.** Which means every
request that touches tenant data must already be in a transaction. My assumption for
`security-engineer`: `ATOMIC_REQUESTS = True`, with the GUC-setting middleware ordered
*inside* it. Flagged as a dependency, not decided here.

### F.3 Fail-closed, measured and non-obvious — hand this to `security-engineer`

`current_setting('raporo.org_id', true)` returns:

- **NULL** in a session that never set it → `NULL::bigint` → predicate NULL → 0 rows.
  Fail-closed. Measured as a non-superuser: `SELECT count(*)` returned 0 with no GUC set.
- **the empty string** in a session where it was set and then `RESET ALL` / `DISCARD ALL`'d.
  Measured. And `''::bigint` raises `22P02 invalid input syntax for type bigint: ""` — the
  policy **errors** instead of filtering.

So the policy predicate must be:

```sql
org_id = NULLIF(current_setting('raporo.org_id', true), '')::bigint
```

which yields NULL, and therefore no rows, in **both** cases. Both measured. The naive
spelling without `NULLIF` is fail-closed in one case and a 500 in the other.

### F.4 RLS is inert without three roles — the schema-side requirement

Measured, and this is the finding that matters most:

| Role | `rolsuper` | `rolbypassrls` | Owner of the table | FORCE RLS | Rows visible (2 exist, 1 in-tenant) |
| --- | --- | --- | --- | --- | --- |
| `probe` (the postgres image default) | t | t | yes | yes | **2** |
| `raporo_owner` | f | f | yes | yes | **1** |
| `raporo_owner` | f | f | yes | no | **2** |
| `raporo_app` | f | f | no | — | **1** |

**Today `POSTGRES_USER=raporo` is the postgres image's superuser, so RLS would be pure
decoration.** A superuser bypasses RLS even with `FORCE ROW LEVEL SECURITY`. And the table
*owner* bypasses it too unless FORCE is set — and the owner is whoever runs migrations.

So the schema must provide, and `devops-engineer` must provision:

| Role | Attributes | Grants | Purpose |
| --- | --- | --- | --- |
| `raporo_owner` | `NOSUPERUSER NOBYPASSRLS`, `CREATE` on the database | owns every table | runs migrations. Measured: `CREATE EXTENSION pg_trgm` succeeds as a non-superuser because `pg_trgm`, `btree_gin` and `pgcrypto` are all `trusted = t` on PG18 |
| `raporo_app` | `NOSUPERUSER NOBYPASSRLS` | `SELECT, INSERT, UPDATE` on tables, `USAGE` on sequences; **no** `DELETE`, **no** `TRUNCATE` | the web application. Withholding DELETE/TRUNCATE is a second, privilege-level enforcement of the no-hard-delete rule that does not depend on the ORM — and round 3 measured that a non-owner role with DML privileges can neither `TRUNCATE` nor `DROP TRIGGER` |
| `raporo_report` | `NOSUPERUSER NOBYPASSRLS` | `SELECT` only | the reporting alias, with a longer `statement_timeout` |

And **`ALTER TABLE ... FORCE ROW LEVEL SECURITY` on every tenant-owned table**, not just
`ENABLE`, because the owner runs migrations and would otherwise be exempt. That belongs in
the migration (§E.5 #8) even though the policy text does not.

One consequence worth flagging to `security-engineer`: with `FORCE` on and the owner
constrained, a *data migration* that touches tenant rows must set the GUC itself or see
nothing. That is correct behaviour and a genuine footgun; it wants a documented
`with tenant(org)` helper for `RunPython`.

### F.5 Two mechanisms so this cannot regress

- **`common.E102`** (security tag, the working E100 shape — pure `settings.DATABASES`
  string inspection, opens no connection): refuse to boot under prod settings if
  `DATABASES[...]["USER"]` matches the owner role name or a configured deny-list. Note the
  E100 lesson explicitly: register it under `Tags.security`, **not** `Tags.database`, or
  `CheckRegistry.run_checks` drops it and the guard sits inert — which is exactly how E100
  shipped dead for two review rounds.
- **A database-backed test** reading `pg_roles`: the connecting role must have
  `rolsuper = false` and `rolbypassrls = false`, and its `rolconfig` must contain a
  `statement_timeout`. Plus a test that every tenant table has `relrowsecurity` **and**
  `relforcerowsecurity` true in `pg_class`. `ENABLE` without `FORCE` is the failure mode
  that looks correct in a migration diff.

Canonical values live in a checked-in `docker/postgres/roles.sql`, applied by
provisioning, and the test compares against it — so there is one source of truth, and the
verification is a test rather than a hope. Roles are cluster objects, outside the app's
migration graph and requiring privileges the app role must not have, which is why they
are not a migration.

### F.6 Per-role timeouts

Measured: `ALTER ROLE ... SET ...` lands in `pg_roles.rolconfig` and is therefore
assertable.

| Role | `statement_timeout` | `lock_timeout` | `idle_in_transaction_session_timeout` | Why |
| --- | --- | --- | --- | --- |
| `raporo_app` | `10s` | `3s` | `15s` | an HTMX fragment that takes 10 s is already a failed page; a 3 s lock wait means a request never joins a long lock queue; 15 s kills a transaction leaked across an external call, which is what makes the pool run dry |
| `raporo_report` | `120s` | `5s` | `60s` | period aggregation and PDF/share-card rendering run in-request until Celery lands in slice 6. 120 s is a ceiling, not a target — `performance-engineer` owns the budget |
| `raporo_owner` | `0` | `5s` | `60s` | see §E.3 |

`statement_timeout` on a role is a guardrail, not a security control — the session can
raise it with `SET`. That is fine and worth writing down, so nobody mistakes it for one.

### F.7 The arithmetic

Assumption (devops owns the real figure): 2 vCPU / 4 GB VPS, per `docs/PRODUCT.md`'s
low-cost self-hosted Docker PaaS.

| Consumer | Shape | Connections |
| --- | --- | --- |
| gunicorn web | `gthread`, 3 workers × 4 threads; pool `max_size = 4` per worker | 12 |
| reporting alias (`raporo_report`) | same 3 processes; pool `min_size = 0, max_size = 2` — most requests never touch it | 6 |
| Celery + beat (slice 6, not now) | 2 prefork workers × 2, plus beat | 5 |
| migrations, `manage.py`, `dbshell` | one-offs | 5 |
| `postgres_exporter`, if devops adds it | — | 2 |
| rolling-deploy overlap | old and new web containers coexist → double the web figure | 12 |
| **Total** | | **42** |
| `superuser_reserved_connections` | PG default | 3 |

**Set `max_connections = 60`.** Not the default 100: 100 is a number nobody chose, and each
backend costs roughly 5–10 MB of process memory plus up to `work_mem` per sort or hash
node — 100 × (8 MB + 2 × 4 MB) exceeds the box. 60 leaves ~30 % headroom over the computed
45, and 60 × ~10 MB ≈ 600 MB sits comfortably beside `shared_buffers = 1GB` on 4 GB.

`gthread` with 4 threads and a 4-connection pool means a thread never waits on a peer in
its own process, so `requests_waiting > 0` sustained means the *box* is saturated, not the
pool mis-sized — which makes it a clean alert.

Leak detection, for `sre-observability`: gauge `pool.get_stats()["requests_waiting"]` and
`connections_num`; alert on `requests_waiting > 0` sustained over 60 s and on
`requests_errors` rising. `idle_in_transaction_session_timeout` is the database-side
backstop for a leaked transaction. **Never the driver default in production** — psycopg
opens an unbounded number of connections with no acquire timeout, which is how a traffic
spike becomes a `FATAL: too many connections` outage rather than a slow page.

---

## G. What this does to the pinned SQL

### G.1 The mechanism handles it, and this is already measured

`test_every_run_sql_statement_in_every_migration_is_pinned` walks the operations
`MigrationLoader` will actually run — including inside `SeparateDatabaseAndState` — so it
does not care how the SQL got there. Round 3 measured this end to end by building a
throwaway guarded-table migration shaped exactly like the documented slice-2 instruction:
the test went **red immediately**, printing the statement and the digest to paste, and it
could not be made green by omission. Adding composite FKs and a `uuidv7` function is the
same shape.

### G.2 Exactly what the implementer does

1. **Add `same_org_fk_v1(table)` to `common/db.py`.** The `_v1` suffix is required by
   `test_every_sql_helper_in_common_db_is_versioned` (regex `_v\d+$` on `name.lower()`;
   the bare-`_v` loophole was closed in round 4).
2. **For every table the helper is called with, add two entries to `PINNED_SQL`**, keyed
   exactly as the existing precedent:

   ```python
   f"same_org_fk_v1('catalog_product')[forward]": "…",
   f"same_org_fk_v1('catalog_product')[reverse]": "…",
   ```

   Keying it in `PINNED_SQL` rather than `PINNED_MIGRATION_SQL` satisfies **both** tests
   with one entry: `test_no_migration_imports_an_unpinned_name_from_common_db` derives
   `pinned_names = {key.split("(")[0] for key in PINNED_SQL}`, so the bare helper name
   becomes pinned; and the catch-all checks
   `sha256(statement) in set(PINNED_SQL.values()) | set(PINNED_MIGRATION_SQL)`.
   Mirror `append_only_triggers_v1(...)` verbatim.
3. **`PINNED_MIGRATION_SQL` is for statements written inline in a migration** — the
   `orgs/0001_initial` style — keyed by hash with a one-line description. The
   `SET LOCAL lock_timeout` operations of §E.3 go here. **Do not mix styles for the
   composite keys**: slice 2's eight tables are eight helper calls, not eight hand-typed
   statements, or the pin set becomes unreviewable.
4. **Getting a hash: run the test and paste.** The failure message is already written for
   this and prints the digest and the text.
5. **Extend the premise assertions.** The catch-all currently asserts that discovery
   reached three named files. Add every new app that carries `RunSQL`
   (`apps/catalog/migrations/0001_initial.py`, …). Without it, a discovery regression makes
   the whole scan vacuous — the failure mode this ledger has recorded twice.

### G.3 No `_V1` constant is edited. Nothing in this round requires it.

- The four shipped composite-FK statements are **untouched**: the new keys are new
  statements on new tables, so they get new hashes and the old four keep theirs.
- `CREATE_APPEND_ONLY_FUNCTION_V1` is **untouched** by the PG18 bump. Its body uses only
  `current_setting`, `current_database`, `TG_OP`, `RAISE ... USING ERRCODE`, and the
  `LIKE 'test!_%' ESCAPE '!'` idiom — none of which changed in PostgreSQL 18.
- `append_only_triggers_v1` is **untouched**: slice 2's ledger tables call it with new
  table names, producing new text that gets new pins. By design.
- **If a `_V1` ever does need to change, it does not change.** Add
  `CREATE_APPEND_ONLY_FUNCTION_V2` alongside it, plus a new migration that depends on
  `("audit", "0002_append_only_trigger")` **and on every later migration that installs an
  earlier version**, with `reverse_sql = CREATE_APPEND_ONLY_FUNCTION_V1` — never a `DROP`,
  because `DROP FUNCTION raporo_append_only()` is refused by Postgres while any guarded
  table still has a trigger on it, which makes the migration unreversible the moment there
  is more than one. That rule is already in the module docstring; I am restating it because
  a platform bump is precisely the situation where someone reaches for an in-place edit.
- **The `raporo_uuidv7()` fallback is the mechanism's second real use**, and that is the
  argument that it was worth building. Its PG18 successor is
  `CREATE_UUIDV7_FUNCTION_V2` (`CREATE OR REPLACE ... SELECT uuidv7()`), a new migration
  depending on `("orgs", "0002_uuidv7_function")`, and a new pin — with `reverse_sql =
  CREATE_UUIDV7_FUNCTION_V1`, restoring the previous *body*, not dropping the function,
  since a `db_default` on six tables depends on it existing.

---

## H. Tests that must exist, by section

| § | Test | Assertion | Mutation that must turn it red |
| --- | --- | --- | --- |
| A.1 | `test_every_store_scoped_model_carries_its_org_pointer` | every concrete subclass has non-null `org` → `orgs.Organization`; no model declares `organization` | remove `org` from the base |
| A.3 | `test_a_store_scoped_insert_does_not_block_on_create_store` (`TransactionTestCase`, two connections) | insert succeeds while the org row is held `FOR NO KEY UPDATE` | give `org` a real `db_constraint` **and** drop `no_key=True` |
| A.4 | `test_org_is_derived_from_the_store_on_every_write_path` | `save()`, `create()`, `bulk_create()` all stamp `org_id` | remove the manager-side derivation (this is the one `save()` alone misses) |
| A.5 | `test_the_composite_key_refuses_a_mismatched_org` | direct `all_objects` write with a foreign `org_id` raises `IntegrityError` **inside** `transaction.atomic()` | change `IMMEDIATE` to `DEFERRED` — the test must fail, and if it does not, the test is wrong |
| A.7 | `test_every_store_scoped_table_has_its_same_org_key` | per-subclass `pg_constraint` row with `contype='f'`, `condeferrable=t`, `condeferred=f`, `confmatchtype='s'`, correct `conkey`/`confkey`; **premise: subclass count ≥ n** | omit one table's `RunSQL`; separately, break the enumeration and confirm the premise assertion fires |
| A.8 | existing `test_django_does_not_defer_constraints_for_loaddata`, `tests/conftest.py::load_fixture` | unchanged | — (already mutation-tested in round 4) |
| B | eleven cases, one per row of §B.2 | `common.E005` present / absent | invert each rule; the `_per_org`-with-`store` and `(id, org)`-without-condition cases are the new ones |
| C.1 | `test_every_first_party_model_has_a_public_id` (`common.E008`) | E008 for a concrete first-party model lacking `PublicIdModel` | add a model without the base |
| C.2 | `test_public_id_is_populated_by_the_database_on_every_write_path` | `create()`, `bulk_create()`, and a raw `INSERT` through `connection.cursor()` all yield a `public_id`; `uuid.UUID(...).version == 7` | replace `db_default` with a Python `default` — the raw-INSERT leg must fail |
| C.5 | `test_the_uuidv7_function_sql_has_not_changed` + a behavioural test | version nibble 7, RFC 9562 variant, extractable timestamp, 100 000 distinct, zero ms-granularity ordering violations | change `set_bit(.., 52, 1)` to `50` → version 4 |
| D.2 | `test_for_store_filters_on_org_explicitly` | the compiled SQL contains an `org_id` predicate | remove it from the manager; the 7× plan regression is otherwise invisible |
| D.3 | `test_every_store_scoped_model_has_an_org_leading_index` (`common.E007`) | E007 when no `Meta.indexes` entry leads with `org` | declare a table with only a `store`-leading index |
| D.5 | `test_the_declared_indexes_exist_in_postgres` | each of the six names present in `pg_indexes` with the expected `indexdef`, including the partial predicate | drop the `WHERE deleted_at IS NULL` |
| D.5 | `test_no_index_name_exceeds_thirty_characters` | Django's `Index.max_name_length` | a 31-character name (this fails at startup as `models.E034`, but the test names the reason) |
| E.2 | `test_every_migration_declares_its_lock_class` | every first-party migration module docstring contains `SAFE ONLINE` or `NEEDS A WINDOW` | add a migration without a label |
| E.5 | `test_the_migration_graph_is_acyclic_and_ordered` | `MigrationLoader().graph` resolves; `orgs/0002` precedes `accounts/0002`, `audit/0003` | reverse the dependency |
| F.5 | `test_the_connecting_role_is_not_a_superuser` | `pg_roles`: `rolsuper=f`, `rolbypassrls=f`, `rolconfig` has a `statement_timeout` | connect as the owner |
| F.5 | `test_every_tenant_table_forces_row_level_security` | `pg_class.relrowsecurity` **and** `relforcerowsecurity` true | `ENABLE` without `FORCE` |
| F.5 | `test_e102_refuses_a_prod_config_using_the_owner_role` | driven **through the check registry**, never by a direct call | register under `Tags.database` — it must go silent, which is the E100 lesson |
| G | existing four stability tests, plus new pins | unchanged mechanism | add a `RunSQL` and confirm the catch-all names it |

---

## I. Things in this plan I think are mistakes, or that I would push back on

Stated plainly, because a plan that only agrees with its brief is not a review.

1. **`DEFERRABLE INITIALLY DEFERRED` in the reference checklist is wrong.** Restated at
   length in §A.8. It would make the tests that guard invariant #1 pass vacuously. This is
   the single most important line in the document.
2. **"A loop in the migration" is wrong** (§A.6). A migration that enumerates the model
   registry at apply time emits registry-dependent SQL, which is the exact fork the
   stability contract exists to prevent — and it would make the pinned text a function of
   the registry, defeating the pin by construction. One `RunSQL` per table with a literal
   name; the loop goes in the test.
3. **The composite trigram GIN index is a measured waste** (§D.6 X1). 8 224 kB for an
   identical query plan; the scalar keys were never used, even with `enable_seqscan=off`
   and a pure equality predicate. And the plain trigram GIN loses to a tenant-leading btree
   at our cardinality, 2.9 ms against 6.3 ms. Both belong in the deferred column, not the
   "hardening" column.
4. **`organization` as the field name is the wrong call** (§A.1), and it is the one place I
   am overruling the brief on a stated decision rather than on a technical detail. Four
   SHA-256-pinned statements already say `org_id`. Choosing `organization` buys a database
   where the same concept has two names and every composite FK reads like a typo.
5. **`AddIndexConcurrently` and `NOT VALID` are premature in this round** (§E.1). They cost
   `atomic = False`, which means a failure leaves a half-migrated database, and they buy
   nothing against empty tables. They are slice-2 tools; the rules are written down for
   then.
6. **The biggest indexing win is a subtraction, and it is not in the brief at all**
   (§D.1). Nineteen indexes exist today that no query will use, and the abstract bases
   would replicate them across every table slices 2–5 add — roughly 45 more, on the
   highest-write tables in the product. If only one thing from this document lands, land
   `db_index=False` on the actor foreign keys.
7. **RLS as scoped is decoration until the role split lands** (§F.4). Measured: a
   superuser bypasses `FORCE ROW LEVEL SECURITY`, and `POSTGRES_USER=raporo` is the
   postgres image's superuser today. Shipping `ENABLE ROW LEVEL SECURITY` with policies
   while the app connects as a superuser gives the *appearance* of a second enforcement
   layer with none of the effect, which is worse than not shipping it — it invites the
   query-layer guard to be relaxed on the strength of a defence that is not there. **The
   role split is a hard prerequisite for the RLS work, not a follow-up.** That is a
   dependency on `devops-engineer` and it should gate the merge.
8. **`statement_timeout = 120s` for reporting is a number I made up** (§F.6) and I want it
   contradicted. It is a ceiling that stops a runaway query, not a budget.
   `performance-engineer` owns the real target, and if a period report needs 120 s it needs
   Celery, not a longer timeout.
9. **Time data is under-specified here on purpose.** Everything is `timestamptz`, stored
   UTC, converted at the edge; the DAY / WEEK / BIWEEK boundary semantics belong to
   `data-reporting-engineer` and `reporting/periods.py`. The one schema-side commitment I
   will make is that the period indexes are on `at DESC` and **half-open ranges**
   (`at >= start AND at < end`) are the only supported form — a `BETWEEN` on a
   `timestamptz` column double-counts the boundary instant, and an index cannot save a
   query from that. Agree it explicitly before slice 4.
