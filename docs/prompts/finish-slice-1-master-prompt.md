# Master prompt — finish Slice 1 to production grade

Paste everything below the line into a fresh Claude Code session in this repo.

---

You are the controller for a multi-agent engineering push on Raporo. Read `CLAUDE.md` first; its principles bind you and every agent you dispatch. Work through the phases below **in order** and do not skip a gate.

## The goal, stated precisely

**Finish Slice 1 — the foundation — completely and to production grade.** Not "the backend": slices 2–6 (products/stock, selling/credit, the report engine, money intelligence, alerts) are out of scope and must stay out. Slice 1 has 14 tasks; Task 0 and merged Tasks 1+2+3 are done, plus part of a tenancy-hardening round. **Tasks 4–14 are untouched.** Your job is everything remaining in Slice 1, ending in a state a paying customer's data could live in.

If you find yourself about to touch slice 2 scope, stop and say so.

## Phase 0 — Orient, and verify rather than read

1. `docs/ROADMAP.md` — the 📍NOW line and the Phase C table. This is the living tracker.
2. `docs/superpowers/slice-1-workspace/HANDOFF.md` — resume point, carried-forward items, blocked-on-human items.
3. `docs/superpowers/slice-1-workspace/LEDGER.md` — **long, and the single most valuable file in the repo.** Every dispatch, every gate verdict, every ruling with its cost-if-wrong, and every incident. Read it. It will save you from re-deciding things that were decided expensively.
4. `docs/superpowers/specs/` — five design documents (~5,400 lines) and `docs/adr/` — eleven ADRs. **Read ADR 0008–0011 and the Amendment sections; several correct earlier text.**

Then **verify the state by execution**, because this project's most expensive lesson is that a claim in a document is a hypothesis:

```bash
docker compose run --rm web pytest -q                    # expect: 574 passed
docker compose run --rm web ruff check .
docker compose run --rm web python manage.py check
docker compose run --rm web python manage.py makemigrations --check --dry-run
docker compose run --rm web python manage.py makemigrations --check --dry-run --settings=config.settings.test
```

**The stack needs three secrets in `.env` that agents cannot write** (`Read(./.env*)` is denied): `RAPORO_APP_PASSWORD`, `RAPORO_MIGRATE_PASSWORD`, `RAPORO_BACKUP_PASSWORD`. If compose refuses to interpolate, that is why — ask the human. On a database whose volume predates the role split, run `docker compose exec db /docker-entrypoint-initdb.d/10-raporo-roles.sh` then `docker compose up -d --wait`.

Report what you found, and **name any disagreement between a document and the machine.** The machine wins.

## Phase 1 — Align the documents. Gate: human approval before any code.

The document layer is lopsided: **~50 lines of product document against ~5,400 lines of architecture spec.** Fix that before building. Produce:

**`docs/PRD.md`** — what we are building, who for, and what it must do. Sources: `docs/PRODUCT.md`, `docs/PROJECT-DESCRIPTION.md`, and the product decisions scattered through `LEDGER.md` (the branding chain, biweekly periods 1–15/16–end, invariant #1, store caps, one-org-per-user, the phone/account model). Owner: `product-owner`, then `craft-editor`. It must state acceptance criteria and, explicitly, **what v1 does not do**.

**`docs/ARCHITECTURE.md`** — the complete picture: module layout, data model, tenancy model, the tech stack and its versions, and the **intended directory tree including the frontend** as a diagram. Consolidate from the five specs and eleven ADRs; link to them rather than restating them, and correct anything they contradict. Owner: `architect`.

**`docs/ARCHITECTURE-ESSENTIALS.md`** — an outline of only the load-bearing decisions, sized so an agent can read it in full before starting work without drowning. Target **under 150 lines.** It is an index with verdicts, not a summary. Owner: `architect`.

**`CLAUDE.md`** — update it to current reality (Python 3.14, PostgreSQL 18, the role split, the new docs). Keep it lean; it loads every session.

**`AGENTS.md`** — a **thin pointer**, not a second source of truth. The 19 role definitions live in `.claude/agents/*.md` and the rules live with the owning role by ADR 0005. Duplicating them creates two things to keep in step, and one will rot. Make it: what this repo is, where the team is defined, where the rules live, the non-negotiables, and the standing rules below.

**Do not create empty scaffolding files or directories.** The intended structure goes in `ARCHITECTURE.md` as a diagram. This repo's Dockerfile copies path-by-path, an empty `.py` is lint-clean, and this project has spent five fix rounds on controls that were present and never executed — an empty tree is that failure mode by design. Scaffold a directory when the task that fills it starts.

**Stop at the end of this phase and get human approval.** Then continue.

## Phase 2 — Ask your questions, together, once

Before building, dispatch `product-owner`, `architect`, `security-engineer`, `database-engineer`, `data-reporting-engineer` and `privacy-compliance` to read the state and each surface every decision they need from the human. Then **consolidate into one set of questions and ask them all at once**, each with a clear recommendation. Do not trickle questions.

These are already known open and must be included:

- **OD1 — IP retention.** Recommendation on record: no full IPs in `AuditLog`; truncate to /24 and /48 for security actions only; full IPs live only in the throttle cache. **Blocks Task 5.**
- **OD2 — where the VPS lives.** If outside Rwanda, every byte of personal data is a cross-border transfer under Law 058/2021 Arts 48–49. **Launch blocker.**
- **OD3** — are investors data subjects in their own right (own login) or records inside the org? Recommendation: records, for v1.
- **OD4** — NCSA/DPO registration, DPA annex in the Terms, named DPO. **Launch blocker.**
- **OD5** — retention after account closure. Recommendation: erase identifiers 30 days after closure; financial records and audit trail 10 years.
- **OD6** — whose 10-year duty is it? Recommendation: state it in the Terms as a contractual instruction from the org.
- **Audit `changes` value-level PII** — key-based redaction cannot see PII inside values (`{"members": ["a@b.rw"]}` has no key). Policy call, not a bug.
- **`store_name` / `org_name` in audit rows** persist in an un-erasable table and are personal data for a sole trader. Accepted deliberately; confirm or overturn.

## Phase 3 — Build, in this order

Each step is one commit, independently verifiable and revertible. The full plan is in `docs/superpowers/specs/2026-09-02-tenancy-hardening-design.md` §F and the schema plan's §E.

**Finish the tenancy hardening first** (designed, partly built):

1. **`org` column on `StoreScopedModel`** + the composite FK `(org_id, store_id) → orgs_store(id, org_id)`, plus the `ScopePin(org_pk, store_pks)` refactor. `StoreScopedModel` has **zero concrete subclasses** today, so this needs no data migration — the last moment that is true. Literal `RunSQL` per table, **never a loop in the migration** (it defeats the SQL pin); the loop goes in the test. `DEFERRABLE INITIALLY IMMEDIATE`, not `DEFERRED` — deferred violations surface only at COMMIT, which never happens inside a `TestCase`, so negative tests would pass vacuously. Owner: `backend-engineer` + `database-engineer`.
2. **One live membership per user** — `UniqueConstraint(fields=["user"], condition=LIVE, violation_error_code="unique")`. Keep the join table: allowing multi-org later must stay a `DROP CONSTRAINT`, not a data migration. Schema plan §J has it fully specified.
3. **`store.access_all` + `permitted_stores()`** — and **exhaustive explicit `PRESETS`** in the same commit. `PRESETS["Manager"]` is subtractive, so merely adding the code to the catalog grants it to Manager and breaks the denial matrix on day one. Never key the override on `Role.name` (user-editable). Owner: `backend-engineer`, per ADR 0011.
4. **`common/tenancy.py::tenant()`** — one context manager, `SET LOCAL` inside a transaction it opens itself, `contextvars` with mandatory token reset. `SET LOCAL` outside a transaction is a silent no-op warning. Middleware, management commands and any future task are *callers* of one door. Add the source-scan test refusing a bare `SET` on a `raporo.*` GUC elsewhere.
5. **RLS policies** — `ENABLE`, **not `FORCE`** (see ADR 0009's Amendment; `FORCE` + `BYPASSRLS` is self-cancelling and the cleanup silently no-ops backfills). `USING` **and** `WITH CHECK`. `NULLIF(current_setting('raporo.org_id', true), '')::bigint` — without `NULLIF` an unset GUC raises `22P02` after the first request on a connection. Owner: `security-engineer` designs, `backend-engineer` builds. **Mind that `test_raporo` currently gets no grants** — the first `SET LOCAL ROLE raporo_app` fixture will hit *permission denied* rather than a policy; wire `grant_runtime_privileges` into `post_migrate` or a session fixture.
6. **Indexes** — every index on a tenant table leads with `org`. Six proposed, seven deferred, and **nineteen removed**: Django indexes every FK column, so the audit/soft-delete bases ship three unused indexes per table. Net −13. Schema plan §D. Also: if the manager filters `org_id` explicitly, Postgres collapses the policy to a one-time filter — 1.045 ms → 0.146 ms measured.
7. **Connections and timeouts** — `CONN_MAX_AGE` or a psycopg3 pool, per-role `statement_timeout`, `lock_timeout` on the migration connection, and the safe-online / needs-a-window classification convention.
8. **`common.E101`** asserting the runtime identity at boot, widened to assert `raporo_app` holds no `UPDATE`/`DELETE` on `audit_auditlog` and no write on `django_migrations`. Phase 2 of the role split is a step, and steps get skipped.

**Then Slice 1 Tasks 4–14** from `docs/superpowers/plans/2026-09-01-slice-1-foundation.md`: the service layer, multi-identifier auth with throttling, 2FA, invites, i18n and the header switcher, password reset. Two constraints are already binding on Task 4: `audit.record` must not receive `ip`, and no audit row may echo a user's identifiers.

**Then the cross-cutting work:**

9. **Period boundaries and timezones.** *This is the largest untested risk in the project and it is not security.* The product is period-based reporting: biweekly 1–15 and 16–end, in the org's timezone. Nothing tests it. Owner: `data-reporting-engineer`, which has never yet run on this codebase. Include month ends, leap years, DST-free-but-offset zones, and a store whose timezone differs from its org's.
10. **The generated denial matrix.** Two orgs, two stores in org A, and **five** actors — the fifth is `a_decoy`, a role literally *named* "Owner" holding only `sale.record`, with the real owner role named something else. It is the only row that proves the check is not name-based. Generate the table over the router so a new endpoint is covered the day it is written. **404, never 403** — and `StoreNotPermitted` must not subclass `PermissionDenied`, which Django renders as 403.
11. **Per-tenant export and erasure** — `export_org`, `export_user`, `erase_org`, `erase_user`, plus `common.E200` and the `ERASURE_PLAN` registry. Privacy ruling §2/§4. Note: the "any other membership?" check must run over the **live** manager, or anyone who ever left an org is never erased.
12. **Performance budgets and the measurement harness** — budgets, `assertNumQueries` on every endpoint, and committed `EXPLAIN` plans. **Do not optimise before measuring**; two perf notes are already deferred with explicit triggers. `dbshell` must connect as `raporo_app` or every plan you look at is the wrong plan.

## Phase 4 — Gates. No step merges without them.

Per `CLAUDE.md` and ADR 0004: `code-reviewer` on every diff · `security-engineer` on anything touching auth, tenancy or input · `database-engineer` on schema and hot queries · `data-reporting-engineer` on anything touching sums, dates or timezones · `qa-engineer` on coverage and denial tests · `performance-engineer` on hot paths · `sre-observability` for instrumentation · `privacy-compliance` on personal data · `tech-lead` as the merge gate. Then `/production-readiness` before anything reaches `main`.

`tech-writer` and `craft-editor` run **continuously, not at the end.** After every step that changes observable behaviour, `tech-writer` reconciles `docs/DEVELOPMENT.md`, `docs/ARCHITECTURE.md`, `ARCHITECTURE-ESSENTIALS.md`, `README.md` and `docs/ROADMAP.md` against what the code now does — **verified by running the commands, not by reading the diff.** In this project the documentation pass has found real defects three times, because the writer is the first participant forced to actually use the thing.

## Phase 5 — Deliver

1. **One consolidated report** combining every agent's findings — verdicts, what was measured, what was deferred and why, what is still open. Do not make the human read individual agent reports.
2. **One human commit message** per logical commit: a plain title and dotted description, honest about what landed and what did not. The last commit's subject claimed schema hardening that was only designed; do not repeat that.
3. **How to test it** — the exact commands, expected output, and how to exercise the new behaviour by hand: the denial matrix, an RLS refusal, a period boundary, an erasure.
4. Update `ROADMAP.md`, `HANDOFF.md` and `LEDGER.md` in the same change as the work.

## Standing rules — earned expensively, do not relearn them

- **Presence is not verification, and a grep is a presence check.** Four controls in this slice were present, correctly named, documented, and never executed. **A guard is unverified until you have watched it refuse something.** `hasattr` lies about `__ror__` — use `in QuerySet.__dict__`.
- **Validate a regression test by mutating the specific guard it targets**, and state the observed failure. A prescribed test once passed with the guard deleted, because a second guard raised first.
- **A reviewer's suggested mechanism is the least-verified artifact in the loop.** Three times this slice an implementer overruled a prescribed fix and was right. State the *property* and the acceptance test; label any mechanism "one option, untested". When an implementer overrules, send it back to the prescribing gate for acknowledgement.
- **No two parallel agents share a checkout.** Give each a `git worktree` and its own compose project (`-p <name>`), and its own scratchpad subdirectory. A shared tree cost this project a deleted security guard and a "mid-edit read" that looked exactly like a real regression.
- **Never mount into `/app/**`** — it leaves a zero-byte root-owned stub on the host, and an empty `.py` is ruff-clean so lint will not catch it.
- **Start a parallel round from a committed base**, or attribution is lost even with perfect file ownership.
- **Agents cannot run `git add`/`commit`/`push`.** The human commits, and only once every agent has reported.
- **`Read(./.env*)` is denied.** Report needed variables; never work around it.
- **Never edit a `_V1` SQL constant** — add `_V2` plus a new migration. `tests/test_db_stability.py` hashes every `RunSQL` statement in every migration.
- **Apply DRY, SoC and least privilege**, but not at the cost of a guard. Two overlapping mechanisms with different failure modes are defence in depth; delete the duplication only when you can show the survivor catches everything.
- **Ask when a decision is the human's.** Ask once, in a batch, with recommendations.

## What "production grade" means here, concretely

Every guard watched refusing something · every constraint tested raising `IntegrityError` · every endpoint in the denial matrix returning 404 not 403 · migration drift clean under both settings modules · a wiped-volume boot reaching a healthy migrated app · `pg_dump` and a restore rehearsed as `raporo_backup` · period boundaries tested across month ends and offset timezones · no personal data in a table that cannot be erased · budgets recorded with `EXPLAIN` plans committed · and documentation whose every command has been run by the person who wrote it down.
