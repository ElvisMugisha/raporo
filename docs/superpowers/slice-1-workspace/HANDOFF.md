# Slice 1 — Handoff (2026-09-02)

The foundation task is **through the merge gate**. Slice 1 as a whole is not finished — Tasks 4–14 are untouched. This file + `LEDGER.md` + the `task-*` reports here are the full record; the live SDD workspace (`.superpowers/`) is gitignored and does NOT travel, which is why these copies exist.

**Read this file, then `LEDGER.md`, before touching code.** Every claim below has a command next to it. Run the commands — do not trust the prose. Across this slice, four separate controls were found *present, correctly named, documented, and never executed*. Prose is not evidence.

---

## Where we are

| | |
| --- | --- |
| **Task 0** — dockerized Django 6.1 scaffold | ✅ complete, reviewed |
| **Tasks 1+2+3** (merged) — `common/` bases, accounts, audit, orgs, all migrations | ✅ **4 fix rounds, 8 gate reviews, tech-lead MERGE WITH FOLLOW-UPS** |
| **Tasks 4–14** | ⏳ not started |

Round-3 gate verdicts: **code-reviewer** APPROVE WITH NITS · **database-engineer** APPROVE WITH NITS · **security-engineer** APPROVE WITH NITS, **BLOCK withdrawn** ("security-engineer says merge"). Round 4 closed the two findings that were more than cosmetic. **tech-lead: MERGE WITH FOLLOW-UPS.**

## Verify the claimed state — run these, don't assume

```bash
cp .env.example .env                                        # if starting fresh
docker compose build                                        # the image changed; a stale one lies
docker compose run --rm web pytest -q                       # expect: 369 passed
docker compose run --rm web ruff check .                    # expect: All checks passed!
docker compose run --rm web python manage.py check          # expect: no issues (0 silenced)
docker compose run --rm web python manage.py makemigrations --check --dry-run
docker compose run --rm web python manage.py makemigrations --check --dry-run --settings=config.settings.test
docker compose run --rm web python -m pytest --version | grep -c "entrypoint:"   # expect: 0
docker compose down -v && docker compose up -d --wait       # expect: healthy in 12-40s
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/healthz           # expect: 200
```

All green at 2026-09-02, run by the controller and independently reproduced by the tech-lead — not by the implementing agents.

---

## Follow-ups, in order. None blocked the commit; **items 3 and 4 block Task 4.**

1. **`RAPORO_ROLE` validation ordering + the stale `environment:` prohibition** — devops. Round 4's never-list `exec`s before the role validation, so `RAPORO_ROLE=bogus pytest` is silently accepted (rc=0) while every other command exits 64. Safety is intact (a test runner is never pre-booted); the *diagnostic* is lost, on the one invocation a pipeline is most likely to typo. Separately, four places state "never put `RAPORO_ROLE=server` in a compose `environment:`" as absolute — round 4 made it conditional, and two of the four contradict a sibling paragraph in the same file. True rule: **not in `compose.yaml`; yes in `compose.prod.yaml`, because the never-list is what makes it safe.**
2. **This file and `ROADMAP.md`** — kept current as of this rewrite. Re-check after item 1 lands.
3. **Resolve 14 add/add conflicts with `origin/dev`, as its own commit on this branch, before Task 4.** Verified: `git merge-base HEAD origin/dev` is the *initial commit*, because `dev` acquired the AI-team setup through separate PRs after this branch diverged. `git merge-tree HEAD origin/dev` → rc=1, 14 conflicts, all docs/config, **no source code**: seven `.claude/agents/*-engineer.md`, `.claude/skills/new-feature/SKILL.md`, `.gitignore`, `README.md`, `docs/PRODUCT.md`, `docs/ROADMAP.md`, `docs/SETUP.md`, `docs/superpowers/specs/2026-09-01-...`. **The resolution is not "take HEAD"** — dev's copies arrived after the divergence, so each needs `git diff HEAD origin/dev -- <path>`. A ~20-minute tech-writer job. Do it now while it is 14 markdown files, not later when it is 14 plus slice 2.
4. **`privacy-compliance` ruling on Rwanda Law 058/2021, before Task 4** — not before an account-deletion UI, which is where it was previously scheduled. The tech-lead re-dated it and the reasoning is sound: `AuditLog` is append-only at the *database* level with a TRUNCATE trigger, so **any PII that reaches an audit row is structurally un-erasable without a migration**, and Task 4 writes the first services that call `audit.record` for user events. This is a scoped confirmation, not a redesign — the security gate already verified "audit redaction + IP validation + IDs-only logging". Rule on (a) whether the current redaction policy satisfies Law 058/2021, and (b) whether the erasure pathway is `anonymize()` or soft-delete-plus-redaction. Two reviewers converged on this independently, which is the strongest signal in the ledger.
5. **`common.E101`** — make the "nothing the app writes at runtime may live under `/app`" invariant a mechanism rather than prose. It is pure settings-string inspection with no database connection, the same shape as `common.E100`, which now works: refuse a `MEDIA_ROOT` or `STATIC_ROOT` under `BASE_DIR`. Converts a deploy-time `PermissionError` three decisions from its cause into a `manage.py check` failure.

Also owed: a `craft-editor` pass has now run on `README.md` and `docs/DEVELOPMENT.md`. It left five technical statements flagged-but-unresolved; three were adjudicated and fixed, and its remaining two notes are in `LEDGER.md`.

---

## Two things about the entrypoint you will trip over

1. **`RAPORO_AUTO_MIGRATE=1` is in the compose service `environment:`, and every `docker compose run` inherits it.** What makes that safe is that `migrate` requires a *positively identified server*; an unrecognised command gets `check` only and says so on stderr. Do not "simplify" that gate away.
2. **Fail-closed has a visible price.** An unrecognised command runs `manage.py check` before yours. `pytest`, `ruff`, `manage.py <sub>`, a bare shell, and `python -m`/`sh -c` wrappers resolving to known tooling are all exempt. Opt out with `-e RAPORO_ROLE=tooling`; `docker compose exec` bypasses the entrypoint entirely.

## Carried forward — deliberately not this slice

- **Nothing the app writes at runtime may live under `/app`.** `MEDIA_ROOT` and `STATIC_ROOT` are its two instances. Measured in the built runtime image: `STATIC_ROOT` unset → `ImproperlyConfigured` (**this is why root-owning `/app` is currently safe**), `/app/staticfiles` → `PermissionError`, `/var/tmp/raporo-static` → 130 files copied. See follow-up 5.
- **A new top-level Python package must be added to the Dockerfile's COPY list**, or it is missing from the shipped image — and the dev bind mount hides that locally, so it first fails at deploy. The rule is stated at the COPY block.
- **Slice 2, before copying the append-only pattern** to StockMovement / Payment / CapitalEntry / Payout:
  1. Do **not** copy the function-install operation. Declare `dependencies = [("audit", "0002_append_only_trigger")]` and carry the trigger operation only, or the migration is irreversible once two tables are guarded (measured: Postgres refuses `DROP FUNCTION` while any trigger depends on it).
  2. **Never assert a constraint violation after a `loaddata` in the same test.** Postgres' `check_constraints()` ends with `SET CONSTRAINTS ALL DEFERRED`, which is transaction-scoped, so every composite key goes quiet for the rest of that test and the assertion passes vacuously. `tests/conftest.py::load_fixture` re-arms with `SET CONSTRAINTS ALL IMMEDIATE` — use it.
  3. Fixtures must be parent-first: our composite keys are `INITIALLY IMMEDIATE`, Django's own FKs are not, so intuitions from other Django projects do not transfer.
  4. An append-only table cannot be wiped. "Restore over an existing database" is not supported; restore means fresh DB → `migrate` → `loaddata`.
  5. The stability contract is **enforced by construction** — a guard migration cannot be committed green; `test_every_run_sql_statement_in_every_migration_is_pinned` prints the SQL and the digest to paste.
- **Security, at the first HTML page (Task 8):** no CSP, `SECURE_PROXY_SSL_HEADER` or `CSRF_TRUSTED_ORIGINS` in `config/settings/prod.py`. The tech-lead's instruction: make this an **acceptance criterion on Task 8 owned by security-engineer**, not a handoff note — "a note in a handoff is how E100 shipped inert."
- **Deploy, devops:** runtime DB role must not own these tables nor hold `TRUNCATE` (in dev the app connects as owner *and* superuser, so the audit trail is only as strong as that role); "no prod/staging database named `test_*`" as a written ops invariant; a standalone `compose.prod.yaml` — **split by file, never a Compose profile or an override-merge**, because both leave dev defaults reachable by omission and `DEBUG=True` should be absent from the file you deploy, not switched off in it. It sets `RAPORO_ROLE=server`, leaves `RAPORO_AUTO_MIGRATE` unset, drops the bind mount, takes secrets from the platform store. A pipeline `--target runtime` build must run `manage.py check` inside the image. `runtime` must not become pipeline-reachable before it has a real server.
- **`performance-engineer`, when ledger writes land:** `merge_scope_pks()` issues a store lookup per widening combinator at build time (a combinator in a loop is an N+1); `_check_update_fk_stores()` issues one lookup per named store-scoped FK per `update()`, so `bulk_update` pays it per batch. **Do not let slice 2 optimise the first one away without re-proving the leak it closed.**
- Username-shape `CheckConstraint` — accepted residual; the Python validators refuse every shape the security gate threw, including Devanagari numerals.

## Blocked on Elvis (cannot be automated)

**Add to `.env.example`:**
```
DJANGO_MEDIA_ROOT=/var/lib/raporo/media
```
Agents are refused by the `Read(./.env*)` deny rule — confirmed this session that it blocks even a blind append. Dev falls back to `/var/tmp/raporo-media`; prod requires it with no fallback, deliberately, so a deploy fails fast rather than scattering uploads.

## Process notes — earned expensively, worth keeping

- **Presence is not verification, and a grep is a presence check.** Four controls this slice were present, correctly named, referenced in documentation, and never executed: `common.E100` registered under a tag nothing ran; `__ror__`/`__rand__` delegating to a `super()` method Django does not define; `GuardedQuery.combine`'s guard with zero coverage; and a set-operator fix reported closed because a grep found the method names — by the implementer, and then again by the controller. **A guard is unverified until you have watched it refuse something.** Note `hasattr` lies about `__ror__` (it finds `type.__ror__` via PEP-604) — use `in QuerySet.__dict__`, or evaluate the expression.
- **A new test can pass while the bug reproduces.** Round 4's prescribed test passed with the guard deleted, because a second guard added in the same round raised first. Validate a regression test by mutating *the specific guard it targets*, and state the observed failure.
- **A reviewer's suggested mechanism is the least-verified artifact in the loop.** Three times this slice an implementer overruled a prescribed fix and was right: a fail-open exempt list, a test that could not reach its guard, and a check that was dead code on the path it targeted. The property was correct all three times; the mechanism was not. State the property and the acceptance test; label any mechanism "one option, untested". When an implementer overrules, send it back to the prescribing gate for a one-line acknowledgement, or the same gate re-prescribes the same shape next slice.
- **No two parallel agents share a checkout** — implementer or reviewer. Isolated compose projects (`-p <name>`) worked flawlessly all slice; the shared *working tree* caused two incidents (a live security guard briefly deleted by an in-place mutation test, and a "mid-edit read" that looked exactly like a real regression). Use a `git worktree` per agent. Isolate the scratchpad too, per-agent subdirectory.
- **Never mount into `/app/**`** — it leaves a zero-byte root-owned stub on the host, and an empty `.py` is ruff-clean, so lint will not catch it. Happened twice.
- **The sequencing change the tech-lead wants for slice 2:** an implementer's report must contain, per guard it touched, the mutation output — guard removed, test red, guard restored, test green. No mutation evidence, and the dispatch comes back unreviewed rather than going to a gate. Eight gate reviews this slice were largely spent establishing that guards were unverified; that is work the implementer could do for free while the code is warm, and it frees the gates for the leaks nobody imagined.
- Agents cannot run `git add`/`commit`/`push`. Elvis commits, and only once every agent has reported.
- Three agent sessions were killed mid-flight by the host process exiting. Each time, file changes had landed and only the reports were lost. **Check disk before re-dispatching.**
