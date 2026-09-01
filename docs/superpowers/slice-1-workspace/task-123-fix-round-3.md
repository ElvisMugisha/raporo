# Fix round 3 — merged Tasks 1+2+3

Four gates reported. **code-reviewer: REQUEST CHANGES.** **security-engineer: BLOCK.** **database-engineer: APPROVE WITH NITS** (and it overturned one of its own earlier verdicts). **tech-writer: docs reconciled**, with residual conflicts listed below.

Credit first, because most of this round is small. S1 is fully closed — every probe from the previous BLOCK now raises, including four the security gate had not previously tried, and the happy paths still work. B1 was called "the right mechanism, implemented better than asked" by two gates independently; its `REMEDY` message was singled out as the best failure message in the repo. The parked `app_configs` item is properly closed and verified mutation-sensitive. B6 corrected the false premise it was commissioned on instead of quietly satisfying it. The devops healthcheck catch (`-h 127.0.0.1`) and installing the entrypoint outside the bind mount were both confirmed correct and non-obvious.

Two things blocking, both with the same root cause: **a control was assumed to work because it was present, rather than because it was executed.**

---

## A. BLOCKING — security-engineer

**A1 · High — S2 is NOT closed. Two of the four forms the last round required still leak, plus a fifth nobody enumerated.**

What *is* fixed, and must stay fixed: operand order. `all_objects.all() | for_store(A)` and its reverse, `union` in both orders, and the `&`/`intersection`/`difference` equivalents all raise `UnscopedQueryError`. `refuse_scope_mix` on the unscoped side is the right place for it.

Still leaking — `RIVAL` below is a product in a **different organization**:

```
Product.objects.for_store(A) | Product.objects.for_store(B)          -> ['RIVAL', 'mine']
Product.objects.for_store(A).union(Product.objects.for_store(B))     -> ['RIVAL', 'mine']
Product.objects.for_store(A) ^ Product.all_objects.all()             -> ['RIVAL', 'mine2']
Product.all_objects.all() ^ Product.objects.for_store(A)             -> ['RIVAL', 'mine2']
```

- **Cross-org `|` / `union()` / `intersection()` / `difference()`.** This was bullet 3 of the original S2 finding and bullet 3 of the round-2 fix ("route the merged pin set through the same one-org resolution `_store_pks()` already uses"). It was not done. `ScopedQuerySet.__or__` (`common/managers.py:323-332`) merges `_scope_pks()` with a plain set union and never re-resolves ownership; the combinator overrides (`:258-268`) only call `refuse_scope_mix`. `for_stores([A, B])` correctly refuses this exact set — `|` and `union()` are its unguarded synonym. That is the IDOR shape: a store id taken off a request, combined with the caller's own.
- **`^` is unguarded in both directions.** Django 6.1 has `QuerySet.__xor__` / `__rxor__` and neither `NoHardDeleteQuerySet` nor `ScopedQuerySet` overrides them, so the guard flag rides along on a symmetric difference that returns everything *outside* the pinned store, across organizations.

**Fix:**
1. Route merged pins through the one-org resolver in `__or__`, `__and__` and every combinator: `merged = _store_pks(set(self._scope_pks()) | set(other._scope_pks()))`, then pin the result. `_store_pks()` already refuses a mixed-org set and unknown ids — reuse it, do not reimplement it.
2. Add `__xor__` / `__rxor__` to `NoHardDeleteQuerySet` (`refuse_scope_mix`) and to `ScopedQuerySet` (both-scoped requirement plus the merged resolution).
3. **Stop enumerating dunders.** `|`, `&` and `^` all funnel through `sql.Query.combine`. Override `combine()` on `ScopedQuery` to re-resolve the merged pin set and refuse an unscoped right-hand query, so a combinator Django adds later is covered by construction. Keep the queryset-level refusal for the unscoped-left case, which `combine()` on `GuardedQuery` cannot see. This is the same lesson the `get_compiler` guard already taught this codebase — guard the seam, not the names.
4. **Pin it with a parametrized matrix** over the full operator surface — `["__or__","__and__","__xor__","__ror__","__rand__","__rxor__","union","intersection","difference"]` × {unscoped leg, cross-org leg, same-org leg}. The forms that work today are entirely unpinned and can regress silently.

**Why this survived a round:** the round-2 report states A2 was inherited from the previous agent and "verified present, not touched". Present is not verified. The controller repeated the same error at session start, confirming A2 by grepping for method names. Neither check executed anything.

**A2 · Medium (new, same code path) — a multi-store pin still writes a cross-store FK.**
`_check_update_fk_stores` tests membership in the *pinned set*, not equality with the *row's own store*:

```python
SaleLine.objects.for_stores([A, A2]).filter(pk=x).update(product_id=<an A2 product>)
# -> 1 row updated; SaleLine(store=A).product now lives in store A2
```

Same organization, so not a tenant breach — but it is exactly the same-store FK invariant `save()` enforces, broken on the update path. A store-A-only user reads `line.product.name` and sees store A2's catalogue; per-store reports for A count A2's product. The previous fix brief said "unless it matches the pinned scope", which is what got built — the brief was loose, not the implementation.

**Fix:** a multi-store pin cannot know each row's store, so refuse rather than approximate — if the pin is not exactly one store and any store-scoped FK appears in the update kwargs, raise `CrossStoreReferenceError`; keep the existing equality check for the single-store case.

## B. BLOCKING — both code-reviewer and security-engineer, independently

**B1 · `common.E100` never runs. The pre-boot guard is inert, and four places assert it works.**

`common/checks.py:292` registers it `@register(Tags.database)`. Django 6.1's `CheckRegistry.run_checks` drops every database-tagged check when no `--database` alias is given ("they do more than mere static code analysis"), and `manage.py check` passes `databases=None`. Reproduced by both gates and independently by the controller, under prod settings with `POSTGRES_DB=test_raporo`:

```
python manage.py check                    -> System check identified no issues (0 silenced).
python manage.py check --database default -> (common.E100) Database 'default' is named 'test_raporo'...
```

A mis-named production database boots and serves, inheriting the append-only TRUNCATE waiver — precisely what the control exists to prevent.

**Fix:** the check does pure `settings.DATABASES` string inspection and opens no connection, so `Tags.database` was simply the wrong tag → `@register()` or `@register(Tags.security)`. Do **not** instead change the entrypoint to `check --database default`: that makes a cheap connection-free guard depend on a reachable database at boot.

Then correct the four false statements: `config/settings/prod.py:19-21`, `common/checks.py:298-301`, `docker/entrypoint.sh:34-40`, `docs/DEVELOPMENT.md:189-192` (the last is routed to tech-writer, not you).

**B2 · E100 has zero test coverage — which is why B1 shipped.** `grep -rn "E100\|ENFORCE_NON_TEST_DATABASE" --include=*.py tests/` returns nothing. The round added eleven tests and none covers the control it documents.

**The constraint on the fix is the important part:** the test must drive `django.core.checks.run_checks()` or `call_command("check")` end to end. A test that calls `check_database_is_not_test_named(None)` directly passes *today, with the wrong tag*, and would have proved nothing — the same tautology class as the item the code-reviewer parked last round. Cover: a `test_`-named database under `ENFORCE_NON_TEST_DATABASE=True` produces `common.E100` through the registry; a normal name does not; and the flag off is silent.

## C. BLOCKING — devops (entrypoint and image)

**C1 · `docker/entrypoint.sh` argv dispatch is wrong in both directions and fails open.** All measured:

- **False positive:** `docker compose run --rm web pytest -k runserver` printed `entrypoint: running manage.py check`, then migrated the **dev** database. Lines 17-19 of that same file say pytest must never race a migrate. `manage.py help runserver` trips it too.
- **False negative (fails open):** `bash -c 'python manage.py runserver'` produced no entrypoint output at all. Same for `/usr/local/bin/gunicorn config.wsgi` (absolute path in a k8s `command:`), `hypercorn`, `granian`, `waitress-serve`, `sh -c "gunicorn ..."`. The only pre-boot guard is silently skipped — and the "skipping" line never prints either, so there is no signal.
- **Fall-through:** with no arguments, `exec` on an empty word list is a no-op that *returns*; control falls past the guard and runs `check` + `migrate` anyway, then exits 0.

**Fix:** stop inferring intent from argv. Gate on an explicit `RAPORO_ROLE=server` (compose sets it, deploys set it), or invert the logic — run the pre-boot sequence by **default** and exempt a known list (`pytest`, `ruff`, `manage.py`, `bash`, `sh`), matching on `$(basename "$1")` rather than scanning every argument. Add `[ "$#" -gt 0 ] || { echo "entrypoint: no command given" >&2; exit 64; }`. Whichever shape you choose, it must **fail closed**: an unrecognised command gets the guard, not a bypass.

**C2 · The stage labelled "the deployable image" ships Django's dev server.** `runtime` inherits `CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]` from `base`. `requirements.txt` has no gunicorn/uvicorn/daphne, so the entrypoint's own dispatch list cannot match anything installed in either image.

**Fix, either is acceptable:** give `runtime` a real server, or delete the "deployable image" framing and say plainly it is a placeholder awaiting the deploy task. A comment asserting a production property the artifact does not have is what is not acceptable. Slice 1 does not need a production server — an honest placeholder is a fine answer, and probably the right one.

**C3 · Medium — `.env.*` is neither gitignored nor dockerignored.** `.gitignore:151` and `.dockerignore` both list `.env` exactly. A `.env.production` or `.env.staging` on a developer's machine would be committed **and** baked into an image layer. Make both `.env*` with a `!.env.example` negation.

**C4 · Low — `RUN chown -R raporo:raporo /app`** makes the application code writable by the runtime user: post-RCE persistence for free. Nothing in `/app` needs to be writable at runtime.

**C5 · Low — `tests/`, `CLAUDE.md`, `.mcp.json` ship in the runtime image.** No secrets, but dead surface.

## D. Nits — worth doing now, all cheap

**D1 · The B1 escape hatches (three gates found overlapping subsets; the security gate's fix closes all of them at once).**
- `_looks_like_sql` only knows `CREATE/DROP/ALTER/SET/COMMENT`. Unversioned `REVOKE`, `GRANT`, `INSERT`, `TRUNCATE`, `DO $$`, `WITH ... UPDATE` constants all pass. **The ledger's own routed devops follow-up is literally a `REVOKE` statement**, so this is the most probable real miss.
- SQL held in a dict, list or class attribute is invisible to the `vars(db)` scan.
- The AST scan only matches `ast.ImportFrom` with `module == "common.db"`. `from common import db` and `import common.db as cdb` both score zero hits.
- `pinned_names` reduces the two function keys to `append_only_triggers_v1`, so a slice-2 migration calling `append_only_triggers_v1("ledger_stockmovement")` counts as pinned while the text it replays is hashed nowhere.

**One test closes all four:** import every `apps/*/migrations/*.py` module and assert every `RunSQL` operation's `sql` and `reverse_sql` hashes to a value in `PINNED_SQL`. That also covers migrations that inline their SQL, which nothing currently does. Keep the structural versioning test as well, with `_looks_like_sql` inverted (treat every non-underscore module-level `str` as SQL unless allowlisted).

**D2 · The documented V1→V2 path is wrong in two ways** — and this is the path B1 exists to make safe, so it matters more than its size suggests. In `common/db.py`'s docstring:
- **Ordering across apps is not automatic.** A V2 shipped in a new app without an explicit `dependencies` edge on every migration installing an earlier version can be ordered *before* `audit/0002` on a fresh install, ending on the V1 body while an already-migrated database ends on V2 — the exact divergence the mechanism prevents, arriving through the door the docstring holds open.
- **A V2's `reverse_sql` must be `CREATE_APPEND_ONLY_FUNCTION_V1`** (restore the previous body), never a DROP. Measured: `DROP FUNCTION raporo_append_only()` is refused by Postgres while any guarded table's trigger depends on it.

**D3 · The pattern as documented produces an irreversible slice-2 migration.** Both `common/db.py` and `audit/0002` tell slice 2 to "install the identical guard with one call". Taken literally — copying both `RunSQL` operations — the ledger migration becomes irreversible the moment two guarded tables exist: reversing drops its own triggers, then hits `DROP FUNCTION` while `audit_auditlog`'s triggers still depend on it. Document that slice-2 migrations declare `dependencies = [("audit", "0002_append_only_trigger")]` and carry the **trigger operation only**; the function is installed once, by `audit/0002`, and its lifecycle stays there.

**D4 · `apps/orgs/migrations/0001_initial.py` reversal caveat is accurate but materially incomplete.** Django's backwards plan for `migrate orgs zero` also unapplies `audit.0002` and `audit.0001`. **It destroys the entire audit log.** For a migration whose sibling is an append-only forensic table the application may not delete a single row from, that is the row-set worth naming. One clause.

**D5 · The post-`loaddata` deferred-constraint window — a slice-2 landmine.** Postgres `check_constraints()` ends with `SET CONSTRAINTS ALL DEFERRED`, which is transaction-scoped. Every `loaddata` inside an outer transaction (any `TestCase`) leaves all four `*_same_org_fk` keys **deferred for the rest of that transaction**. Measured: a cross-org UPDATE refused at baseline was *accepted* after a `loaddata` in the same test, surfacing only at teardown attributed to the wrong test. Slice 2 will write ledger fixtures and then assert bad writes are refused — those assertions would pass vacuously. Add a test pinning this behaviour and a prominent docstring warning: never assert a constraint violation after a `loaddata` in the same test; issue `SET CONSTRAINTS ALL IMMEDIATE` after loading, or keep them in separate test methods.

**D6 · `tests/test_fixture_loading.py`:**
- The docstring says "the four `*_same_org_fk` composite foreign keys"; it exercises three. The untested one, `audit_auditlog_store_same_org_fk`, is the one that coexists with the append-only trigger — i.e. the exact combination B6 was commissioned to de-risk. Add an audit row to the ordered fixture, or narrow the docstring.
- The child-first test asserts only `"_same_org_fk" in str(exc.value)`; assert the specific name (`orgs_storeaccess_membership_same_org_fk`), as the cross-org test already does for its own.
- `test_a_dumpdata_round_trip_restores_every_row` claims to be "the regression guard for slice 2" but hard-codes `("accounts", "orgs")`, so it cannot catch what its docstring describes. `dumpdata` without `--natural-foreign` never calls `sort_dependencies`; across apps the order is `INSTALLED_APPS` order and nothing else. A slice-2 app landing above `apps.orgs` produces an unrestorable full dump while this test stays green. Make the dump unscoped, or derive the app list from `INSTALLED_APPS` (excluding `contenttypes`, `auth.Permission`, `sessions`, `admin.LogEntry`).
- `disable_constraint_checking()` is called without a `finally`. It is a no-op on Postgres today, which is what the test asserts — but the day Django implements it, this test disables constraint checking on a session-scoped connection and then fails, leaving whatever runs next unguarded. Wrap it.

**D7 · `bulk_update` raises `CrossStoreReferenceError` from inside Django's `atomic(savepoint=False)`**, which marks the surrounding transaction for rollback — a caller that catches it cannot continue in the same transaction. Fail-closed and the right direction; needs a docstring line so a service author is not surprised.

**D8 · `# syntax=docker/dockerfile:1`** buys nothing here (no `RUN --mount`, no heredocs, no `COPY --link`) and costs a registry round-trip; it failed one gate's build this session. Drop it, or pin a digest as a deliberate choice.

## E. Explicitly NOT this round — carried forward

- **CSP, `SECURE_PROXY_SSL_HEADER`, `CSRF_TRUSTED_ORIGINS`** in `config/settings/prod.py` — harmless while `/healthz` is the only view; must land in the slice that renders the first HTML page or ships behind a proxy.
- **Runtime DB role must not own these tables nor hold `TRUNCATE`**; "no prod/staging database named `test_*`" as a written ops invariant; a **separate production compose manifest** (today `compose.yaml` pins `target: dev`, dev settings with `DEBUG=True` and `ALLOWED_HOSTS=["*"]`, *and* auto-migrate in one file — anyone running it on a server gets all three). All → devops-engineer at deploy.
- **`User` erasure / anonymisation under Rwanda Law 058/2021** → needs a `privacy-compliance` decision before any account-deletion UI.
- **A DB `CheckConstraint` for the username shape** — accepted as residual.

## Evidence required before re-review

Re-run and paste verbatim: full `pytest -v`, `ruff check .`, `manage.py check`, and `makemigrations --check --dry-run` under both settings. For A1, A2, B1 and C1 specifically: **paste the failing output from before your fix and the passing output after.** Presence is not verification — that is what let A2 through a whole round, and both the previous agent and the controller made that mistake. The security gate will re-run its own ~120-probe harness against your result.
