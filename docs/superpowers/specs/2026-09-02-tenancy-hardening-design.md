# Tenancy hardening — design

Date: 2026-09-02 · Author: architect · Status: proposed, awaiting Elvis
Branch: `feat/slice-1-foundation` · Companion ADRs: 0008 (org column), 0009 (RLS), 0010 (public identifiers)

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

Elvis's five settled decisions are the input, not the subject: denormalised `organization`
on `StoreScopedModel` with a composite FK; RLS in slice 1; UUIDv7 public identifiers;
tenant-leading indexes + connection config + per-tenant export/delete + a generated denial
matrix; Python 3.14 + PostgreSQL 18. §G records the two places where I would push back and
what would change my mind.

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

`for_store()` now goes through the resolver. That closes a hole the current code cannot
see: **`_store_pk` never checks that the store exists or who owns it**, so
`Model.objects.for_store(<a rival's store id>)` compiles today and returns the rival's
rows. It is correct-by-design in the sense that authorization is the caller's job — but
slice 1 has no service layer yet, `store_id` is exactly the kind of value that arrives from
a URL, and it is the classic IDOR shape. After this change a store id from another
organization is refused at the pin, by the same `CrossStoreReferenceError` and the same
message `for_stores([A, RIVAL])` already produces, and independently by RLS (§B).

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
        queryset = queryset.filter(organization_id=pin.org_pk, store_id__in=pin.store_pks)
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
database-engineer's call against the index plan; either is safe. The `organization_id = %s`
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
| `_store_for_write` | also injects `organization_id` from the pin on a single-row create; refuses an explicit `organization`/`organization_id` that disagrees with the pin |
| `_given_store_pk` | gains a sibling `_given_org_pk`, same shape |
| `_check_write_store` | also refuses an out-of-scope `organization`/`organization_id` |
| `bulk_create` | fills `organization_id` per row from the pin (the pin's org is single-valued even when its store set is not, so this always succeeds where the store fill succeeds) |
| `_refuse_store_reparenting` | also refuses `organization`/`organization_id` in `update()`, for a strictly stronger reason than store re-parenting: it would break the composite FK, and re-homing an organization is not an operation this product has |
| `_check_update_fk_stores` | unchanged, including the `resolve_expression` refusal and the multi-store refusal |
| `StoreScopedManager.raw` | unchanged, still refused |

### A.9 `StoreScopedModel` — derivation, then assertion

The row's `organization_id` is **derived, never asked for**, in the pattern
`StoreAccess._derive_org` established and two gates blessed. New method
`_derive_organization()`, called from `save()` before `_assert_related_stores_match`, and
from `ScopedQuerySet.bulk_create` for each object — as `_assert_related_stores_match`
already is, because `bulk_create` never calls `save()`.

Source precedence, first hit wins:

1. the pin, when the row was created through a pinned queryset — free, and guaranteed
   consistent with `store` because `resolve_scope` derived both from one read;
2. a cached `store` instance's `org_id` — free;
3. an explicit `organization_id` the caller passed;
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
USING (organization_id = raporo_current_org_id()
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
<business table> (organization_id, store_id)  ->  orgs_store (id, org_id)
```

`orgs_store` already carries
`UniqueConstraint(fields=["id", "org"], name="orgs_store_id_org_uniq")` as a composite-FK
target — added in `orgs/0001_initial` for exactly this pattern, with three such keys already
live. Requirements I need from database-engineer:

- `DEFERRABLE INITIALLY IMMEDIATE`, matching the four existing `*_same_org_fk` keys, for the
  reason the ledger records: `INITIALLY DEFERRED` violations surface only at COMMIT, which
  never happens inside a test transaction, so tests pass vacuously.
- Emitted by a **versioned, hash-pinned helper** in `common/db.py`
  (`store_org_fk_v1(table) -> (forward, reverse)`), mirroring `append_only_triggers_v1`,
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
| every future `StoreScopedModel` table | `organization_id` | yes, from its own initial migration |
| `audit_auditlog` | `org_id`, **nullable** | yes — policy text is security-engineer's (§B.6) |
| `accounts_user`, `accounts_twofactor`, `accounts_recoverycode` | none | **no** |

`accounts_user` is a global namespace by product design: username, email and phone are
unique across the installation, and one user may hold memberships in several organizations.
There is nothing to key a policy on. What compensates is already built and gate-verified:
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
- authenticated request → the org comes from `request.session` but is **re-validated against
  a live `Membership` on every request**, never trusted from the session. A session outlives
  a revoked membership; that is the whole reason the check is per-request rather than at
  login. One query, cached on `request.tenant`.
- a user with exactly one membership needs no session key.
- **the org never changes within a request.** Switching org is a POST that writes the
  session; the next request picks it up. No code path re-issues the GUC.

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

Every table `ENABLE`d must also be `FORCE`d, and the acceptance test is not "the policy
exists in `pg_policies`" but "a read as `raporo_app` with the wrong context returns nothing
while the same read with the right context returns the row".

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
TENANT_COLUMNS   = {"store", "store_id", "organization", "organization_id"}
IDENTITY_COLUMNS = {"uuid"}
```

| Kind | Test | Requirements | On failure |
| --- | --- | --- | --- |
| 1 — tenant-rooted business key | `referenced & TENANT_COLUMNS` and not `referenced & IDENTITY_COLUMNS` | `_requires_live_rows(condition)` is true | E005, current message and hint |
| 2 — public identifier | `referenced == IDENTITY_COLUMNS` | `condition is None`; the field is `editable=False` | E005, new message |
| 3 — anything else | otherwise | — | E005: "does not include `store` or `organization`" |

Kind 2 **inverts** both of E005's current demands, which is exactly why it must be a named
kind and not an `if` buried in the old code path:

- it must be **global**: a public identifier that is unique only per tenant is not an
  identifier. Adding `store` to it would let two rows in two stores share a URL.
- it must be **unconditional**: a soft-deleted row keeps its identifier for ever. Conditioned
  on live rows, a tombstone would release its uuid, a later insert could take it, and a
  stale URL or an audit reference would resolve to a different row. Reissuing an identifier
  is worse than reserving one.

Unchanged: field-level `unique=True` is still an error — including on `uuid`, because the
identity constraint must be a named `UniqueConstraint` in `Meta.constraints` so that E008
can find it, so that it can carry a `violation_error_message`, and so that the rule inspects
one uniform structure. `unique_together` is still an error. `_requires_live_rows`'s AND-level
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

| id | Subject | Rule |
| --- | --- | --- |
| `common.E007` | the `uuid` field on any concrete `IdentifiedModel` subclass | `UUIDField`, `null=False`, `editable=False`, has a callable default, `db_index` **not** set |
| `common.E008` | the identity constraint | exactly one kind-2 `UniqueConstraint` on the concrete model. Catches the `ScopedThingOwnMeta` accident: a subclass declaring its own `Meta` without inheriting the base's loses the base's constraints, the same trap `common.E002` exists for |
| `common.E009` | `organization` on a store-scoped model | non-nullable FK to `orgs.Organization` with `related_name="+"` — mirroring `_check_store_field`/E003 exactly, including its tolerance for a string target in isolated registries |

All three are startup errors, not conventions, and all three need the coverage standard the
E100 incident set: driven through `django.core.checks.run_checks()`, never by calling the
function directly, and each with a deliberately-broken model under `isolate_apps` so the
test fails when the check is removed.

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

### D.4 Tenant-leading indexes, and `common.E010`

Rule: **every `Index` on a store-scoped model leads with `organization` or `store`.** After
§A.5 every scoped query emits `organization_id = %s AND store_id IN (...)`, so a
tenant-leading composite is the shape the planner wants and a non-leading index is a promise
the query shape cannot keep.

`common.E010` enforces it as a startup error with no escape hatch. The one case that looks
like a counter-example — the architecture spec's partial index on
`expiry_date WHERE expiry_date IS NOT NULL` — is not one: the real query is "what expires
soon *in this store*", so `Index(fields=["store", "expiry_date"], condition=...)` is both
compliant and better. If a genuinely global index is ever needed on a store-scoped table,
that is an argument to have in review, not a flag to set in `Meta`.

Naming convention for database-engineer: `<table>_org_store_<col>_idx`. Unique constraints
are not `Index` objects, so E010 does not see them and the global `uuid` unique index is
untouched — the correct outcome, and worth a comment because it will look like an
inconsistency.

### D.5 Per-tenant export and delete

**Export — buildable now.** `apps/orgs/management/commands/export_org.py`, a
`TenantCommand` subclass, `--org <uuid>`. Iterates a declared table order and writes NDJSON
per model with `.iterator()`; includes soft-deleted rows, which are part of the record, and
the organization's audit rows. Two content rules make the output re-importable and id-free:
emit `uuid`, never the bigint `id`; represent every FK by the target's `uuid`. It runs
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
  materialised (`sorted(r.uuid for r in qs)`), and every "returns nothing" assertion is
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
class IdentifiedModel(models.Model):
    uuid = models.UUIDField(_("public id"), default=uuid7, editable=False)

    class Meta:
        abstract = True
        constraints = [UniqueConstraint(fields=["uuid"],
                                        name="%(app_label)s_%(class)s_uuid_uniq")]
```

Mixed in at three points, which covers everything:

- `SoftDeleteModel(IdentifiedModel)` → `Organization`, `Store`, `Role`, `Membership`,
  `StoreAccess`, and every `StoreScopedModel` by inheritance;
- `AuditLog(IdentifiedModel, models.Model)` → explicitly, because a future audit screen
  links to rows by URL and `AuditLog` is not soft-deletable;
- `accounts.User` → explicitly, because it descends from `AbstractBaseUser`, and because
  member-management URLs (Task 10) need a user identifier that is not the username.

The `%(app_label)s_%(class)s` placeholders are what make a constraint declarable on an
abstract base at all. `common.E008` then asserts the constraint is present on the
**concrete** model, which catches the `ScopedThingOwnMeta` accident: a subclass declaring its
own `Meta` without inheriting the base's silently loses it, exactly as it silently loses the
default manager, which is what `common.E002` already exists for.

### E.2 `db_default=UUID7()` or a Python default? Python default.

`default=uuid.uuid7` (stdlib, Python 3.14 — verified in this repo's interpreter: `3.14.6`,
`uuid.uuid7()` returns `01a061ef-…-7177-…`). Five reasons, heaviest first:

1. **It decouples the identity scheme from the platform bump.** `UUID7()` compiles to
   `uuidv7()`, and Django raises
   `NotSupportedError("UUID7 requires PostgreSQL version 18 or later.")` below PG18. A
   devops agent is still verifying PG18 feasibility. With a Python default, §F step 1 lands
   whatever that verification concludes.
2. **`db_default` and `full_clean()` interact badly, and measurably.** A field with
   `db_default` has `db_returning = True`, and `Field.get_default()` returns a
   `DatabaseDefault` sentinel. `Model.clean_fields()` skips it (`django/db/models/base.py`,
   the `isinstance(raw_value, DatabaseDefault)` branch), but `Model.validate_unique()` and
   `UniqueConstraint.validate()` do **not** — both read `getattr(instance, attname)` and
   build a lookup from it, so the pre-insert uniqueness check compiles
   `WHERE uuid = UUIDV7()`: a wasted query with a nonsense predicate on PG18, and a
   `NotSupportedError` on anything older.
3. **An unsaved instance has a real identifier.** With `db_default`, `obj.uuid` on an
   unsaved object is a `DatabaseDefault` expression object. In a template-rendered app that
   builds fragments for not-yet-saved objects and puts the uuid in DOM ids and `hx-*`
   attributes, that is a papercut waiting in the one place nobody tests.
4. It works on any backend, which keeps a future fast local test path open.
5. The gap it leaves is small and already governed: the only non-Django writers this project
   permits are `RunSQL` migrations, and the stability contract already forces every one of
   those through a hash pin and a review.

`default=` rather than `db_default=` also means the column carries **no** database default,
so a raw `INSERT` that omits `uuid` fails on `NOT NULL` — loud, which is the right failure.

Revisit trigger for the ADR: PG18 confirmed everywhere **and** a bulk `COPY` loader or an
external writer appears. Then add `db_default=UUID7()` *alongside* the Python default —
`Field._get_default` checks `has_default()` first, so the Python default keeps winning on the
ORM path and the database default covers only the paths that bypass it. Additive migration,
not a redesign.

### E.3 Indexed separately? No.

`db_index` is not set. On PostgreSQL a `UniqueConstraint` is backed by a unique B-tree index
on `uuid`, which serves every lookup this identifier has (`get(uuid=...)`). A second index
would be redundant and would cost a write on every insert. `common.E007` enforces its
absence, so nobody adds it "for lookups".

This looks inconsistent with the deliberately redundant plain index on `accounts_user`, so
the reason is worth recording: that one exists because an **expression-only**
`UniqueConstraint` raises a `NON_FIELD_ERRORS` `ValidationError`, so the field-level unique
was what produced per-field form errors. The uuid constraint is field-based, and the field is
`editable=False` and never appears in a form. The exception does not generalise.

### E.4 What it means for URL design in an HTMX app

- **The bigint pk never leaves the process.** Not in a URL, not in HTML, not in an `HX-*`
  header, not in a DOM id. The uuid is the only identifier that crosses the boundary:
  `id="sale-{{ sale.uuid }}"`, `hx-target="#sale-{{ sale.uuid }}"`,
  `{% url 'sales:detail' sale.uuid %}`.
- **Routes use Django's built-in converter**: `<uuid:sale_uuid>`. It accepts only the
  canonical hyphenated lowercase form, so a malformed identifier is a 404 from the resolver
  and never reaches a view.
- **No organization in the URL.** The active org comes from the tenant context, so paths are
  `/stores/<store_uuid>/sales/<sale_uuid>/` and there is no org-shaped enumeration surface at
  all. Rejected alternative: `/o/<org-slug>/…` — readable, but it puts a mutable, user-chosen
  value in every URL and gives multi-org users a way to address an org they are not currently
  in, which then needs its own authorization check on every route.
- **`uuid` is not an authorization control.** Three layers, three jobs: the uuid removes
  *enumeration*, the pin removes *authorization risk*, RLS removes both when the app forgets.
  Concretely, views never call `.get(uuid=…)`; they call one selector,
  `common/selectors.py::get_scoped(model, uuid, *, store)`, which is
  `model.objects.for_store(store).get(uuid=…)` — so a valid uuid belonging to another tenant
  raises `DoesNotExist` and renders a 404, with no oracle in the difference between "wrong id"
  and "not yours".
- **HTMX-specific:** a lost tenant context on a fragment request must respond with
  `HX-Redirect`, not an HTML redirect — an HTML redirect gets swapped into the fragment target
  and the user sees a login page inside a table cell. Every fragment endpoint also answers a
  full-page GET, per the architecture spec's progressive-fallback rule, so both shapes need
  the same selector.
- **Enforcement:** "no pk in the DOM" is a Task-8 acceptance criterion owned by
  frontend-engineer and code-reviewer, per the tech-lead's rule that a control in a handoff
  note is how E100 shipped inert. Each screen lands with an integration test asserting its
  rendered fragment contains the uuid and not the pk.
- **`audit.record`** gains `target_uuid` alongside `target_type`/`target_id`, so an audit
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
identifiers per row and one rule to hold: **the pk never leaves the process; the uuid never
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
| 1 | **Public identifier.** `IdentifiedModel`; `uuid` on `SoftDeleteModel`, `AuditLog`, `User`; `common.E007`/`E008`; **the E005 rewrite (§D.1)** | `common/models.py`, `common/checks.py`, `accounts/0002`, `orgs/0002`, `audit/0003`, `testapp/0002` | `manage.py check` green; a uuid on every row; E005 accepts a kind-2 and an org-rooted kind-1 constraint and still refuses a bare one | drop 4 columns |
| 2 | **`organization` on `StoreScopedModel`** + `store_org_fk_v1()` in `common/db.py` + `_derive_organization` + the write-path fills | `common/models.py`, `common/managers.py`, `common/db.py`, `testapp/0003` | `pg_constraint` shows the key; a cross-org write is refused by the database; a store's org cannot be updated while it has rows | drop 1 column + 1 constraint |
| 3 | **The pin becomes `(org, stores)`** (§A) | `common/managers.py` only | the 45-case matrix before/after with mutation output; the query-count table of §A.5 | pure Python revert |
| 4 | **Tenant context** (§C) — `common/tenancy.py`, `TenantMiddleware`, `TenantCommand`, connection settings | `common/tenancy.py`, `common/middleware.py`, `config/settings/*` | the GUC is set inside and absent outside the transaction; the two-requests-one-connection test; the `SET ` source scan | remove middleware |
| 5 | **RLS** — roles, `ENABLE` + `FORCE`, `raporo_current_org_id()`, policies, the `app` alias | `common/db.py`, `orgs/0003`, `audit/0004`, `config/settings/*` | a read as `raporo_app` with the wrong context returns nothing while the right context returns the row | `DISABLE ROW LEVEL SECURITY` |
| 6 | **Tenant-leading indexes + `common.E010`** | `common/checks.py`, migrations | `EXPLAIN` on a pinned read uses the leading index | drop indexes |
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
- **Step 9 gates nothing.** Nothing in steps 1–8 needs PostgreSQL 18: RLS is ancient, the
  composite FK is ordinary SQL, and §E.2 chose a Python default precisely so the identifier
  does not depend on `uuidv7()`. `nulls_distinct=False` on a nullable business key (PG15+) is
  the only other version-sensitive item and it has no consumer yet. If the devops verification
  concludes against PG18, **nothing in this design changes.**
- **Slice 2's precondition, not slice 1's:** `store_org_fk_v1()` and the RLS install helper
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

2. **Decision 3 (UUIDv7), one detail:** not `db_default=UUID7()` in this round, for the five
   reasons in §E.2 — chiefly that it couples the identifier to an unverified PG18 bump, and
   that `DatabaseDefault` misbehaves in `validate_unique()` / `UniqueConstraint.validate()`.
   A Python default gets the same identifier with none of the coupling, and `db_default` can be
   added later as a purely additive backstop.

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

## H. Self-review

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
