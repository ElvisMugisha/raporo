# Slice 1 — Handoff (machine move, 2026-09-01)

Committed as **work-in-progress** to move from the work laptop to the home laptop. Slice 1 is NOT finished. This file + `LEDGER.md` + the `task-*` reports here are the full record; the live SDD workspace (`.superpowers/`) is gitignored and does NOT travel, which is why these copies exist.

## Where we are
- Branch: `feat/slice-1-foundation` (off the docs branch that carries the spec + plan).
- **Task 0** (dockerized Django 6.1 scaffold): complete, reviewed, clean.
- **Tasks 1+2+3** (merged: `common/` bases, `accounts`, `audit`, `orgs` + migrations): implemented; 254 tests green as of fix round 1.
  - Re-reviews after fix round 1: **code-reviewer** APPROVE WITH NITS, **database-engineer** APPROVE WITH NITS, **security-engineer** BLOCK (10/12 closed).
  - **Fix round 2** (closing the last 2 security findings + 6 cleanups): PARTIALLY landed — see below.

## Fix round 2 — exact state (verify on the new machine with the greps in LEDGER.md)
LANDED on disk:
- A1 — update-path FK bypass CLOSED: `update()` calls `_refuse_store_reparenting()` + `_check_update_fk_stores()` in `common/managers.py`.
- A2 — set operators CLOSED: `__ror__`/`__rand__`/`union`/`intersection`/`difference` on the scoped queryset.
- B2 — prod `test_*` database-name guard in `config/settings/prod.py`.
- B3 — image validator magic pre-sniff + `Image.DecompressionBombError` in `common/validators.py`.
- B4 — orgs/0001 docstring: partially corrected (re-verify wording).

STILL TO DO (resume here):
- **B1 (MUST-FIX before slice 2)** — `apps/audit/migrations/0002_append_only_trigger.py` still imports the live `CREATE_APPEND_ONLY_FUNCTION`/`append_only_triggers` from `common/db.py` with no stability pin. Add `tests/test_db_stability.py` pinning a SHA-256 of the literal SQL those helpers produce for `audit_auditlog`, so any future edit is a loud, reviewed break. (An agent was one step from writing this when we stopped.)
- **B5** — strengthen `tests/test_common_checks.py::test_the_check_honours_the_app_configs_it_is_given` with a deliberately-failing model in an excluded app (today it only asserts `== []`, which passes even if the filter is dropped).
- **B6** — loaddata/fixture regression test against the four `*_same_org_fk` composite FKs (proves DEFERRABLE INITIALLY IMMEDIATE still tolerates out-of-order fixtures).
- Write the "FIX ROUND 2" section into `task-123-report.md` with verbatim `pytest -v`, `ruff check .`, `manage.py check`, `makemigrations --check` output.
- Then re-run the three gate re-reviews; security must confirm A1+A2 closed → PASS.

## To continue on the home laptop
1. `git clone` / pull the branch, then **`docker compose build`** — Pillow was added (image validation), so the image must rebuild.
2. **Add `DJANGO_MEDIA_ROOT=/var/lib/raporo/media` to `.env.example` by hand** — agents can't (it's inside the denied `.env*` glob). Prod requires it with no fallback; dev has a default in `base.py`.
3. First DB run must start clean: `docker compose down -v` then `pytest --create-db` (the AUTH_USER_MODEL swappable-user history requires a fresh volume — see LEDGER.md Task 0).
4. Restore the live SDD workspace if you want the subagent-driven skill to resume tracking: copy this folder's files back to `.superpowers/sdd/2026-09-01-slice-1-foundation/` (LEDGER.md → progress.md).

## Routed to devops-engineer (not code, do at deploy)
- Runtime DB role must NOT own these tables and must NOT hold TRUNCATE (compose currently connects as superuser+owner `raporo`, so a compromised app process could wipe the audit trail).
- "No production/staging database may be named `test_*`" as a written ops invariant.

## Open product decision carried forward
- `User` erasure/anonymisation pathway under Rwanda Law 058/2021 — needs a `privacy-compliance` decision before any account-deletion UI (both reviewers flagged; B3's hard-delete guard only refuses deletion, it doesn't provide erasure).
