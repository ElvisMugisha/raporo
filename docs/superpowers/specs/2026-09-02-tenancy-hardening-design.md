# Tenancy hardening — design

Date: 2026-09-02 · Author: architect · Status: proposed, awaiting Elvis
Revision 2 (2026-09-02): Elvis's owner-access and one-org-per-user rulings folded in.
Branch: `feat/slice-1-foundation` · Companion ADRs: 0008 (org column), 0009 (RLS),
0010 (public identifiers), **0011 (org-wide store access is a permission code)**

> **Read §I first, then §J.** §I is the subject of revision 2: where the permitted store set
> is resolved and how the org owner's override works. §J lists every statement in §A–§H that
> revision 2 corrects — three controller arbitrations, two Elvis rulings and the confirmed
> PostgreSQL 18.6 / Python 3.14.7 platform. Sections A–H were not renumbered, so every
> cross-reference from the other four design documents still resolves.

Implementers: **database-engineer** owns migrations, index shapes and the SQL bodies;
**backend-engineer** owns `common/` and `apps/`; **security-engineer** owns the RLS threat
model and the policy text for `audit_auditlog`; **devops-engineer** owns database roles,
connection settings and the platform bump. Where this document names an interface rather
than an implementation, that is deliberate — the constraint is mine, the internals are theirs.

---

## 0. The problem, and the constraints it has to fit

Raporo is period-based sales reporting for Rwandan multi-store businesses. One
organization runs 1–5 stores; a user's permitted store set is per-membership. Invariant #1
(a row belongs to exactly one store, and a query may never span two organizations) is the
release-blocking correctness property of the whole product.

Today invariant #1 is enforced **entirely in Python**, at the query layer
(`common/managers.py`), the model layer (`common/models.py`) and at startup
(`common/checks.py`). That machinery has survived roughly 110 attacks across three
security-engineer harness runs and four fix rounds; it is the most-tested code in the
repository. Every change below is measured against one question: *does this keep every
refusal that machinery already makes?*

Constraints I am designing to:

| Constraint | Source |
| --- | --- |
| Django 6.1, PostgreSQL, no DRF until a real API consumer exists, business logic in a service layer, views thin | ADR 0006, ADR 0007 |
| Django templates + HTMX; fragments are addressed by URL | ADR 0007 |
| Boring technology; every new dependency justifies its maintenance cost | CLAUDE.md |
| No hard deletes anywhere; `audit_auditlog` is append-only by database trigger | slice-1 invariants |
| Any SQL a migration runs is versioned and hash-pinned (`common/db.py`, `tests/test_db_stability.py`) | LEDGER B1 |
| A guard is unverified until someone has watched it refuse something | LEDGER, four times |
| Team size: one human plus agents | CLAUDE.md |

Elvis's five settled decisions are the input, not the subject: denormalised `org`
on `StoreScopedModel` with a composite FK; RLS in slice 1; UUIDv7 public identifiers;
tenant-leading indexes + connection config + per-tenant export/delete + a generated denial
matrix; Python 3.14 + PostgreSQL 18 (**landed and verified: 3.14.7 / 18.6, 369 tests green**).
§G records the two places where I would push back and what would change my mind.

Two further rulings arrived after revision 1 and are the subject of §I: **a user belongs to
exactly one organization**, and **a user reaches only the stores they were granted, except the
org owner, who reaches any store in their org.** The second contradicts a deliberate built
decision (`StoreAccess`'s docstring rejected exactly this implicit rule), so §I.0 states the
contradiction before fixing it and §I.4 rules on what `StoreAccess` is for now.

**One assumption, flagged because it changes the shape of §C if wrong:** the application
process will stop connecting to PostgreSQL as the table owner and as a superuser. RLS is
inert otherwise (§C.5). This is devops-engineer's work and it is a *precondition* of
Elvis's decision 2, not an optional companion to it.

---

## A. What happens to the existing query-layer guards

### A.1 The answer in one paragraph

The org column **sits alongside** the store machinery and **simplifies exactly one part of
it**. It replaces nothing. The reason is a distinction the checklist blurs: today's
`SELECT` in `_store_pks()` does not ask *"which organization does this row belong to"* —
which is what the new column answers — it asks *"which organization does store id 42 belong
to"*, about identifiers a caller supplied before any row has been touched. That question
still lives in `orgs_store` and no column on a business row can answer it. So the resolver
stays. What changes is that the resolver currently computes the answer and **throws it
away**, and once it keeps it, the pin becomes a pair `(org_pk, store_pks)` — and every
*widening* merge becomes an integer comparison plus set algebra instead of a database round
trip.

Net effect: one refusal added (single-store pins now verify ownership), one query removed
per combinator, no refusal weakened.

### A.2 The value object

New in `common/managers.py`, immediately above `_store_pk`:

```python
@dataclasses.dataclass(frozen=True, slots=True)
class ScopePin:
    org_pk: int
    store_pks: tuple[int, ...]      # non-empty, de-duplicated, insertion-ordered
```

`ScopePin` is the whole pin. It is immutable so a merge cannot mutate a leg in place —
`Query.combine` mutates `self`, and the existing code already takes care to refuse before
`super()` for that reason.

### A.3 `_store_pk` — unchanged

`_store_pk(store) -> int` keeps its exact behaviour and its exact messages, including the
refusal of `None` for the reason its docstring gives (`WHERE store_id IS NULL` looks scoped
and returns nothing). It is a type and shape normaliser; nothing about the org column
touches it.

### A.4 `_store_pks` → `resolve_scope`

Rename `_store_pks(stores) -> list[int]` to `resolve_scope(stores) -> ScopePin`. The body
changes by three lines:

```python
    owners = dict(store_model.all_objects.filter(pk__in=pks).values_list("pk", "org_id"))
    unknown = [pk for pk in pks if pk not in owners]
    if unknown:
        raise ValueError(f"for_stores() was given unknown store ids: {unknown}.")
    orgs = set(owners.values())
    if len(orgs) > 1:
        raise CrossStoreReferenceError(...)          # message unchanged, verbatim
    return ScopePin(org_pk=orgs.pop(), store_pks=tuple(pks))
```

Both existing messages must survive **verbatim**; the security harness and eight tests read
them, and one of them ("a query may never span organizations") is the sentence the whole
guard exists to say.

Two additions to `resolve_scope`:

1. **A per-context memo.** `resolve_scope` consults `common.tenancy.store_org_cache()` — a
   plain `dict[int, int]` bound to the active tenant context (§C.1), absent when no context
   is active. A hit means zero queries. This is sound because a store cannot change
   organization once it has any business row: the composite FK of §B.3 refuses the
   `UPDATE orgs_store SET org_id = …` that would break it. Without a context (management
   commands, most tests) the cache does not exist and the query runs, exactly as today.
2. **The instance fast path.** When every element of `stores` is a saved `Store` *instance*
   with `org_id` loaded, the pin is built from `store.org_id` with no query at all — the
   instance came from a query that already resolved ownership.

### A.5 `for_store` / `for_stores` — one behaviour change, deliberate

```python
    def for_store(self, store):
        return self._pin(resolve_scope([store]))

    def for_stores(self, stores):
        return self._pin(resolve_scope(stores))
```

`for_store()` now goes through the resolver. **What that buys is a consistent diagnostic,
not a closed leak** — revision 1 said otherwise and was corrected by execution (§J.1).
Neither primitive authorizes a store against a caller: `for_stores([<a rival's store id>])`
returns the rival's rows exactly as `for_store()` does, because `for_stores()` refuses a set
*spanning* two organizations and refuses *unknown* ids, and a single-store set from one rival
organization has nothing to be compared against. That is correct for a scoping primitive.

The genuine residue is an **asymmetry in how the two spellings fail**: an unknown store id
raises `ValueError` from `for_stores()` and returns **silently empty** from `for_store()`. A
query that looks scoped and returns nothing is how scoping bugs hide — `_store_pk`'s own
docstring makes that argument about `None`, and it applies one level up. Routing `for_store()`
through `resolve_scope()` makes both spellings raise on an unknown id, and via §A.4's memo and
instance fast path it makes the pin cheaper rather than dearer.

Authorization has an owner and it is not the pin: **§I**. `require_store()` turns a URL
identifier into a `Store` the actor may reach, or a 404, so a store id from request data never
reaches `for_store()` at all.

Cost, stated as testable numbers:

| Expression | Queries before | after |
| --- | --- | --- |
| `for_store(<Store instance>)` | 0 | 0 |
| `for_store(<int pk>)`, no tenant context | 0 | 1 |
| `for_store(<int pk>)`, warm context cache | 0 | 0 |
| `for_stores([...])` | 1 | 1 (0 warm) |
| `a \| b`, same single store | 0 | 0 |
| `a \| b`, widening within one org | 1 | **0** |

`_pin` writes both predicates and both attributes:

```python
    @staticmethod
    def _pin(queryset, pin):
        queryset = queryset.filter(org_id=pin.org_pk, store_id__in=pin.store_pks)
        queryset.query.store_scoped = True
        queryset.query.store_scope_pks = pin.store_pks     # name kept: 6 tests read it
        queryset.query.org_scope_pk = pin.org_pk           # new
        return queryset
```

Keep the attribute names `store_scoped` and `store_scope_pks`. Six assertions in
`tests/test_common_bases.py` read `query.store_scope_pks`; `queryset_scope()` and
`refuse_scope_mix()` are built on them; the 45-case operator matrix is the most valuable
regression asset in the project. `org_scope_pk` is purely additive.

`queryset_scope(queryset) -> (bool, tuple)` keeps its signature. Add
`queryset_pin(queryset) -> ScopePin | None` beside it for callers that need the org.

Whether the single-store case emits `store_id = %s` or `store_id IN (%s)` is
database-engineer's call against the index plan; either is safe. The `org_id = %s`
term must be present in both, because it is what the tenant-leading index (§D.4) and the
RLS cross-check (§B.4) are built on.

### A.6 `merge_scope_pks` → `merge_pins`, and the query disappears

```python
def merge_pins(model, left, right, operator, *, narrow) -> ScopePin:
```

Rules, in this order — the order is load-bearing and the tech-lead flagged it once already:

1. **Both `None` → `UnscopedQueryError`**, with the current message and the current type.
   This check runs *first*. Round 4 measured that if it does not, a shortcut can hand back
   an empty pin, `ScopedQuery.combine` then sets `store_scoped = True`, and the query
   compiles with no store predicate at all.
   `test_combining_two_unpinned_querysets_raises_the_documented_error` (9 combinators) is
   what holds this and must survive the refactor unchanged.
2. **One `None` → not reachable here**, because `refuse_scope_mismatch` runs before
   `merge_pins` on both the scoped and the unscoped side. Keep that ordering; keep both
   call sites.
3. **`left.org_pk != right.org_pk` → `CrossStoreReferenceError`**, message naming both
   organization ids. *This is the replacement for the re-resolution query.* It is not
   weaker: the union of two sets each already proven to lie inside one organization lies
   inside one organization, and the "unknown store id" half of the old resolution was
   already performed on each leg when that leg was pinned. Both facts were established by a
   database read; the merge now reasons from them instead of asking again.
4. **Store sets** merge exactly as today: union by default; intersection when
   `narrow=True` (`&` and `intersection()` only — `difference()` deliberately does not
   narrow, because its rows come from the left leg); an empty intersection falls back to the
   union, because a pin of no stores reads as "unpinned" downstream.

The `left == right` shortcut that exists purely to keep `a | a` lazy can go — there is
nothing left to be lazy about; every merge is now pure computation. Keep its test,
inverted: `test_merging_a_pin_with_itself_costs_no_query` becomes
`test_no_combinator_costs_a_query`, asserting **0** queries for the widening case too. That
test is the acceptance criterion for this sub-section.

**The performance carry-forward is discharged, not optimised away.** `HANDOFF.md` says:
"Do not let slice 2 optimise the first one away without re-proving the leak it closed." The
leak was `for_store(A) | for_store(RIVAL)` being an unguarded synonym for the already
refused `for_stores([A, RIVAL])`. Required evidence, per the tech-lead's round-4 standard:
the 45-case operator × order matrix re-run before and after, plus a mutation pass in which
rule 3 is deleted and the cross-org legs are shown to return `RIVAL` rows again. Numbers in
the report, not prose.

### A.7 `ScopedQuery.combine` and `_combinator_query` — same shape, new payload

`ScopedQuery.combine` keeps its structure exactly: `refuse_scope_mismatch(rhs, connector)`,
then `merge_pins(...)`, then `super().combine(...)`, then write the merged pin — both
refusals before `super()`, because `Query.combine` mutates `self` and a refusal must leave
nothing half-merged.

`ScopedQuerySet._combinator_query` folds left-to-right over `other_qs` with `merge_pins`
and `narrow=combinator in NARROWING_COMBINATORS`, unchanged apart from the value type.

`NoHardDeleteQuerySet`'s six operators, `union()`, `refuse_scope_mix`,
`refuse_sliced_combine`, `GuardedQuery.refuse_scope_mismatch` and
`GuardedQuery.names_to_path`: **not touched**. They reason about *whether* a leg is pinned,
never about *what to*. Leaving them alone is the point; they are where the last three leaks
were found.

### A.8 Writes

| Site | Change |
| --- | --- |
| `_store_for_write` | also injects `org_id` from the pin on a single-row create; refuses an explicit `organization`/`org_id` that disagrees with the pin |
| `_given_store_pk` | gains a sibling `_given_org_pk`, same shape |
| `_check_write_store` | also refuses an out-of-scope `organization`/`org_id` |
| `bulk_create` | fills `org_id` per row from the pin (the pin's org is single-valued even when its store set is not, so this always succeeds where the store fill succeeds) |
| `_refuse_store_reparenting` | also refuses `organization`/`org_id` in `update()`, for a strictly stronger reason than store re-parenting: it would break the composite FK, and re-homing an organization is not an operation this product has |
| `_check_update_fk_stores` | unchanged, including the `resolve_expression` refusal and the multi-store refusal |
| `StoreScopedManager.raw` | unchanged, still refused |

### A.9 `StoreScopedModel` — derivation, then assertion

The row's `org_id` is **derived, never asked for**, in the pattern
`StoreAccess._derive_org` established and two gates blessed. New method
`_derive_org()` (on `StoreScopedModel`), called from `save()` before `_assert_related_stores_match`, and
from `ScopedQuerySet.bulk_create` for each object — as `_assert_related_stores_match`
already is, because `bulk_create` never calls `save()`.

Source precedence, first hit wins:

1. the pin, when the row was created through a pinned queryset — free, and guaranteed
   consistent with `store` because `resolve_scope` derived both from one read;
2. a cached `store` instance's `org_id` — free;
3. an explicit `org_id` the caller passed;
4. the active tenant context's org (§C) — free;
5. one `SELECT org_id FROM orgs_store WHERE id = %s` via `Store.all_objects`.

Whenever two of these are known and disagree, raise `CrossStoreReferenceError` naming both.
When only 3 or 4 supplied the value, the composite FK is the backstop and a wrong value
surfaces as an `IntegrityError` — loud, at the write, naming the constraint.

`_assert_related_stores_match` is otherwise unchanged. It compares `store_id`, and store
equality is what it is for. It gains nothing and loses nothing from the org column: a
store-scoped FK whose row is in another *organization* is a fortiori in another *store*.

### A.10 What is explicitly **not** removed

E004 (no traversable relation to a store-scoped model), E006 (only store-scoped models may
point at one), the `+`-query-name refusal in `GuardedQuery.names_to_path`,
`get_compiler`'s unpinned refusal, the soft-delete filter, the hard-delete refusals and the
append-only trigger. None is subsumed by the org column and none is subsumed by RLS. The
`organization` FK itself must carry `related_name="+"`, or E004 fires on it — which is the
correct outcome and the reason E004 exists.

---

## B. How RLS and the application guards divide responsibility

### B.1 Verdict on the proposed split

**The checklist's split is right. I would state its reason differently, and I would add a
third leg it omits.**

Adopt: **organization isolation in the database (RLS), store isolation in the application
(the machinery of §A), the two kept honest by the composite FK, and cross-checked at one
seam.**

The reason is not primarily "the store set is per-user" — it is that RLS's practical unit is
a value that is **constant for the whole request and cheap to compare**. The organization is
that. The permitted store set is not: an owner's single request can legitimately query store
A, then store B, then A+B, and a GUC that changes mid-request is precisely the state §C
exists to make impossible.

### B.2 Is store-level RLS achievable? Yes. I am recommending against it, and here is why

It is achievable, and cheaply, so this deserves a real answer rather than a dismissal. Push
the permitted store set into a second GUC as a text array and read it through a `STABLE`
helper so the planner evaluates it once:

```sql
USING (org_id = raporo_current_org_id()
       AND (raporo_current_store_ids() IS NULL
            OR store_id = ANY (raporo_current_store_ids())))
```

That works, is index-friendly, and needs no correlated subquery against
`orgs_storeaccess`. Three reasons not to do it now:

1. **It relocates an authorization decision into a mechanism that cannot explain itself.**
   The permitted store set is derived from `Membership` + `StoreAccess` + role permissions.
   If RLS also enforces it, a bug in that derivation becomes *zero rows* instead of
   `PermissionDenied`. This codebase has already refused silently-empty results as a design
   consequence: `_store_pk` refuses `None` for exactly that reason, in exactly those words.
2. **It breaks the property that makes `SET LOCAL` safe.** One org per request is
   invariant. One store set per request is not, so a store GUC would have to be re-set per
   query, and a GUC that changes mid-transaction is the class of bug §C is designed to
   eliminate. Buying a second control at the price of the first one's proof is a bad trade.
3. **Asymmetric blast radius.** An org-dimension hole is a cross-tenant breach — a rival
   business reading your sales. A store-dimension hole inside one org is a manager seeing
   another branch of their own company: serious, a denial test, not a breach. The database
   should carry the catastrophic case; the service layer, which can produce a translated
   denial message and an audit row, should carry the other.

Revisit trigger: an org whose branches are separate legal entities, or a per-store investor
portal where a store's numbers are confidential *within* the org. Then
`raporo_current_store_ids()` is the mechanism, and it is additive.

### B.3 The composite FK is the agreement, not a comment

```
<business table> (org_id, store_id)  ->  orgs_store (id, org_id)
```

`orgs_store` already carries
`UniqueConstraint(fields=["id", "org"], name="orgs_store_id_org_uniq")` as a composite-FK
target — added in `orgs/0001_initial` for exactly this pattern, with three such keys already
live. Requirements I need from database-engineer:

- `DEFERRABLE INITIALLY IMMEDIATE`, matching the four existing `*_same_org_fk` keys, for the
  reason the ledger records: `INITIALLY DEFERRED` violations surface only at COMMIT, which
  never happens inside a test transaction, so tests pass vacuously.
- Emitted by a **versioned, hash-pinned helper** in `common/db.py`
  (`same_org_fk_v1(table) -> (forward, reverse)`), mirroring `append_only_triggers_v1`,
  because slice 2 adds four tables that need it and hand-rolled SQL per table is how the
  append-only guard nearly forked.
- Naming: `<table>_store_same_org_fk`, matching the existing convention.
- **A second, load-bearing consequence for the migration's docstring:** this key is what
  makes "a store never changes organization" a database fact. `UPDATE orgs_store SET org_id
  = …` is refused for any store that has a single business row, and that is what licenses
  the memoisation in §A.4 and the reasoning in §A.6 rule 3. If this key is ever dropped,
  that reasoning goes with it.

### B.4 One seam where the two mechanisms are compared

`ScopedQuery.get_compiler` keeps its unpinned refusal and gains one check: if a tenant
context is active and `org_scope_pk != current_org_id()`, raise
`TenantContextMismatch(UnscopedQueryError)`.

This is a **diagnostic, not a control**, and the design must say so or the next reviewer
will over-trust it. The control is RLS: on a disagreement the row's org matches neither
predicate and the query returns nothing. The check converts a silent empty result into a
loud, named error carrying both org ids — the difference between a five-minute bug and a
two-day one. When no context is active (management commands, most of the test suite) the
check does not fire; that is not a hole, because in that state RLS itself returns zero rows
for the app role.

### B.5 What RLS covers, and what it deliberately does not

| Table | Tenant key | RLS in slice 1 |
| --- | --- | --- |
| `orgs_organization` | `id` | yes, with one carve-out (§C.6) |
| `orgs_store`, `orgs_role`, `orgs_membership`, `orgs_storeaccess` | `org_id` | yes |
| every future `StoreScopedModel` table | `org_id` | yes, from its own initial migration |
| `audit_auditlog` | `org_id`, **nullable** | yes — policy text is security-engineer's (§B.6) |
| `accounts_user`, `accounts_twofactor`, `accounts_recoverycode` | none | **no** |

`accounts_user` is a global namespace by product design: username, email and phone are
unique across the installation, and the multi-identifier auth backend must resolve an
identifier to a user **before any organization is known**, so at that point there is nothing
to key a policy on. (Revision 1 also gave "one user may hold memberships in several
organizations" as a reason; that is no longer true — see §J.3 — and the surviving reason is
sufficient on its own.) What compensates is already built and gate-verified:
the non-enumerating multi-identifier auth backend, per-identifier and per-IP throttling, and
uniform error responses. This is written down because "why is there no policy on the user
table" is the first question a reviewer will ask.

### B.6 Handed to security-engineer, with the constraint stated

- **`audit_auditlog` policy text.** `org_id` is nullable because system-initiated rows have
  no organization. A `SELECT` policy of `org_id = raporo_current_org_id() OR org_id IS NULL`
  makes every tenant a reader of every system row — not acceptable. A `WITH CHECK` that
  refuses `org_id IS NULL` breaks `audit.record` for system events. My constraint: a tenant
  must never *read* a NULL-org row; the app must be able to *write* one; and if that write
  permission is granted, the threat model should note that it is a record-hiding primitive
  (write a row with no org and it is invisible in the tenant's own audit view) and rule on
  whether that is acceptable or whether system rows need a separate path.
- **The honest limitation, which belongs in the threat model rather than being discovered
  later.** `raporo.org_id` is a *custom* GUC, so any role can `SET` it — the security gate
  measured exactly this for `raporo.allow_truncate`. RLS by GUC therefore defends against
  **our own bugs** (a missing `WHERE`, a raw query, a future DRF endpoint that forgets the
  pin), not against an attacker with arbitrary SQL execution as the app role. That is the
  same limitation the append-only trigger was accepted with, on the same reasoning ("anyone
  reaching it could `DROP TRIGGER`"), and it is still worth a great deal. Concretely: RLS
  does not reduce the severity of an SQL-injection finding by one notch.
- **Policy completeness.** Policies must be created without a `FOR` clause (all commands)
  and must carry both `USING` and `WITH CHECK`; a `USING`-only policy leaves `INSERT` into
  another organization open.

---

## C. Where the tenant context is set, and how it fails closed

### C.1 The one place is a context manager, not the middleware

`common/tenancy.py` — a new module importing nothing else from `common/`, so the dependency
direction is `tenancy ← managers ← models ← checks`.

```python
ORG_GUC = "raporo.org_id"                    # namespace shared with common/db.py's two GUCs

class NoTenantContextError(UnscopedQueryError): ...
class TenantContextMismatch(UnscopedQueryError): ...

@dataclasses.dataclass(frozen=True, slots=True)
class TenantContext:
    org_pk: int
    source: str            # "request" | "command" | "task" | "test" — for error messages
    store_org_cache: dict[int, int] = dataclasses.field(default_factory=dict)

_current: ContextVar[TenantContext | None] = ContextVar("raporo_tenant", default=None)

def current_org_id() -> int | None
def require_org_id() -> int                  # raises NoTenantContextError
def store_org_cache() -> dict[int, int] | None

@contextlib.contextmanager
def tenant(org_pk: int, *, source: str): ...
```

`tenant()` is the single door. It does four things and undoes all four:

1. asserts `org_pk` is a positive `int` — no `Organization` instance, no string, the same
   normalisation discipline `_store_pk` uses and for the same reason;
2. enters `transaction.atomic()`;
3. issues `SET LOCAL "raporo.org_id" = %s` as the **first statement** of that transaction,
   parameterised, never interpolated;
4. sets the `ContextVar` and, on exit, **resets it with the token in a `finally`**.

`contextvars`, not thread-locals, because the same primitive then works unchanged under
ASGI and across `sync_to_async`. The token reset is mandatory, not hygiene: a WSGI worker
thread reuses its context, so a `set()` without a matching `reset()` leaks the previous
request's org into the *next* request on that thread — the application-layer twin of the
`CONN_MAX_AGE` leak.

### C.2 Why `SET LOCAL`, spelled out so nobody re-litigates it

`SET LOCAL` is discarded at `COMMIT` or `ROLLBACK`. Therefore:

- a persistent connection (`CONN_MAX_AGE > 0`) carries nothing into the next request;
- a psycopg connection pool, should one ever be enabled, carries nothing to the next
  borrower;
- a savepoint rollback inside the request cannot unset it, because the `SET LOCAL` is the
  first statement of the *outermost* transaction, before any savepoint exists;
- and `SET LOCAL` outside a transaction is a no-op that emits only a `WARNING`, which is why
  `tenant()` owns the `atomic()` rather than trusting a caller to have opened one. It must
  additionally assert `connection.in_atomic_block` after entering, so the no-op case is an
  error rather than a silently context-free request.

**The mechanism that keeps it true**, since this slice has produced four controls that were
present, named, documented and never executed:

- `tests/test_tenancy_context.py` runs two requests for two organizations over one
  deliberately reused connection and asserts the second cannot see the first's rows — after
  first asserting the row *is* visible under the correct context, so an empty result is not
  mistaken for isolation;
- a source-scan test in the shape of
  `test_no_migration_imports_an_unpinned_name_from_common_db`: a bare `SET ` adjacent to a
  `raporo.` GUC may appear in exactly two files, `common/tenancy.py` and `common/db.py`, and
  nowhere else in `common/`, `apps/` or `config/`. It fails with the file and line, not with
  a lecture;
- a mutation requirement in the implementer's report: change `SET LOCAL` to `SET`, show the
  cross-request test go red, restore it, show it green.

### C.3 The three callers

**1. Request path — `common/middleware.py::TenantMiddleware`,** placed immediately after
`AuthenticationMiddleware` and before `MessageMiddleware`:

```
resolve the org  ->  no org?  ->  call get_response with no context and no transaction
                 ->  org      ->  with tenant(org_pk, source="request"): get_response(request)
```

Org resolution rules — the details are security-engineer's, these are the constraints:

- anonymous request → no context. Login, registration, password reset, `/healthz` and
  static all run with no context, and must not need one: `accounts_*` carries no RLS, and
  registration is the sanctioned exception of §C.6.
- authenticated request → `Membership.objects.select_related("role").get(user=request.user)`.
  One query, cached on `request.tenant`, re-read **every** request so a revoked membership
  drops the context on the next one. Nothing in the session participates: a user belongs to
  exactly one organization (§J.3), so there is no current-org selection, no session key and no
  org switcher. `select_related("role")` is required, not an optimisation — §I.3's query
  budget for the store resolver depends on it.
- `DoesNotExist` → no context. `MultipleObjectsReturned` → a violated database constraint,
  therefore a bug: it surfaces as a 500 with an audit row and is **never** resolved by picking
  one membership.
- **the org never changes within a request**, and now cannot change between them either
  without a membership change. No code path re-issues the GUC.

Consequences of the middleware owning a transaction, for `docs/DEVELOPMENT.md` rather than
for discovery:

- every tenant request is one transaction. `ATOMIC_REQUESTS` stays `False` — one mechanism,
  not two.
- template rendering happens inside the middleware chain (`BaseHandler._get_response` calls
  `response.render()` there), so lazy querysets evaluated during rendering are inside the
  transaction and inside the GUC. This property is what makes the design work in a
  server-rendered app and deserves a test of its own.
- a `StreamingHttpResponse` is consumed **after** the middleware returns, so a lazy queryset
  in a streaming response would evaluate with no GUC and yield nothing. Rule: no streaming
  response may lazily query tenant data; materialise first, or do the work in a management
  command. The per-tenant export (§D.5) is a command for this reason among others.
- `idle_in_transaction_session_timeout` must exceed the slowest tenant view, and
  `statement_timeout` must sit below it (§D.3).

**2. Management commands — `common/management/base.py::TenantCommand`.** Adds a required
`--org <uuid>` argument (the public identifier of §E, not a pk), resolves it, and wraps
`handle()` in `tenant(org_pk, source="command")`. A command that must see every organization
does not get a GUC escape hatch — it runs as the migration role, which holds `BYPASSRLS`
(§C.5). A privilege is auditable and revocable; a bypass GUC is a second mechanism and a
gift to an attacker.

**3. Celery, when it arrives (slice 6) — designed, not built.** Every task signature carries
`org_uuid` as its first positional argument; a `@tenant_task` decorator resolves it and
wraps `run()` in `tenant(..., source="task")`. Two rules: context is **never** inherited
implicitly from the enqueuing request (a delayed task can run after the membership is
revoked, so the org must be re-resolved and re-authorised at execution time), and a
registered task without an `org_uuid` parameter fails a **system check at startup**, not at
run time. Nothing is implemented now beyond leaving the door the right shape.

### C.4 Fail-closed, in two layers with different failure modes

| Layer | No context | Wrong context |
| --- | --- | --- |
| Database (RLS, app role) | reads return **zero rows**; `INSERT` violates `WITH CHECK` and raises | same |
| Application | `require_org_id()` raises `NoTenantContextError` | `get_compiler` raises `TenantContextMismatch` |

The database fails closed *quietly* and the application *loudly*, in that combination:

```sql
CREATE OR REPLACE FUNCTION raporo_current_org_id() RETURNS bigint
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT NULLIF(current_setting('raporo.org_id', true), '')::bigint
$$;
```

The `true` second argument and the `NULLIF` are both required.
`current_setting('raporo.org_id')` without them **raises** `unrecognized configuration
parameter` in a session that has never set it, and returns `''` — which then fails
`::bigint` — in a session that has set and cleared it. Two different errors for the same
state. The helper makes that state one value, `NULL`, and a `NULL` comparison makes every
policy predicate false. Requirements for database-engineer: `STABLE` (never `IMMUTABLE`), a
pinned `search_path`, and installation through a **versioned, hash-pinned constant** in
`common/db.py` (`CREATE_CURRENT_ORG_FUNCTION_V1` / `DROP_CURRENT_ORG_FUNCTION_V1`) with its
hash in `PINNED_SQL`, plus the RLS `RunSQL` statements pinned in `PINNED_MIGRATION_SQL`. The
round-4 catch-all enforces the second half by construction: the migration cannot be
committed green without the digest.

The one residual silence worth naming: an `UPDATE` or `DELETE` filtered to zero rows by a
policy is silent. `DELETE` does not exist in this codebase, and `update()` goes through the
guard of §A.8, so the reachable surface is a raw `UPDATE` — which is already the case where
zero rows is the correct answer.

### C.5 Roles — the precondition, stated as an interface to devops-engineer

`FORCE ROW LEVEL SECURITY` subjects the table owner to its own policies. **Nothing subjects
a superuser.** `compose.yaml` connects as `raporo`, which the security gate measured as
owner *and* superuser. Without a role split, RLS is inert in dev and in every environment
where the app connects as owner — the fifth "present, correctly named, documented, never
executed" control of this slice, and the most expensive one, because the whole team would
believe org isolation was enforced by the database.

What I need to exist:

| Role | Owns tables | `BYPASSRLS` | `TRUNCATE` | Used by |
| --- | --- | --- | --- | --- |
| `raporo_migrator` | yes | yes | — | `migrate`, admin/global commands, test-database creation |
| `raporo_app` | **no** | **no** | **no** | the web process; every request |

`raporo_app` also satisfies the devops item already routed in the ledger ("runtime DB role
must not own these tables nor hold TRUNCATE"). Two roles, one database. In dev and test,
`DATABASES["default"]` stays the migrator so `pytest --create-db` and `migrate` work, and a
second alias exists purely so the tests can prove RLS bites:

```python
DATABASES["app"] = {**DATABASES["default"],
                    "USER": os.environ["POSTGRES_APP_USER"],
                    "PASSWORD": os.environ["POSTGRES_APP_PASSWORD"],
                    "TEST": {"MIRROR": "default"}}
```

`TEST["MIRROR"]` is what makes this cheap: Django creates no second test database and runs
no second migration pass; the alias points at the same test database under a different role.
The RLS denial tests declare `databases = {"default", "app"}` and read through
`using("app")`. In production the polarity flips: `default` is `raporo_app`, and the deploy
pipeline's migration step overrides the user — `compose.prod.yaml`'s business, already on
devops' list.

**Tables are `ENABLE`d and deliberately not `FORCE`d** — revision 1 said the opposite and
was overruled by measurement (§J.4): `FORCE` plus `BYPASSRLS` is self-cancelling, and dropping
`BYPASSRLS` from the migration role makes data-migration backfills silently no-op. The
acceptance test is unchanged and is what actually matters: not "the policy exists in
`pg_policies`" but "a read as `raporo_app` with the wrong context returns nothing while the
same read with the right context returns the row".

### C.6 Registration: the one sanctioned no-context write

`register_owner()` creates the organization, so it cannot run inside `tenant(org.pk)` — the
org does not exist yet. Design: it runs with **no** context up to and including the
`Organization` INSERT, then enters `tenant(org.pk, source="request")` for the store, role,
membership, store-access and audit rows.

That requires `orgs_organization` to permit an `INSERT` when no context is set, which is a
carve-out and must be written as one:

- `SELECT`/`UPDATE`/`DELETE`: `id = raporo_current_org_id()`.
- `INSERT`: permitted when `raporo_current_org_id() IS NULL`.

Unauthenticated organization creation *is* the registration flow, so the control on it is
rate limiting and the service layer, not RLS. It appears in the denial matrix (§D.6) as an
explicitly **allowed** case, so nobody later "fixes" it and breaks registration, and nobody
later mistakes it for an oversight.

### C.7 Interaction with the two existing `raporo.*` GUCs

`raporo.allow_truncate` and `raporo.enforce_truncate_guard` (`common/db.py`) share the
namespace. Assessment:

- **Naming is coherent already** — keep `raporo.org_id` in the same namespace, declared as a
  named constant, and add the rule: a `raporo.*` GUC is declared exactly once, in
  `common/tenancy.py` or `common/db.py`, and read only from pinned SQL.
- **No functional interaction.** The truncate GUCs are read by a trigger function; the org
  GUC is read by policy predicates. They never appear in the same statement.
- **One shared property worth recording:** all three are custom GUCs, so all three are
  settable by any role (§B.6), and all three are cleared by `RESET ALL` / `DISCARD ALL`.
  Clearing is harmless for `raporo.org_id` precisely because it is re-set per transaction —
  another way of saying the pooling story in §C.2 depends on `SET LOCAL` and on nothing
  else.
- **One imperative:** do not "save a round trip" by promoting `raporo.org_id` to a
  session-level `SET` at connection creation. That is the leak, and it is the leak *caused
  by* the isolation mechanism. The source-scan test in §C.2 is what makes it unfixable by a
  well-meaning future optimisation.

---

## D. The rest of the round

### D.1 `common.E005`, precisely

E005 today rejects any unique constraint that omits `store` or is not conditioned on live
rows. With `organization` on the row, "unique per organization" is legitimate and E005 must
not reject it. Replace the current pair of checks with a **kinded** rule. Every
`UniqueConstraint` on a concrete store-scoped model is classified into exactly one kind by
the set of column names it references — `constraint.fields` plus `_expression_names()` over
`constraint.expressions`, which the current implementation already computes:

```python
TENANT_COLUMNS   = {"store", "store_id", "org", "org_id"}
IDENTITY_COLUMNS = {"public_id"}
```

| Kind | Test | Requirements | On failure |
| --- | --- | --- | --- |
| 1 — tenant-rooted business key | `referenced & TENANT_COLUMNS` and not `referenced & IDENTITY_COLUMNS` | `_requires_live_rows(condition)` is true | E005, current message and hint |
| 2 — public identifier | `referenced == IDENTITY_COLUMNS` | `condition is None`; the field is `editable=False` | E005, new message |
| 3 — anything else | otherwise | — | E005: "does not include `store` or `org`" |

**Revision 2:** the settled shape for `public_id` is field-level `unique=True` rather than a
named `UniqueConstraint` (§J.2), so kind 2 is reached through the schema plan's own E005
exemption for a non-editable `UUIDField` carrying a default. The *rule* kind 2 encodes is
unchanged and is the reason the exemption is safe — the identity index must be global and
unconditional — so it is argued here and enforced there. The schema plan owns the final E005
decision table; this sub-section is the argument behind two of its rows.

Kind 2 **inverts** both of E005's current demands, which is exactly why it must be a named
kind and not an `if` buried in the old code path:

- it must be **global**: a public identifier that is unique only per tenant is not an
  identifier. Adding `store` to it would let two rows in two stores share a URL.
- it must be **unconditional**: a soft-deleted row keeps its identifier for ever. Conditioned
  on live rows, a tombstone would release its `public_id`, a later insert could take it, and a
  stale URL or an audit reference would resolve to a different row. Reissuing an identifier
  is worse than reserving one.

Unchanged: field-level `unique=True` is still an error **except** on a non-editable
`UUIDField` carrying a default — i.e. `public_id`, and nothing else. Revision 1 demanded a
named `UniqueConstraint` here so it could carry a `violation_error_message`; that argument
fails on its own terms, because `public_id` is `editable=False`, never appears in a form and
can only collide through a UUIDv7 birthday event, so there is no user-facing message to
localise. `unique_together` is still an error. `_requires_live_rows`'s AND-level
handling of nested `Q` is still correct and still needed for kind 1.

Deliberately **not** adjudicated by E005: whether a given business key should be per store
or per organization. `UniqueConstraint(fields=["organization", "name"], condition=LIVE)` on
a table whose natural key is per store is a business bug, not a tenancy leak, and a system
check that guesses intent will be wrong in both directions. E005 refuses leaks; the choice
per model belongs to product-owner and database-engineer.

Both column sets are named in one place — `common.models.TENANT_COLUMNS` /
`IDENTITY_COLUMNS`, next to the base that declares the columns — so the only way to change
the rule is to change the base.

### D.2 Three new startup errors, in the E005 family's style

**Numbering is the schema plan's** (it owns `common/checks.py` this round): `common.E007` is
the org-leading index rule and `common.E008` is "every first-party model has a `public_id`".
`E100` is taken. So revision 1's three proposals reduce to one survivor plus one new check, at
the next free ids:

| id | Subject | Rule |
| --- | --- | --- |
| `common.E009` | `org` on a store-scoped model | non-nullable FK to `orgs.Organization` with `related_name="+"` — mirroring `_check_store_field`/E003 exactly, including its tolerance for a string target in isolated registries |
| `common.E010` | `PRESETS` exhaustiveness (§I.9) | every code in `PERMISSIONS` appears in at least one preset or in a declared `UNASSIGNED` set, so a new code cannot enter the catalog without an explicit per-preset decision |

**Withdrawn from revision 1.** Its `E007` (the `public_id` field's shape) and `E008` (exactly
one kind-2 `UniqueConstraint` on the concrete model) are subsumed: with field-level
`unique=True` there is no separate constraint object to find, and the schema plan's own E008
covers the field's presence. The `ScopedThingOwnMeta` accident that motivated revision 1's E008
— a subclass declaring its own `Meta` and silently losing the base's constraints — cannot
happen to a field-level `unique=True`, because it lives on the field and not in `Meta`. That is
an incidental benefit of the schema plan's shape, and worth recording because it was one of my
five arguments against it.

Both surviving checks are startup errors, not conventions, and both need the coverage standard
the E100 incident set: driven through `django.core.checks.run_checks()`, never by calling the
function directly, and each with a deliberately-broken model under `isolate_apps` so the test
fails when the check is removed.

### D.3 Connection and timeout configuration — devops-engineer's file, my constraints

In `config/settings/base.py`, `DATABASES["default"]`:

- `CONN_MAX_AGE` set explicitly from the environment — a value, not left at the implicit
  `0` — **and** `CONN_HEALTH_CHECKS = True`. The two go together, or a stale persistent
  connection surfaces as a request error instead of a reconnect.
- `OPTIONS`: `connect_timeout`, `application_name`, and via libpq `options`:
  `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`.
- Ordering constraint from §C.3: `statement_timeout` < slowest tenant view <
  `idle_in_transaction_session_timeout`, because the middleware holds a transaction open for
  the whole request. Pick the numbers from measurement, not from taste, and record them in
  `docs/DEVELOPMENT.md`.
- psycopg connection pooling (`OPTIONS["pool"]`) is deliberately **not** enabled: one web
  container with persistent per-worker connections is enough at this scale, and a pool adds
  a second connection lifecycle to reason about in the same round that introduces the GUC.
  Revisit when workers × replicas approaches `max_connections`. For the record, the pool
  would be *safe* under this design, and only because of `SET LOCAL`.
- The export command (§D.5) needs `SET LOCAL statement_timeout = 0` — a per-command override
  inside `tenant()`, never a global relaxation.

### D.4 Tenant-leading indexes, and `common.E007`

Rule: **every `Index` on a store-scoped model leads with `organization` or `store`.** After
§A.5 every scoped query emits `org_id = %s AND store_id IN (...)`, so a
tenant-leading composite is the shape the planner wants and a non-leading index is a promise
the query shape cannot keep.

`common.E007` enforces it as a startup error with no escape hatch (renumbered in
revision 2 to the schema plan's id — same rule, one owner). The one case that looks
like a counter-example — the architecture spec's partial index on
`expiry_date WHERE expiry_date IS NOT NULL` — is not one: the real query is "what expires
soon *in this store*", so `Index(fields=["store", "expiry_date"], condition=...)` is both
compliant and better. If a genuinely global index is ever needed on a store-scoped table,
that is an argument to have in review, not a flag to set in `Meta`.

Naming convention for database-engineer: `<table>_org_store_<col>_idx`. Unique constraints
are not `Index` objects, so E007 does not see them and the global `public_id` unique index is
untouched — the correct outcome, and worth a comment because it will look like an
inconsistency.

### D.5 Per-tenant export and delete

**Export — buildable now.** `apps/orgs/management/commands/export_org.py`, a
`TenantCommand` subclass, `--org <uuid>`. Iterates a declared table order and writes NDJSON
per model with `.iterator()`; includes soft-deleted rows, which are part of the record, and
the organization's audit rows. Two content rules make the output re-importable and id-free:
emit `public_id`, never the bigint `id`; represent every FK by the target's `public_id`. It runs
inside `tenant()`, so RLS is what proves it cannot over-read — and a denial test asserts an
export produced under org A's context contains no row belonging to org B.

**Delete — blocked, and correctly so.** Follow-up 4 in `HANDOFF.md` (the
`privacy-compliance` ruling on Rwanda Law No. 058/2021) already gates Task 4, and it gates
this. Three structural facts constrain whatever it decides:

1. There are no hard deletes anywhere, and `audit_auditlog` is append-only *by database
   trigger*, so erasure cannot mean `DELETE`.
2. It cannot mean "overwrite the PII columns of every row" either, because on
   `audit_auditlog` an `UPDATE` is refused by the same trigger.
3. Therefore erasure is `anonymize_org(org)`: overwrite PII in place on the mutable tables,
   soft-delete every row, write one final audit entry — **and it only works if audit rows
   never contained PII in the first place.**

That last point is the interface I need from `privacy-compliance`, and it is implementable
either way the ruling goes: a per-model declared `PII_FIELDS`, a guarantee that
`audit.record` writes none of them (today's `SENSITIVE_KEY_PARTS` denylist becomes a
positive rule derived from `PII_FIELDS`), and a system check that every model with candidate
PII declares the tuple. Do not build `erase_org` before the ruling; do build `PII_FIELDS`
and its check, because the ruling needs it whichever way it lands.

### D.6 The generated cross-tenant denial matrix

`tests/test_tenancy_matrix.py`. Generated from the model registry, never enumerated, because
the point is that **a model added in slice 2 acquires coverage without anyone remembering**.

- **Subjects:** every concrete `StoreScopedModel` subclass, plus the org-level spine
  (`Store`, `Role`, `Membership`, `StoreAccess`) and `AuditLog`, discovered via
  `django.apps.apps.get_models()`.
- **Shapes:** the pinned read against a rival store; the unpinned read; `all_objects`; the
  existing 45-case operator × operand-order matrix; every write path (`create`,
  `get_or_create`, `update_or_create`, `bulk_create`, `update`, `bulk_update`,
  `soft_delete`); `raw()`; and raw SQL executed **as `raporo_app` through the `app` alias**,
  which is the RLS layer.
- **Contexts:** correct org, rival org, no context.
- **Assertion discipline, from this slice's most expensive lesson:** every result is
  materialised (`sorted(r.public_id for r in qs)`), and every "returns nothing" assertion is
  preceded by proving the row *is* returned under the correct context. An empty result and a
  working guard are otherwise indistinguishable.
- **The anti-rot mechanism:** a `TENANCY_FACTORIES` registry maps each model to a callable
  that builds one row in a given store, and a premise test fails when a concrete
  store-scoped model has no factory, printing the model label and the factory signature to
  paste. Same shape as `test_every_run_sql_statement_in_every_migration_is_pinned`, which
  the database-engineer measured as "enforced by construction, cannot be made green by
  omission".
- **Allowed cases are in the matrix too**, explicitly: the no-context `Organization` INSERT
  of §C.6, and `all_objects` returning tombstones. A matrix that lists only refusals invites
  someone to "fix" a legitimate path.

---

## E. UUIDv7 placement

### E.1 Which base

New abstract base in `common/models.py`, above `AuditedModel`:

```python
class PublicIdModel(models.Model):
    public_id = models.UUIDField(_("public id"), db_default=UUID7(),
                                 editable=False, unique=True)

    class Meta:
        abstract = True
        # `unique=True` above is the index; see §J.2
```

Mixed in at three points, which covers everything:

- `SoftDeleteModel(PublicIdModel)` → `Organization`, `Store`, `Role`, `Membership`,
  `StoreAccess`, and every `StoreScopedModel` by inheritance;
- `AuditLog(PublicIdModel, models.Model)` → explicitly, because a future audit screen
  links to rows by URL and `AuditLog` is not soft-deletable;
- `accounts.User` → explicitly, because it descends from `AbstractBaseUser`, and because
  member-management URLs (Task 10) need a user identifier that is not the username.

`unique=True` lives on the field, so unlike a `Meta.constraints` entry it cannot be lost by a
subclass that declares its own `Meta` — the `ScopedThingOwnMeta` accident `common.E002` exists
for does not reach it. The schema plan's `common.E008` still asserts every first-party concrete
model carries the base, because inheriting the base is the part a new model can forget.

### E.2 `db_default=UUID7()` or a Python default? ~~Python default.~~ **Withdrawn — see §J.2**

> **Revision 2: this sub-section is withdrawn.** PostgreSQL 18.6 is confirmed in this stack, so
> its heaviest argument (decoupling the identifier from an unverified platform bump) no longer
> exists, and the database-engineer measured the alternative directly. The settled shape is
> `public_id = UUIDField(db_default=UUID7(), editable=False, unique=True)` on `PublicIdModel`.
> §J.2 gives the full rename table and keeps the one residual that still matters (an unsaved
> instance's `public_id` is a `DatabaseDefault` sentinel — never render one). The reasoning
> below is kept only as the record of a decision that was reversed on evidence.

The reversed argument, in one paragraph rather than five, because the branch it protected is
dead. Revision 1 chose `default=uuid.uuid7` chiefly to decouple the identifier from a PG18 bump
that was still unverified — Django raises
`NotSupportedError("UUID7 requires PostgreSQL version 18 or later.")` below 18, so a
`db_default=UUID7()` would have blocked step 1 on step 9. PostgreSQL 18.6 is now confirmed in
this stack, so that reason is gone, and the schema plan measured the rest: the write cost is
lower than UUIDv4's, and `Field.db_returning` (which is `has_db_default()`) makes the value
available after `create()` on the same `INSERT ... RETURNING`. Adopt `db_default=UUID7()`.

**The one residual, kept as a rule with a test.** A field with `db_default` returns a
`DatabaseDefault` sentinel from `get_default()`, so an **unsaved** instance's `public_id` is an
expression object rather than a UUID. `Model.clean_fields()` skips it, but
`Model.validate_unique()` and `UniqueConstraint.validate()` read `getattr(instance, attname)`
and would build `WHERE public_id = UUIDV7()` — on PG18 a wasted query with a nonsense
predicate, reachable only from a hand-written `full_clean()` on an unsaved object, since
`public_id` is `editable=False` and appears in no `ModelForm`. The visible face of the same
fact matters more in a template-rendered HTMX app: **never put an unsaved object's `public_id`
in a template**, or a `DatabaseDefault` renders into a DOM id. `create()` populates it, so the
window is narrow, and one test on a fragment rendered from an unsaved instance pins it.

### E.3 Indexed separately? No.

`db_index` is not set. On PostgreSQL `unique=True` **is** a unique B-tree index, and it is the
index the URL lookup uses — the schema plan measured
`Index Scan using sale_public_id_uniq on sale`. A second index would be redundant and would
cost a write on every insert.

This looks inconsistent with the deliberately redundant plain index on `accounts_user`, so
the reason is worth recording: that one exists because an **expression-only**
`UniqueConstraint` raises a `NON_FIELD_ERRORS` `ValidationError`, so the field-level unique
was what produced per-field form errors. `public_id` is `editable=False` and never appears in
a form, so it has no form errors to shape. The exception does not generalise.

### E.4 What it means for URL design in an HTMX app

- **The bigint pk never leaves the process.** Not in a URL, not in HTML, not in an `HX-*`
  header, not in a DOM id. The `public_id` is the only identifier that crosses the boundary:
  `id="sale-{{ sale.public_id }}"`, `hx-target="#sale-{{ sale.public_id }}"`,
  `{% url 'sales:detail' sale.public_id %}`.
- **Routes use Django's built-in converter**: `<uuid:sale_public_id>`. It accepts only the
  canonical hyphenated lowercase form, so a malformed identifier is a 404 from the resolver
  and never reaches a view.
- **No organization in the URL.** The active org comes from the tenant context, so paths are
  `/stores/<store_public_id>/sales/<sale_public_id>/` and there is no org-shaped enumeration surface at
  all. Rejected alternative: `/o/<org-slug>/…` — readable, but it puts a mutable, user-chosen
  value in every URL and gives multi-org users a way to address an org they are not currently
  in, which then needs its own authorization check on every route.
- **`public_id` is not an authorization control.** Three layers, three jobs: the `public_id` removes
  *enumeration*, the pin removes *authorization risk*, RLS removes both when the app forgets.
  Concretely, views never call `.get(public_id=…)`; they call one selector,
  `common/selectors.py::get_scoped(model, public_id, *, store)`, which is
  `model.objects.for_store(store).get(public_id=…)` — so a valid identifier belonging to another tenant
  raises `DoesNotExist` and renders a 404, with no oracle in the difference between "wrong id"
  and "not yours" — the same 404 rule §I.7 states for the store dimension.
- **HTMX-specific:** a lost tenant context on a fragment request must respond with
  `HX-Redirect`, not an HTML redirect — an HTML redirect gets swapped into the fragment target
  and the user sees a login page inside a table cell. Every fragment endpoint also answers a
  full-page GET, per the architecture spec's progressive-fallback rule, so both shapes need
  the same selector.
- **Enforcement:** "no pk in the DOM" is a Task-8 acceptance criterion owned by
  frontend-engineer and code-reviewer, per the tech-lead's rule that a control in a handoff
  note is how E100 shipped inert. Each screen lands with an integration test asserting its
  rendered fragment contains the `public_id` and not the pk.
- **`audit.record`** gains `target_public_id` alongside `target_type`/`target_id`, so an audit
  screen can link to a row without a join. A new nullable column on an append-only table is a
  plain `AddField`; no existing row is rewritten.

### E.5 Do `Organization` and `Store` need one, given the slug?

**Yes, both.** The slug is not an identifier:

1. `orgs_organization_unique_live_slug` is conditioned on live rows, by design and with a
   test — *"a soft-deleted org releases its slug"*. An identifier another row can later take
   is not an identifier.
2. It is mutable and user-chosen, so a URL keyed on it breaks on rename and leaks the
   organization's name into referrers, proxy logs and shared links.
3. `Store` has no slug at all, and its name is unique only per org among live rows.

The slug keeps its job — human-facing text in report filenames, share cards and branding —
and stops being a routing key. Since §E.4 keeps the org out of URLs entirely, that is a
demotion with no replacement cost.

### E.6 The pk stays

`BigAutoField` remains the primary key. The `(id, org_id)` composite-FK targets are already
built on it, every FK stays 8 bytes rather than 16, index locality on joins is better, and
`_store_pk` / `ScopePin` / `AuditLog.target_id` are integer-typed throughout. The cost is two
identifiers per row and one rule to hold: **the pk never leaves the process; the `public_id` never
enters a `WHERE` clause without a pin.**

---

## F. Sequencing

Nine steps. Each is one commit, independently verifiable and independently revertible. The
order is chosen so no step can be armed before the thing it depends on has been *watched
working*.

One fact makes this much cheaper than it looks: **`StoreScopedModel` has no concrete subclass
in any production app today.** `catalog`, `inventory`, `sales`, `money` and `reporting` do not
exist yet; the only concrete subclasses live in `tests/testapp`, which `config.settings.test`
installs alone and which carries its own migration. So the abstract-base change in step 2
needs **no production migration at all**. This is the last moment that will be true.

| # | Step | Touches | Independently verified by | Revert |
| --- | --- | --- | --- | --- |
| 0 | Prerequisites already on the list: the 14 `origin/dev` add/add conflicts, and the `privacy-compliance` ruling | docs | `git merge-tree` rc=0 | n/a |
| 1 | **Public identifier.** `PublicIdModel`; `public_id` on `SoftDeleteModel`, `AuditLog`, `User`; the schema plan's `common.E008`; **the E005 rewrite (§D.1)** | `common/models.py`, `common/checks.py`, `accounts/0002`, `orgs/0002`, `audit/0003`, `testapp/0002` | `manage.py check` green; a public_id on every row; E005 accepts a kind-2 and an org-rooted kind-1 constraint and still refuses a bare one | drop 4 columns |
| 2 | **`org` on `StoreScopedModel`** + `same_org_fk_v1()` in `common/db.py` + `_derive_org` + the write-path fills | `common/models.py`, `common/managers.py`, `common/db.py`, `testapp/0003` | `pg_constraint` shows the key; a cross-org write is refused by the database; a store's org cannot be updated while it has rows | drop 1 column + 1 constraint |
| 3 | **The pin becomes `(org, stores)`** (§A) | `common/managers.py` only | the 45-case matrix before/after with mutation output; the query-count table of §A.5 | pure Python revert |
| 4 | **Tenant context** (§C) — `common/tenancy.py`, `TenantMiddleware`, `TenantCommand`, connection settings | `common/tenancy.py`, `common/middleware.py`, `config/settings/*` | the GUC is set inside and absent outside the transaction; the two-requests-one-connection test; the `SET ` source scan | remove middleware |
| 5 | **RLS** — roles, `ENABLE` + `FORCE`, `raporo_current_org_id()`, policies, the `app` alias | `common/db.py`, `orgs/0003`, `audit/0004`, `config/settings/*` | a read as `raporo_app` with the wrong context returns nothing while the right context returns the row | `DISABLE ROW LEVEL SECURITY` |
| 6 | **Tenant-leading indexes + `common.E007`** | `common/checks.py`, migrations | `EXPLAIN` on a pinned read uses the leading index | drop indexes |
| 7 | **The generated denial matrix** (§D.6) | `tests/` only | it goes red when any guard from steps 2–5 is mutated | delete tests |
| 8 | **Per-tenant export** (§D.5); `PII_FIELDS` + its check | `apps/orgs/management/`, `common/checks.py` | an export under org A contains no org-B row | delete command |
| 9 | **Platform bump** (Python 3.14 + PostgreSQL 18) — devops, already in the working tree | `docker/`, `compose.yaml` | suite green on both | revert images |

Notes on the order that matter:

- **Step 1 and the E005 rewrite are one commit, not two.** The identity constraint is a shape
  E005 currently rejects, so shipping the column first breaks `manage.py check` on every
  store-scoped model.
- **Step 4 strictly before step 5.** Arming RLS against an unproven context is how you get an
  outage that looks like a data-loss incident. Step 4 is fully verifiable on its own: the
  GUC's presence and absence are observable without a single policy existing.
- **Step 3 after step 2**, because the pin's org predicate needs a column to filter on. Step 3
  is otherwise pure Python and reverts cleanly, which matters because it edits the most-tested
  file in the project.
- **Step 9 is done.** The platform bump landed and was verified by execution: Python 3.14.7,
  Django 6.1, PostgreSQL 18.0006, `supports_uuid7_function = True`,
  `supports_virtual_generated_columns = True`, 369 tests green. So `UUID7()` is available to
  step 1 and there is no fallback branch to carry (§J.2). Steps 1–8 did not depend on it
  anyway: RLS is ancient and the composite FK is ordinary SQL.
- **Slice 2's precondition, not slice 1's:** `same_org_fk_v1()` and the RLS install helper
  must exist as versioned, hash-pinned helpers before the four ledger tables land, or each of
  them hand-rolls its own SQL. Same lesson `append_only_triggers_v1` already taught, at the
  cost of a fix round.

### What must be true before Task 4 (the service layer) starts

1. **Steps 1–5 landed and gate-passed.** Task 4 writes the first services and the first
   `audit.record` calls for user events; both need the identifier, the org column and the
   context to exist, or every service written now gets rewritten in slice 2.
2. **The `register_owner` context boundary is decided and encoded** (§C.6). It is the one
   service that legitimately begins with no tenant context, and the `orgs_organization`
   INSERT carve-out must exist before its first test runs.
3. **`TenantCommand` and `tenant()` exist**, because `create_store`'s `SELECT … FOR UPDATE`
   on the org row (per the slice-1 plan) must run inside the same transaction that carries
   the GUC. Two transactions per service call would put the lock and the tenant predicate in
   different places.
4. **The two items already gating Task 4 are closed:** the `origin/dev` conflict resolution
   and the `privacy-compliance` ruling. The second is now doubly load-bearing — it also
   decides §D.5's delete half.
5. **The denial-matrix skeleton (step 7) exists even if thin**, so every service Task 4 writes
   lands with generated cross-tenant coverage rather than acquiring it in a later fix round.

---

## G. Where I would push back

Design to the decisions as written; these are on the record because the ledger says three
implementers have overruled a brief on this project and all three were right.

1. **Decision 2 (RLS now) is right, and it is not implementable as stated without a companion
   that is not on the list.** RLS is inert for a superuser and, without `FORCE`, for the table
   owner. `compose.yaml` connects as `raporo`, which the security gate measured as **both**.
   So "RLS lands in slice 1" is true only if the `raporo_app` / `raporo_migrator` role split
   lands in slice 1 too (§C.5). If devops cannot deliver the role split in this round, my
   recommendation is to **land steps 1–4 and 6–8 and hold step 5**, rather than ship policies
   that no environment enforces. Shipping RLS without the role split would be the fifth
   control this slice that is present, correctly named, documented and never executed — and by
   far the most consequential, because the whole team would then believe organization isolation
   was a database fact.

2. ~~**Decision 3 (UUIDv7), one detail:** not `db_default=UUID7()` in this round.~~
   **Withdrawn in revision 2.** The push-back's main premise — an unverified PG18 bump — was
   resolved by verification, and the database-engineer measured `db_default=UUID7()`'s write
   cost and confirmed `db_returning` populates the value with no extra round trip. Adopt
   `db_default=UUID7()` (§J.2). I record the reversal rather than deleting it: the argument was
   sound on the information available, and it was evidence that changed it, which is the
   standard this project holds itself to in both directions.

Two smaller notes, not disagreements:

- **`Organization.slug` should stop being described as an identifier anywhere.** It cannot be
  one: `orgs_organization_unique_live_slug` is conditioned on live rows *by design*, with a
  test named for it. Anything already written that treats the slug as stable needs a line
  changed.
- **The export/delete item is one buildable half and one blocked half.** Export can be built
  now; erasure cannot be designed before the Law 058/2021 ruling, because the append-only
  audit trigger forecloses both obvious mechanisms (§D.5). Building `PII_FIELDS` and its
  system check now is the part that is useful under either ruling.

---

## H. Self-review (revision 1 — see §J.7 for revision 2)

- **Placeholders:** none. Every file and symbol named above is either an existing path or a
  new one specified with its module, name and signature.
- **Contradictions checked:** §A.6 removes a query the ledger recorded as a deliberate cost —
  §A.6 discharges that carry-forward explicitly and names the required mutation evidence. §B.2
  argues against store-level RLS while §B.1 endorses the split; both rest on the same "one
  value per request" property, stated once. §E.2 and §F both depend on PG18 *not* being a
  prerequisite; §F step 9 says so in one line.
- **Ambiguities resolved by naming them:** the `register_owner` no-context boundary (§C.6);
  which tables get RLS and which deliberately do not (§B.5); whether E005 should adjudicate
  per-store versus per-org business keys — it should not (§D.1).
- **Left to other roles, with the constraint stated rather than the internals:** migration
  shapes and index definitions (database-engineer); the `audit_auditlog` policy text and the
  GUC threat model (security-engineer); roles, connection numbers and the platform bump
  (devops-engineer); the PII declaration and the erasure pathway (privacy-compliance).
- **Not verified by me, and it should be before step 5 lands:** that `FORCE ROW LEVEL
  SECURITY` plus a non-owner role actually refuses in this stack. Everything in §C.5 is
  reasoned from PostgreSQL semantics and from the security gate's measurement that the app
  role is owner and superuser. Per this slice's own standard, reasoning is not evidence — the
  first thing step 5 must produce is a watched refusal.

---

## I. The permitted store set — Elvis's owner ruling (revision 2, 2026-09-02)

> *"A user may access more than one store, and only if they were given access to both of
> them. But the owner of the org can access any store under their org."*

This section is the subject of revision 2 and it is the only place the owner override exists.
It is recorded as **ADR 0011**. §J lists every earlier statement in this document that
revision 2 corrects, including the one this ruling contradicts outright.

### I.0 What this contradicts, stated before it is fixed

`StoreAccess`'s docstring reads *"Which stores a membership may work in. Materialised even for
owners: explicit rows beat an implicit 'owners see everything' rule."* The implicit rule it
rejected is the one Elvis has now asked for. That sentence must not survive the change: as
written it states a design rule the system will no longer follow, and a future reader would
trust it. §I.4 gives the replacement text verbatim.

### I.1 The resolver

**One function. Its name and signature:**

```python
# apps/orgs/services/access.py

def permitted_stores(membership: Membership) -> StoreSet:
    """Every live store this membership may reach, and how it got them.

    The only place the org-wide override exists. Nothing else in the codebase
    reads `StoreAccess` or decides which stores an actor may reach.
    """
```

```python
@dataclasses.dataclass(frozen=True, slots=True)
class StoreSet:
    org_pk: int
    stores: tuple[Store, ...]      # live, in Store.Meta.ordering (name) order
    via: str                       # "access_all" | "store_access" — messages and audit, never control

    def __bool__(self) -> bool: ...                       # False when the set is empty
    def __contains__(self, store: Store | int) -> bool: ...
    @property
    def store_pks(self) -> tuple[int, ...]: ...
    def by_public_id(self, public_id: uuid.UUID) -> Store | None: ...
```

Two derived gates live in the same module, are the only callers of the resolver that views
ever touch, and contain no policy of their own:

```python
def require_store(membership: Membership, public_id: uuid.UUID) -> Store:
    """The store behind a URL identifier, or `StoreNotPermitted` (rendered 404)."""

def require_store_permission(
    membership: Membership, public_id: uuid.UUID, code: str
) -> Store:
    """`require_store` + `Role.has(code)`. Both gates, one call, so neither is forgotten."""
```

**Why it takes a `Membership` and not a `User`.** The membership *is* the actor in this
domain: it carries the user, the organization and the role, which are the three inputs the
answer depends on. A `User` argument would make the function resolve the membership as a side
job and re-query what every caller already holds. After the one-org-per-user ruling (§I.6) the
membership is a total function of the user, so nothing is expressible with a `User` that is not
expressible with a `Membership`.

**It refuses rather than guessing.** A soft-deleted membership, or a membership whose `org_id`
disagrees with the active tenant context, raises `TenantContextMismatch` (§B.4's type, reused).
The org disagreement check is cheap and loud, and it is what stops a resolver call made under
the wrong context from returning a plausible-looking wrong set.

**An empty set is a legitimate answer, not an error.** A member whose only store was
soft-deleted, or whose access was revoked, has zero reachable stores. That is a 200 with an
empty state ("you have no stores; ask your administrator"), not a 404 and not an exception.
`StoreSet` is falsy so the view layer can branch on it. What *is* refused is trying to *pin* an
empty set: `for_stores(())` would raise `ValueError` naming a function the caller never called
— the exact failure `merge_scope_pks`'s docstring already records — and a pin of no stores
reads as "unpinned" downstream, which is worse than an error. So `StoreSet.pin()` raises
`NoPermittedStores` on an empty set, and the empty case is handled before any query is built.

**The bodies, both branches:**

```python
    if membership.role.has(STORE_ACCESS_ALL):
        stores = Store.objects.filter(org=membership.org_id)
        via = "access_all"
    else:
        stores = Store.objects.filter(
            org=membership.org_id,
            access__membership=membership,
            access__deleted_at__isnull=True,
        )
        via = "store_access"
```

Three things in there are load-bearing:

- `Store.objects` is the `SoftDeleteManager`, so both branches are live-rows-only for free.
  `Store` is org-level, not store-scoped, so no pin is needed to read it — this is the one
  table the resolver may query without already knowing the answer.
- `access__deleted_at__isnull=True` is the **revocation path**. Without it a soft-deleted
  `StoreAccess` row still resolves, and revocation silently does nothing. It gets its own
  test, and the test asserts the store *is* present before the revocation, so an empty result
  is not mistaken for a working guard.
- `org=membership.org_id` appears in **both** branches although the member branch does not
  need it (the composite FK already guarantees a `StoreAccess` row cannot mix organizations).
  It is there so the two branches emit the same predicate shape and both use the org-leading
  index the schema plan's `common.E007` requires. A free predicate that makes two plans
  identical is worth writing.

### I.2 Who calls it, and how a command or a task gets one

**Not the middleware.** `TenantMiddleware` (§C.3) resolves the *organization*, because the org
is one value for the whole request and it has to be known before the first statement runs.
The permitted store set is not that: it is consulted zero times on a login page and three times
on a sales screen, and resolving it eagerly on every request would cost a query on paths that
never look at a store. The middleware's job stops at putting the resolved `Membership` on
`request.tenant`.

**The service layer calls it, and the view layer calls only the gates.** Concretely:

| Caller | Gets its `Membership` from | Calls |
| --- | --- | --- |
| A view addressing one store (every detail page and HTMX fragment) | `request.tenant.membership` | `require_store_permission(membership, store_uuid, code)` |
| A view listing across stores (the consolidated report, the store picker) | same | `permitted_stores(membership)` |
| A service that must not trust its caller (`record_sale`, `restock`) | its `actor` argument | `require_store_permission(...)`, again — the gate is idempotent and cheap, and a service that trusts the view is a service that is unsafe from a future DRF endpoint |
| `TenantCommand` (§C.3) | `--as-member <user public id>`, optional | `permitted_stores(membership)` |
| A future `@tenant_task` (§C.3) | `membership_public_id` in the task signature, re-resolved at execution time | `permitted_stores(membership)` |

A management command that operates on the *whole* organization does not get a membership and
does not call the resolver: it runs under `tenant(org_pk, source="command")` and pins with
`for_stores(Store.objects.filter(org=org_pk))` explicitly. That is the honest shape — the
command is not acting as a person, so there is no permitted set to resolve, and writing
`--as-member` would invite someone to fake an actor to widen a scope. `export_org` (§D.5) is
in this category.

For a task the rule from §C.3 carries over unchanged and matters more here: the membership and
its store set are **re-resolved at execution time, never inherited from the enqueuing
request**. A task queued while someone was an owner must not run as one after the demotion.

### I.3 How the override composes with `for_stores()`

`for_stores()` pins a set and refuses one spanning two organizations. For an owner the set is
"every live store in the org", and the question is whether that arrives as a list of ids or as
a subquery.

**Materialised list of ids. A subquery is not a close second, it is unimplementable.**

1. **`merge_pins` is set algebra over integers** (§A.6): union, intersection, and an
   `org_pk` equality test. A subquery pin cannot be unioned with another pin, cannot be
   intersected, cannot be compared for the `left == right` case, and cannot be printed in the
   error message that names both organizations. The 45-case operator matrix reads
   `query.store_scope_pks` as a tuple. Making the pin a subquery would mean redesigning the
   merge algebra to buy nothing.
2. **The cap is five.** `MAX_STORES_PER_ORG = 5`, enforced under a row lock by `create_store`.
   `store_id IN (1,2,3,4,5)` against an `(org_id, store_id, …)` leading index is the plan the
   planner wants. A subquery adds a hash semi-join per statement and hides the cardinality.
3. **A materialised list is a snapshot with a known age** — this call, this transaction. A
   subquery re-evaluates per statement, so an owner's set could change *between two statements
   of one request*. That is the same hazard §B.2 rejects one layer down when it refuses a GUC
   that changes mid-request, and it would make a report's totals disagree with its own row
   list.

**Query cost, which is the specific question asked.** `_store_pks()` issues one query today to
resolve ownership. The owner path does **not** add a second, because the resolver returns
`Store` *instances* and §A.4's instance fast path then builds the pin with no query at all:

| Step | Member | Owner |
| --- | --- | --- |
| resolve the membership + role | 0 (shared with `TenantMiddleware`, see below) | 0 |
| `permitted_stores()` own query | 1 (`Store` ⋈ `StoreAccess`) | 1 (`Store` by org) |
| `for_stores(store_set.stores)` | **0** (instances carry `org_id`) | **0** |
| total, per request | 1 | 1 |

Two conditions make the zeros real, and both are constraints on other sections:

- `TenantMiddleware`'s per-request membership re-validation (§C.3) must
  `select_related("role")`. It fetches that row anyway; without `role` the resolver fetches it
  again and the table above gains a query in both columns.
- §A.4's instance fast path must be built, and its premise — a `Store` instance loaded from the
  database carries a correct `org_id` — is what §B.3's composite FK makes a database fact. If
  that key is ever dropped, this row of the table goes with it.

`for_stores()` still refuses unknown ids and still refuses a cross-org set. The resolver never
hands it either, which is the point: **ids the resolver produced have already been proven live
and in-org, and ids from a URL never reach `for_stores()` at all** — they reach
`require_store()`, which resolves them against the permitted set.

**If the cap ever rises.** The list stays the right answer to roughly one hundred stores. Past
that, `IN` lists of hundreds of literals start to cost plan-cache churn and statement size, and
the answer is still not a subquery: for an `access_all` actor, `org_id = %s` **alone** selects
exactly the enumerated set, so the store predicate can be dropped. That means
`ScopePin(org_pk, store_pks=None)` as an explicit "org-wide" value and `merge_pins` treating
`None` as the top of the lattice — a real change to the merge algebra with its own tests, not a
flag. Recorded as a designed revisit trigger with its condition, not built.

### I.4 What `StoreAccess` is for now

**Ruling: owner memberships get no `StoreAccess` rows, and the docstring's argument does not
survive.**

The argument was auditability: an explicit row is a reviewable grant. It fails on its own
terms once the resolver ignores those rows for `access_all` roles, because then the row is not
the grant — it is a **decoy**. Someone auditing "who can reach store A2" would read
`orgs_storeaccess`, find no row for the owner, and conclude correctly today; find a row and
conclude nothing, because the row neither grants nor withholds. A reviewable artefact that does
not control the thing it appears to control is worse than its absence.

Two further costs settle it. Materialised owner rows must be *maintained*: `create_store` fans
out one row per owner membership, `soft_delete_store` retracts them, and a role edit that adds
or removes `store.access_all` rewrites them. That is a denormalisation with an invalidation
problem, which is precisely what the override was chosen to avoid. And when the two disagree —
and they will — the resolver wins, so the rows were never authoritative.

**What replaces the audit story, and why it is better.** The grant is now the role, and role
edits already go through a service that requires `role.manage` and writes an `audit.record`
row. So "who can reach every store, and who gave them that" is answered by the audit trail of
role edits plus the membership's current role. That records the *decision* ("grant
`store.access_all` to the role named X") rather than its consequences ("five rows appeared"),
which is the artefact a reviewer actually wants.

**`StoreAccess`'s job, stated positively:** it is the complete and exclusive record of
store access for every membership whose role does **not** hold `store.access_all`. It is no
longer a complete record of who can reach a store.

**Replacement docstring** — this is the text, because the current one states a rule the system
will not follow:

```python
class StoreAccess(SoftDeleteModel, AuditedModel):
    """Which stores a membership may work in — for every membership whose role does
    *not* hold `store.access_all`.

    A role holding that code reaches every live store in its organization and gets no
    rows here (ADR 0011): a row that does not control access would be a decoy for
    anyone auditing who can reach a store. The grant for such a role is the role
    itself, and role edits are audited, so the reviewable artefact is the decision
    rather than its fan-out. `apps/orgs/services/access.py::permitted_stores()` is the
    only reader of this table and the only place the two branches meet.

    `org` is denormalized so the database can hold `(membership, org)` and
    `(store, org)` together and refuse a row that mixes two organizations.
    """
```

**The demotion hazard, closed by construction.** A membership promoted from Manager to Owner
may still carry `StoreAccess` rows from before. They are inert while the role holds
`store.access_all` — and they become that membership's entire store set the instant it is
demoted, silently, to whatever it happened to hold months earlier. No database constraint can
express "no rows for a membership whose role's JSONB permission list contains this string", so
this is a service invariant with a test:

```python
def set_membership_role(
    membership: Membership, role: Role, actor, *, stores: Sequence[Store] | None = None
) -> Membership:
```

Moving *to* a role holding `store.access_all` soft-deletes the membership's `StoreAccess` rows
in the same transaction. Moving *away* from one requires `stores` and refuses `None`, so the
new store set is stated rather than inherited from stale rows. Both directions land with a
denial test.

### I.5 Revocation and the stale-set problem

The reference checklist is explicit that the permitted-store list must not live in a token,
because revoking access must take effect immediately. We use session auth, not JWT, but a
Django session is a token by that standard the moment the application trusts it without
re-reading the source of truth. So:

**Where the set may be cached: nowhere. The resolver queries on every call.**

| Location | Ruling |
| --- | --- |
| `request.session` | **Never.** This is the token the checklist forbids. A session outlives a revoked `StoreAccess` row, a demotion, and a soft-deleted store. |
| A signed cookie or any client-held value | **Never**, same reason, plus the client can replay an old one. |
| Redis / `django.core.cache` | **Not in slice 1**, and there is no async infrastructure to invalidate it with. Adding one would mean designing an invalidation protocol for a saving of roughly one millisecond. |
| The `TenantContext` (per request, per transaction) | **Permitted but deliberately not used** — see below. |
| A module-level or class-level dict | **Never.** A WSGI worker thread reuses its context; this is the application-layer twin of the `CONN_MAX_AGE` leak §C.2 exists to prevent. |

**Why not even the per-request memo, which is free.** A memo on `TenantContext` would be
correct across requests and wrong *within* one, in a way that bites the primary user. An
owner's set changes whenever a store is created or soft-deleted, and `create_store` runs inside
a request: memoised, the owner would create a store and then be unable to see it until the next
page load, because the memo was warmed before the insert. Closing that means an
`invalidate_permitted_stores()` hook and two call sites (`create_store`, `soft_delete_store`)
that a third mutation path in slice 2 will forget. Two indexed queries returning at most five
rows do not buy that. So: **no memo, and therefore no invalidation triggers to enumerate, for
either kind of actor.**

That is also the honest reading of "immediately": revocation takes effect at the *next check*,
not at the next request. Every gate is a check.

**The two invalidation triggers, recorded because they are what a memo would have to handle**
and because the difference is the thing Elvis asked about. A member's set changes on a
`StoreAccess` grant or revoke, or a role change. An owner's set changes on **`create_store` and
`soft_delete_store`** — mutations of the *store roster*, which touch no row belonging to that
membership at all. That asymmetry is exactly why a memo is a bad trade: the owner's
invalidation trigger lives in a service that has no reason to know the resolver exists. If a
profile ever justifies a memo, it must be keyed on the org's store roster and not on the
membership, and it needs both hooks and both tests before it is worth anything.

**What §A.4's `store_org_cache` may still hold.** That memo maps `store_pk -> org_pk` and is
sound for the reason §B.3 gives: a store cannot change organization while it has any business
row. It says nothing about whether a store is *live* or *reachable*, and it must never be used
to answer either. Liveness is the resolver's query; reachability is the resolver's branch.
Keeping those three facts in three places, with the cache holding only the immutable one, is
what makes the cache safe while the store set is not cached at all.

### I.6 One organization per user — what it removes from this document

A database-engineer is landing Elvis's other ruling (a user belongs to exactly one
organization) as a live-conditioned unique constraint on `Membership.user`. The structural
consequence for this design is that **an authenticated user has exactly one organization, so
there is no current-org selection and no org switcher.** §J.3 lists the specific edits. The two
that matter:

- `TenantMiddleware` resolves the org with
  `Membership.objects.select_related("role").get(user=request.user)` — `.get()`, not a session
  key and not a `.filter().first()`. `MultipleObjectsReturned` from that call is a violated
  database constraint, so it must surface as a 500 and an audit row, never be quietly resolved
  by picking one. `DoesNotExist` means no context, which is the anonymous path.
- The `/o/<org-slug>/…` alternative §E.4 rejects loses one of its two arguments (a multi-org
  user could address an org they were not currently in). The surviving argument — the slug is
  mutable and user-chosen and would leak the org name into referrers — is sufficient on its
  own, and the org still does not appear in URLs.

### I.7 The denial matrix and its canonical fixture

The matrix Elvis is working from encodes this ruling exactly, so it becomes the canonical
fixture for the generated matrix of §D.6. Every future endpoint is tested against it.

**Fixture — `tests/conftest.py::tenancy_matrix`, function-scoped:**

| | |
| --- | --- |
| Organizations | **A**, **B** |
| Stores | **A1**, **A2** in org A; **B1** in org B |
| Roles in A | `Nyiricyubahiro` — holds `store.access_all` (the *real* owner role, deliberately not named "Owner") · `Manager` — no `store.access_all` · `Owner` — a **decoy**, `permissions=[sale.record]` only |
| Roles in B | `Owner` — holds `store.access_all` |
| Rows | one row of every registered store-scoped model in each of A1, A2, B1 |

**Actors:**

| Actor | Membership | Role | `StoreAccess` |
| --- | --- | --- | --- |
| `a_owner` | org A | `Nyiricyubahiro` (has `store.access_all`) | **none** (§I.4) |
| `a1_manager` | org A | `Manager` | A1 only |
| `a_decoy` | org A | `Owner` (name only, no code) | A1 only |
| `b_owner` | org B | `Owner` (has `store.access_all`) | none |
| *anonymous* | — | — | — |

**The matrix:**

| Actor | Target | Expected | Which layer refuses |
| --- | --- | --- | --- |
| `a_owner` | A1 row | 200 | resolver: `access_all` branch includes A1 |
| `a_owner` | A2 row | **200** | resolver: `access_all` branch includes A2 — *this row is the ruling* |
| `a1_manager` | A1 row | 200 | resolver: live `StoreAccess` row |
| `a1_manager` | A2 row | 404 | `require_store` → `StoreNotPermitted` |
| `a1_manager` | write to A2 | 404 | the same gate, before the write is built |
| `a_decoy` | A1 row | 200 | resolver: live `StoreAccess` row |
| `a_decoy` | A2 row | **404** | resolver: the role is *named* "Owner" and holds no code |
| `b_owner` | A1 row | 404 | resolver (A1 is not in org B) **and** independently RLS |
| *anonymous* | any | 401 | authentication, before any resolver runs |

Elvis's matrix has four actors. **`a_decoy` is my one addition and it is not optional:** it is
the only row that proves the check is not name-based, which is the specific vulnerability the
constraint was issued against. Naming the real owner role `Nyiricyubahiro` costs nothing and
makes the same point from the other side — the name is arbitrary, and a matrix that names the
powerful role "Owner" would pass under a name-based implementation.

Deliberately **not** in the fixture: a Seller. Manager and Seller differ on the *permission*
axis, which `require_permission` owns and which has its own tests. Adding one here would
dilute a matrix whose subject is the store axis. Say so in the fixture's docstring, or someone
will add it as thoroughness.

**404, never 403 — and the two ways to get that wrong.**

1. `StoreNotPermitted` **must not** subclass `django.core.exceptions.PermissionDenied`.
   Django's default handler renders that as 403, and a 403 confirms the row exists, which turns
   the override's complement into an existence oracle across sibling stores. It is a plain
   `Exception` in the service layer — the service layer must not import HTTP concerns
   (ADR 0007 keeps a DRF path open), so the translation lives in exactly one place, a
   `process_exception` hook in `common/middleware.py`.
2. The 404 must be **byte-identical** to a 404 for a row that does not exist. Same template,
   same headers. For an HTMX fragment it is a genuine 404 and not an `HX-Redirect` — the row
   really is not there, so there is nothing to redirect to.

**Denials are audited, under the actor's own organization.** `b_owner` reaching for A1 is a
security event worth recording, and it is recorded under org **B**. Recording it under A would
be one tenant writing into another's audit trail — a cross-tenant write, which RLS refuses
anyway, so the code would fail loudly at the worst moment.

### I.8 What this does to `security-engineer`'s central finding

The threat model's conclusion is *"Inside an organization, RLS is blind and the
application-layer guards are the entire defence."* The owner override is an in-org
authorization decision, so it sits entirely inside that blind spot. Plainly:

> **If `permitted_stores()` has a bug, every store in that one organization becomes readable
> and writable by every member of it, and nothing below the Python layer will stop it.** RLS
> checks the organization and the organization is correct. The composite foreign key checks
> the organization and the organization is correct. The store predicate the query carries is
> the one the buggy function produced, and the query layer's job is to enforce that predicate,
> not to second-guess it. The blast radius is one organization, entirely — every store, reads
> and writes — and the only thing that can detect it is a test.

Three bounds on that sentence, so it is not read as worse or better than it is:

- **It is not cross-tenant.** A bug here cannot show org A's rows to org B. The org predicate
  comes from `tenant()` and from RLS, never from this function, and `permitted_stores()`
  refuses a membership whose org disagrees with the active context (§I.1).
- **It is one function, in one module, with one branch.** That is the whole argument for
  putting it there: the override cannot be partially implemented, and a diff touching it is
  visible in review. Compare the rejected alternative, where the answer is spread across
  `create_store`'s fan-out, `soft_delete_store`'s retraction and every reader of
  `orgs_storeaccess`.
- **It changes the severity of an existing finding.** `PRESETS["Manager"]` holds
  `member.manage` without `role.manage`, so a Manager can move a member — including themselves
  — into the owner role. That role now carries `store.access_all`, so the escalation's payoff
  rises from "reshape roles within my own store set" to "read and write every store in the
  organization". ADR 0011's rule 3 (exhaustive presets + `common.E009`) stops the *code* from
  reaching Manager by accident; it does **not** close this path. Routed to
  `security-engineer` as a separate ruling, and named here so nobody assumes it was handled.

**What I owe the threat model in return:** the sentence above, verbatim, belongs in its §7
beside the finding it extends, because §7 currently describes the store dimension as "a manager
seeing another branch of their own company — serious, a denial test, not a breach". With the
override in place the reachable set for a *single* resolver bug is the whole organization rather
than one sibling branch, so the severity of that row rises even though its classification
(intra-tenant) does not.

### I.9 The catalog change, and the trap in `PRESETS`

```python
STORE_ACCESS_ALL = "store.access_all"

PERMISSION_LABELS = {
    ...,
    STORE_ACCESS_ALL: _("Access every store in the organization"),
}
```

**Then stop, because adding it to the catalog grants it to Manager.** Today:

```python
PRESETS = {
    "Owner": PERMISSIONS,
    "Manager": PERMISSIONS - {ROLE_MANAGE, STORE_MANAGE},
    "Seller": frozenset({SALE_RECORD}),
}
```

Manager is defined **subtractively**, so `store.access_all` lands in it automatically and the
matrix row `a1_manager → A2 → 404` fails on the day the code is introduced. This is the same
shape as the recorded `PRESETS["Manager"]` self-promotion hazard: a subtractive definition
nobody re-reads when the catalog grows.

**Fix the cause, not the instance.** Every preset becomes an explicit `frozenset` with its
codes written out, and `common.E010` fails startup unless every code in `PERMISSIONS` appears
in at least one preset or in a declared `UNASSIGNED: frozenset[str]`. Adding a code then cannot
be committed green without someone deciding, per preset, in writing. `Owner` may stay
`PERMISSIONS` — Owner genuinely is everything, and the check confirms it rather than assuming
it. Numbering: the schema plan takes `common.E007` and `E008`, `E100` is taken, and §D.2's
surviving `org`-FK check takes `E009`, so this is `E010`.

`Role.has()` needs **no change** — it already tests catalog membership, so an unknown or
removed code is `False`, which is the correct failure direction for an override.

`store.access_all` is granted by editing a role, which requires `role.manage`. It appears in
the role editor as an ordinary checkbox, which is the point of choosing a code: it is
data-driven, revocable, and visible where every other permission is.

**And the axis rule, because it is worth more than the code itself:** `store.access_all` widens
**reach** and grants no **rights**. An owner reaching store A2 still needs `sale.record` to
record a sale there. Which stores (this resolver) and which actions (`Role.has`) are orthogonal,
which is why `require_store_permission()` exists as one call — a view that passes one gate and
not the other is a bug, and the combined helper makes that bug hard to write.

### I.10 How this is tested

- The generated matrix of §D.6 gains the fixture and rows of §I.7. It is **generated from the
  model registry**, so a model added in slice 2 acquires the owner-override rows without
  anyone remembering.
- **Mutation evidence is required, not optional**, to the standard §A.6 sets: delete the
  `access_all` branch and show `a_owner → A2` go from 200 to 404; invert it to a
  `role.name == "Owner"` check and show `a_decoy → A2` go from 404 to 200 and
  `a_owner → A2` from 200 to 404. That second pair is the evidence that the name-based
  implementation is a vulnerability, measured rather than asserted.
- Every "returns nothing" assertion is preceded by proving the row *is* returned under the
  correct actor, per this slice's most expensive lesson. `a_decoy → A2 → 404` is worthless
  without `a_decoy → A1 → 200` beside it.
- The revocation test soft-deletes a `StoreAccess` row **mid-request** and asserts the next
  gate call in the same request refuses. That is the test that would fail if anyone adds a
  memo, which is how §I.5's ruling stays enforced rather than remembered.
- Query counts from §I.3's table are asserted with `assertNumQueries`, both branches. They are
  the acceptance criterion for "the owner path adds no query", and they go red if
  `TenantMiddleware` loses its `select_related("role")`.

---

## J. Revision 2 — corrections to §A–§H

Revision 1 was written before three controller arbitrations, two Elvis rulings and the platform
bump landed. Every statement it now gets wrong is listed here with its replacement, because a
64 KB document with a stale paragraph in the middle is how a documented control that does not
exist gets believed. Where the correction is short, it has been applied in place and is
recorded here as well.

### J.1 §A.5 — the `for_store()` "leak" was overstated (controller arbitration 4)

Revision 1 said `for_store(<a rival's store id>)` "compiles today and returns the rival's
rows", framed as a hole `for_stores()` did not have. **That framing is wrong and was corrected
by execution.** `for_stores([rival_pk])` returns the rival's rows too. Neither primitive
authorizes a store against a caller, and neither is supposed to: `for_stores()` refuses a set
*spanning* two organizations and refuses *unknown* ids, and a single-store set from one rival
organization has nothing to be compared against. That is correct behaviour for a scoping
primitive. An implementer chasing a leak here would find nothing, which is the worst kind of
task.

**The genuine residue is a diagnostic asymmetry.** An unknown store id raises `ValueError`
from `for_stores()` and returns **silently empty** from `for_store()`. A query that looks
scoped and returns nothing is exactly how scoping bugs hide — which is `_store_pk`'s own
docstring's argument about `None`, applied one level up. Routing `for_store()` through
`resolve_scope()` makes both spellings raise on an unknown id. Keep the change; change its
justification. It buys a consistent diagnostic and, via §A.4's memo and instance fast path, a
cheaper pin — not a closed leak.

And the authorization question revision 1 had no answer for now has one: **§I owns it.**
`require_store()` turns a URL identifier into a `Store` the actor may reach, or a 404. Store
ids from request data never reach `for_store()`. The pin's job is to enforce a scope, not to
decide one.

### J.2 §E — PostgreSQL 18.6 is confirmed, so the fallback branch is dead; and `public_id` wins

Verified in this stack: Python 3.14.7, Django 6.1, PostgreSQL 18.0006,
`supports_uuid7_function = True`, `supports_virtual_generated_columns = True`, 369 tests green.

**§E.2 is withdrawn.** Its case for a Python default rested chiefly on decoupling the
identifier from an unverified PG18 bump (reason 1) and on `db_default`'s interaction with
`validate_unique()` (reason 2). Reason 1 no longer exists. Reason 2 is real but small: the field
is `editable=False` and appears in no `ModelForm`, so the wasted `WHERE public_id = UUIDV7()`
lookup is reachable only from a hand-written `full_clean()`, and on PG18 it is a wasted query
rather than an error. Against that, the database-engineer's schema plan measured the write cost
of `db_default=UUID7()` directly and established that `Field.db_returning` makes the value
available after `create()` on the same `INSERT ... RETURNING` — no extra round trip. Measured
evidence beats my reasoning.

**Adopt the schema plan's shape**, which is also the reason to name it once and stop:

| Revision 1 (§E, ADR 0010) | Revision 2 — settled |
| --- | --- |
| field `uuid` | field **`public_id`**, base **`PublicIdModel`** |
| `default=uuid.uuid7` (Python) | **`db_default=UUID7()`** |
| named `UniqueConstraint` in `Meta` | **`unique=True`**, with the matching E005 exemption |
| revision 1's `common.E007` = field shape, `E008` = constraint present | **both withdrawn** (§D.2). The schema plan's numbering stands: **`E007`** = org-leading index rule, **`E008`** = every first-party model has a `public_id`. Free ids go to **`E009`** (`org` FK on store-scoped models) and **`E010`** (`PRESETS` exhaustiveness, §I.9) |
| the org column is `organization` / `organization_id` | **`org` / `org_id`** (controller arbitration 2 — four already-pinned `RunSQL` statements contain `REFERENCES orgs_store (id, org_id)`) |
| `store_org_fk_v1(table)` | **`same_org_fk_v1(table)`** |
| §D.4's index check `common.E010` | **`common.E007`**, same rule |

**These renames were applied in place**, not left to a lookup table 1400 lines away — a rename
table is not a mechanism, and a spec that names the wrong field produces code that names the
wrong field. The table above is the record of what changed and why, so a reader holding
revision 1 can reconcile it. Two paragraphs that revision 1 wrote *against* the schema plan's
shape are kept under a withdrawal banner rather than deleted (§E.2, §G item 2): the arguments
were sound on the information available and it was evidence that reversed them, which is worth
more on the record than a clean-looking document.

**The one residual worth keeping from §E.2, as a rule with a test:** with `db_default`, an
**unsaved** instance's `public_id` is a `DatabaseDefault` sentinel, not a UUID. In a
template-rendered HTMX app that builds fragments for not-yet-saved objects, that would render
as an expression object into a DOM id. Rule: never put an unsaved object's `public_id` in a
template. `create()` populates it, so the reachable window is narrow and a single test on a
fragment rendered from an unsaved instance pins it.

**§F step 9 changes from a gate to a fact.** It said "step 9 gates nothing" and "if the devops
verification concludes against PG18, nothing in this design changes". The verification
concluded *for* PG18 and has already landed, so step 9 is done and `UUID7()` is available to
step 1. There is no fallback branch to carry.

**ADR 0010 needs a one-paragraph amendment** and I have not made it, because the `uuid` versus
`public_id` choice is a controller arbitration between two proposed documents rather than mine
to settle unilaterally. Exactly what changes: the field name to `public_id`, the base to
`PublicIdModel`, `UUIDField(default=uuid.uuid7, editable=False)` to
`UUIDField(db_default=UUID7(), editable=False, unique=True)`, the `UniqueConstraint`/no-index
paragraph to the `unique=True` paragraph, and `common.E007`/`E008` to the schema plan's
numbering. The ADR's *decision* — a time-ordered public identifier separate from the bigint pk,
the pk never leaving the process, no org in URLs — is unaffected.

### J.3 §C.3 and §B.5 — one organization per user

Elvis's ruling (a user belongs to exactly one organization), landing as
`orgs_membership_unique_live_user` — a live-conditioned unique constraint on `Membership.user`
alone, with the existing `orgs_membership_unique_live_user_per_org` retained for its distinct
error message — removes work from §C rather than adding it. The join table stays, so allowing
multi-org later is a `RemoveConstraint` rather than a data migration; that reversal path is the
database-engineer's and it is why nothing below turns the org into a field on `User`.

**§C.3, request path — replace the org-resolution rules with:**

- anonymous request → no context, unchanged.
- authenticated request → `Membership.objects.select_related("role").get(user=request.user)`.
  One query, cached on `request.tenant`, re-read every request so a revoked membership drops
  the context on the next one. `select_related("role")` is now **required**, not an
  optimisation: §I.3's query budget depends on it.
- `DoesNotExist` → no context (an authenticated user with no live membership; the registration
  and invite-acceptance flows are where that state is legitimate).
- `MultipleObjectsReturned` → a **violated database constraint**, therefore a bug. It must
  surface as a 500 with an audit row, never be resolved by picking one membership. A
  `.filter().first()` here would convert a constraint violation into a silent arbitrary choice
  of tenant, which is the worst available failure.
- **Delete:** "the org comes from `request.session`", "a user with exactly one membership needs
  no session key" (now the only case), and "switching org is a POST that writes the session".
  There is no current-org selection, no session key and no org switcher. Nothing in the session
  participates in tenant resolution.

**§B.5 — the stated reason `accounts_user` gets no RLS policy is now wrong.** Revision 1 said
"one user may hold memberships in several organizations". That is no longer true. The real and
surviving reason: **username, email and phone are unique installation-wide, and the
multi-identifier auth backend must resolve an identifier to a user *before* any organization is
known.** At that point there is no tenant key to write a policy on. The compensating controls
are unchanged and already built: the non-enumerating backend, per-identifier and per-IP
throttling, uniform error responses.

**§E.4 — the rejected `/o/<org-slug>/…` alternative** loses its second argument (a multi-org
user could address an org they were not currently in). The first — the slug is mutable,
user-chosen, and leaks the organization's name into referrers, proxy logs and shared links —
stands alone. The org still does not appear in URLs, and the conclusion is unchanged.

**One consequence outside this document, flagged for its owner:** the privacy ruling's §4 case
*"members who belong to another org keep their account and lose only this membership"* cannot
arise, which simplifies `erase_org`. That is `privacy-compliance`'s document, not mine.

### J.4 §C.5 and ADR 0009 — `ENABLE` without `FORCE` (controller arbitration 1)

Revision 1 ended §C.5 with *"Every table `ENABLE`d must also be `FORCE`d"*, and reasoned from
PostgreSQL semantics while flagging that it had not verified. **The security-engineer measured
the opposite and the controller arbitrated for the measurement:** `FORCE ROW LEVEL SECURITY`
combined with `BYPASSRLS` is self-cancelling, and removing `BYPASSRLS` from the migration role
makes data-migration backfills silently no-op — a much more expensive failure than the one
`FORCE` was meant to prevent. **Adopt `ENABLE` without `FORCE`.**

The acceptance test is unchanged and is what actually matters: not "the policy exists in
`pg_policies`", but "a read as `raporo_app` with the wrong context returns nothing while the
same read with the right context returns the row". The role split remains a hard prerequisite
(§G item 1 stands, and three agents arrived at it independently).

**ADR 0009 contains the same error** — *"every table is `ENABLE`d **and** `FORCE`d"* in its
Decision section — and I have not edited it here, because my file scope for this revision is
this document and ADR 0011. The replacement clause is: *"every table is `ENABLE`d; `FORCE` is
deliberately not used, because `FORCE` plus `BYPASSRLS` is self-cancelling and dropping
`BYPASSRLS` from the migration role makes backfills silently no-op."* One clause, one file.

### J.5 §D.6 — the matrix now has a canonical fixture

§D.6's design (generated from the registry, `TENANCY_FACTORIES` premise test, allowed cases
listed alongside refusals, every emptiness assertion preceded by a positive one) is unchanged
and needed. What it lacked was a fixture. **§I.7 is that fixture and it is canonical** — two
organizations, three stores, five actors including the decoy — and every future endpoint is
tested against it. Add to §D.6's shapes list: the owner-override rows, and the two mutations of
§I.10.

### J.6 What revision 2 does **not** change

Stated so the diff is not read as wider than it is. §A.1–A.4 and A.6–A.10 (the `ScopePin`
value object, the resolver, the merge algebra, the write-path fills, `_derive_org`, the
not-removed list); §B.1–B.4 and B.6 (the RLS/application split, the argument against
store-level RLS — which §I.3 now reinforces from the pin's side, the composite FK, the
`get_compiler` cross-check, the security-engineer handoffs); §C.1, C.2, C.4, C.6, C.7
(`tenant()` as the single door, `SET LOCAL` and its source-scan test, fail-closed in two
layers, the registration carve-out, the GUC namespace rules); §D.1–D.5; §E.1 and E.3–E.6
modulo the renames of J.2; §F's ordering; §G's two push-backs, of which item 1 (the role split
is a prerequisite) has since been confirmed by three independent arrivals.

### J.7 Self-review of revision 2

- **Placeholders:** none. `permitted_stores`, `StoreSet`, `require_store`,
  `require_store_permission`, `set_membership_role`, `STORE_ACCESS_ALL`, `common.E010`,
  `StoreNotPermitted`, `NoPermittedStores` and `tests/conftest.py::tenancy_matrix` are each
  specified with their module, name and signature.
- **Contradictions hunted deliberately**, since the point of §J is that revision 1 acquired
  some: the `StoreAccess` docstring (§I.0, §I.4), the `for_store()` framing (§J.1), the `uuid`
  versus `public_id` names (§J.2), multi-org in §C.3 and §B.5 (§J.3), `FORCE` in §C.5 and
  ADR 0009 (§J.4). §I.5's "no cache" and §A.4's `store_org_cache` are not a contradiction and
  §I.5 says why in its last paragraph: the cache holds the one immutable fact, and liveness and
  reachability are never read from it.
- **One addition beyond what was asked, flagged as such:** `a_decoy` in the canonical fixture
  (§I.7). Elvis specified four actors; the fifth is the only row that proves the check is not
  name-based, which is the vulnerability the constraint was issued against.
- **One thing I am changing that nobody asked me to, flagged for the tech-lead:** `PRESETS`
  must stop being defined subtractively (§I.9). This is not tidiness — with the catalog
  addition and `PRESETS` as written, `a1_manager → A2 → 404` fails on day one, so the
  exhaustive presets and `common.E010` are part of this change's minimum, not a follow-up.
- **Left to other roles, with the constraint stated:** the `Membership.user` constraint and its
  interaction with the now-implied per-org one (database-engineer); the raised severity of the
  `member.manage`-without-`role.manage` escalation path and the §7 amendment of the threat model
  (security-engineer); the ADR 0009 `FORCE` clause and the ADR 0010 naming amendment
  (controller arbitration); the `erase_org` simplification (privacy-compliance).
- **Not verified by me, and it should be before this ships:** that `Role.has()` returns `False`
  for `store.access_all` when the code is absent from `PERMISSIONS` — i.e. that the catalog
  gate in `has()` fails in the safe direction for an *override* code and not just for a
  *grant* code. It reads correct (`code in PERMISSIONS and code in set(self.permissions)`), but
  per this slice's own standard, reading is not evidence.
