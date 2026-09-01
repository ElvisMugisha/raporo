# Fix round 2 — merged Tasks 1+2+3

Three scoped re-reviews came back. **code-reviewer: spec ✅ / APPROVE WITH NITS** (all 9 items addressed). **database-engineer: APPROVE WITH NITS** (all 3 findings addressed, verified by reading `pg_constraint` directly). **security-engineer: BLOCK** — 10 of 12 findings fully closed, but two are still reachable by a different keystroke, and both fire on the first store-scoped model slice 2 adds.

Credit where due: F1/F2/F4/F5/F6/F7/F8/F9/F11/F12 are all confirmed closed against a fresh ~110-attack harness, including raw-SQL inserts, a non-owner DB role, and path-traversal filenames. The `GuardedQuery` `+` work was verified more complete than you claimed. This round is two real holes plus small cleanups.

---

## A. BLOCKING — the two holes (both in `common/managers.py` + `common/models.py`, ~15 lines each)

**A1 · High — `update()`, `bulk_update()` and `save(update_fields=["store"])` bypass the same-store FK invariant.**
You enforced same-store FKs in `save()` and `create()`/`bulk_create()`, and those are genuinely closed. But the update paths never re-validate. Proven:
- `SaleLine.objects.for_store(A).filter(pk=x).update(product_id=<a store-B product>)` → **1 row updated**, and `SaleLine.all_objects.get(pk=x).product.name` then reads `'RIVAL'`.
- `for_stores([A, A2]).update(store_id=A2)` moves a child while its parent stays in A — an orphaned cross-store parent/child pair.
- `save(update_fields=["store"])` validates nothing: the `wanted` narrowing skips `store` by name, so no FK is re-checked.
- `bulk_update()` inherits the same hole.

**Fix:**
- In `ScopedQuerySet.update()`: for every kwarg naming a store-scoped FK, resolve the target's `store_id` and raise `CrossStoreReferenceError` unless it matches the pinned scope.
- Refuse `store`/`store_id` in `update()` outright — re-parenting a row is a service operation that must also move its children, not a bulk column write.
- In `_assert_related_stores_match()`: when `store` or `store_id` is present in `update_fields`, validate **all** store-scoped FKs, not only the named ones.
- Regression tests: each of the three probes above must now raise.

**A2 · Medium — set operators and combinators are not closed; `union()` was never safe.**
Your fix blocked the exact reproduction (`for_store(A) | all_objects.all()`), but three equivalent rewrites still leak:
- `Product.all_objects.all() | Product.objects.for_store(A)` → `['RIVAL','mine','mine2']`. Operand order decides it: `type(left).__or__` wins and the combined query carries no `store_scoped` attribute at all.
- `Product.objects.for_store(A).union(Product.all_objects.all())` → `['RIVAL','mine','mine2']`. Your report's claim that "`union()` was already safe" is **false** — only the `objects.all()` leg was caught.
- `for_store(A) | for_store(B)` and `for_store(A).union(for_store(B))` → rows from two **organizations** in one query, which `for_stores([A,B])` correctly refuses.

**Fix:**
- Add `__ror__`/`__rand__` (or make `NoHardDeleteQuerySet` refuse to combine with a pinned queryset) so operand order cannot flip the guard.
- Override `union`/`intersection`/`difference` to require every leg pinned, and pin the result.
- Route the merged pin set through the same one-org resolution `_store_pks()` already uses, so `|` and `union` cannot span organizations either.
- Regression tests: all four leaking forms above must raise, and the same-org forms must still work.

## B. Cleanups — all reviewer-raised, all small (do them in the same round)

**B1 · Enforce the `common/db.py` stability contract (database-engineer: must-fix before slice 2 becomes the second caller).** `apps/audit/migrations/0002` *imports* the live helper, so a future edit to `common/db.py` silently changes what an already-shipped migration applies on fresh installs, while already-migrated databases keep the old function body — Django tracks migrations by name, not content. The docstring "contract" is a comment, not a mechanism. Pick one and implement it: (a) migrations embed the literal returned SQL at authoring time, (b) versioned constants (`APPEND_ONLY_FUNCTION_V1`, never edited in place; a `V2` added alongside for any change), or (c) a test pinning a hash of the literal SQL text so any edit is a loud, reviewed break. State which you chose and why.

**B2 · Fail fast on a mis-named production database.** The TRUNCATE exemption for `test_*` databases fails open silently. Security measured that `SET raporo.allow_truncate='on'; TRUNCATE audit_auditlog;` **succeeds in a production-named database** — custom GUCs need no privilege. (That is acceptable against the intended attacker, since anyone who can reach it could equally `DROP TRIGGER`, but the naming boundary should not be silent.) Add a startup assertion in `config/settings/prod.py` (or a system check that runs under prod settings) refusing a database whose name starts with `test_`.

**B3 · Harden the image validator's parsing surface** (`common/validators.py`, both Low but this is an untrusted-input path): add a 4-byte magic pre-sniff for PNG/JPEG/WebP before calling `Image.open()`, so attacker bytes don't reach every registered Pillow header parser (where the GD/FITS bomb CVEs lived); and add `Image.DecompressionBombError` to the caught tuple, so a crafted oversize header raises `ValidationError` rather than a 500.

**B4 · Fix the stale docstring** in `apps/orgs/migrations/0001_initial.py` — its reversal caveat describes a scenario that no longer exists now that the constraint landed inside `0001` rather than a follow-up migration.

**B5 · Make the `app_configs` test earn its name** (`test_the_check_honours_the_app_configs_it_is_given`). It asserts only `== []`, which would also pass if the filter were dropped entirely, since every real model currently passes every check. Set up a deliberately-failing model in an *excluded* app so the test can distinguish "filtered correctly" from "filter removed". D9 was also never in the mutation list — add it.

**B6 · Add a `loaddata`/fixture regression test against the four composite FKs.** Django's Postgres backend issues `SET CONSTRAINTS ALL DEFERRED` around fixture loading, so out-of-order fixtures should still work under `DEFERRABLE INITIALLY IMMEDIATE` — prove it before slice 2's ledger tables copy the pattern.

## C. Explicitly NOT yours — routed elsewhere, do not implement

- Runtime DB role must not own these tables and must not hold `TRUNCATE` (today `compose.yaml` connects as `raporo`, which is superuser **and** owner, so a compromised app process could wipe the audit trail in two statements) → **devops-engineer**, at deploy.
- "No production or staging database may be named `test_*`" as a written operational invariant → **devops-engineer** (B2 above is only the code-side assertion).
- `DJANGO_MEDIA_ROOT=/var/lib/raporo/media` in `.env.example` → **Elvis by hand**; that path is inside your denied settings glob, so you cannot edit it. Do not retry.
- A DB `CheckConstraint` making the username-shape rule structural (validators only run in `full_clean()`, so `User(username="x@y.com").save()` still writes) → noted as residual; `create_user()`/forms remain the sanctioned path. Not this round.

## Evidence required before I re-review

Re-run and paste verbatim: full `pytest -v`, `ruff check .`, `manage.py check`, and `makemigrations --check --dry-run` under both settings. Every probe named in A1 and A2 needs a regression test that fails without your fix — the security reviewer will re-run its own harness against your result, so a test that passes for the wrong reason will be caught.
