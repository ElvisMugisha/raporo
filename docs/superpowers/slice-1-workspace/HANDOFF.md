# Slice 1 — Handoff (home laptop → work laptop, 2026-09-01 night)

Committed as **work-in-progress**. Slice 1 is NOT finished. This file + `LEDGER.md` + the `task-*` reports here are the full record; the live SDD workspace (`.superpowers/`) is gitignored and does NOT travel, which is why these copies exist.

**Read this file, then `LEDGER.md`, before touching code.** Every claim below has a command next to it. Run the commands — do not trust the prose. The last handoff was accurate about seven things and optimistic about one, and the optimistic one cost a full round.

---

## Where we are in one paragraph

Slice 1 has 14 tasks. **Task 0** (dockerized Django 6.1 scaffold) is complete and reviewed. **Tasks 1+2+3** were merged into one dispatch (`common/` bases + accounts + audit + orgs + all migrations) and have now been through **three fix rounds and four gate reviews**. Fix round 3 is **fully landed on disk and independently verified**, but **its gate re-reviews have not been run** — that is the first job tomorrow. **Tasks 4–14 have not been started.**

## First thing tomorrow, in order

1. `git fetch && git checkout feat/slice-1-foundation && git pull`
2. **`docker compose build`** — the image changed (multi-stage, entrypoint, `postgresql-client` in the dev target). A stale image will behave nothing like this document says.
3. `cp .env.example .env`
4. **Add `DJANGO_MEDIA_ROOT` to `.env.example` by hand** — still outstanding, still cannot be automated (see "Blocked on Elvis" below).
5. `docker compose down -v && docker compose up -d --wait` — should reach both-containers-healthy in ~25s.
6. Verify the state this document claims (the block below). If anything disagrees, believe the machine.
7. **Then: run the three gate re-reviews on fix round 3** (code-reviewer, security-engineer, database-engineer). Security's BLOCK cannot lift until it re-runs its own harness against A1/A2.

## Verify the claimed state — run these, don't assume

```bash
docker compose run --rm web pytest -q                      # expect: 330 passed
docker compose run --rm web ruff check .                   # expect: All checks passed!
docker compose run --rm web python manage.py check         # expect: no issues (0 silenced)
docker compose run --rm web python manage.py makemigrations --check --dry-run
docker compose run --rm web python manage.py makemigrations --check --dry-run --settings=config.settings.test
docker compose run --rm web pytest -q -k runserver | grep -c "entrypoint:"   # expect: 0
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/healthz       # expect: 200
```

All of the above were green at 2026-09-01 end of session, run by the controller, not by the implementing agents.

---

## Fix round 3 — what landed (all verified by execution)

Brief: `task-123-fix-round-3.md`. It consolidates four gate reports and is the document tomorrow's re-reviews should be judged against.

| Item | What it was | Verified how |
| --- | --- | --- |
| **A1** | Cross-org `\|`/`union`/`intersection`/`difference` leaked across organizations; `^`/`__rxor__` unguarded both directions | All four leaking expressions executed before and after; 45-case operator matrix (9 operators × 5 legs), 30 cases failed pre-fix |
| **A2** | Multi-store `for_stores()` pin allowed a cross-store FK update | Probe executed before and after |
| **B1** | `common.E100` was inert — registered `@register(Tags.database)`, which bare `manage.py check` skips | Re-tagged `Tags.security`; controller confirmed it now **refuses** a `test_`-named prod DB and stays silent on a normal one |
| **B2** | E100 had zero tests | 6 registry-driven tests; 4 fail on the old tag |
| **C1** | Entrypoint dispatch failed open and also fired on `pytest -k runserver` (it migrated a real database) | 13-shape matrix measured before and after |
| **C2** | `runtime` stage claimed to be "the deployable image" while shipping Django's dev server | Reframed as an explicit placeholder — see the decision below |
| **C3** | `.env*` neither gitignored nor dockerignored | `git check-ignore` both directions; `.env.example` confirmed still tracked |
| **C4/C5** | `/app` writable by the runtime user; `tests/`, `CLAUDE.md`, `.mcp.json` in the runtime image | Confirmed in the built image |
| **D1–D8** | B1 escape hatches, V1→V2 path errors, the post-`loaddata` deferral window, fixture-test gaps | Each with a regression test; D1 mutation-checked |

Two findings from inside the round that were nobody's assignment:

- **A latent bug the gates missed.** `NoHardDeleteQuerySet.__ror__`/`__rand__` delegated to `super().__ror__()` — and Django 6.1's `QuerySet` defines neither. Both would have raised `AttributeError` on first real use. They were present (a grep found them) and unreachable. `hasattr` also lies here: it finds `type.__ror__` via PEP-604. This is precisely how round 2's A2 was reported closed.
- **Healthcheck budgets were too tight for a cold start.** The first wiped-volume `up --wait` *failed*: `db` had `start_period: 15s` against a ~30s `initdb`, and `web` came in at 102s against a budget of exactly 102s. Fixed with `start_period` (db 60s, web 120s), deliberately not `retries`.

## Decisions taken this session (do not re-litigate)

- **C2 — no production server yet.** `runtime` is an honest placeholder, not a deployable image. Adding gunicorn now means a dependency with no consumer and no reviewed configuration (worker model, timeouts, graceful shutdown, access-log format, proxy header trust). Those belong with the deploy task.
- **C1 — the entrypoint fails closed.** Pre-boot runs by default; a narrow tooling list is exempt. This overruled the fix brief, which suggested an exempt list that was itself fail-open.
- **B1 — retag, don't change the entrypoint.** The check is pure `settings.DATABASES` string inspection; making a connection-free guard depend on a reachable database at boot would be worse.

## Two things about the entrypoint you will trip over

1. **`RAPORO_ROLE=server` must never go in a compose service's `environment:`.** `docker compose run` inherits it and would force the pre-boot sequence back onto `pytest`, reintroducing the exact bug C1 fixed. It belongs on a deploy workload spec.
2. **Fail-closed has a visible price.** `docker compose run --rm web python -c '...'` and `bash -c 'ruff check .'` now run check + migrate. Opt out with `-e RAPORO_ROLE=tooling`. `docker compose exec` bypasses the entrypoint entirely and is unaffected.

---

## Open work, in priority order

### 1. Run the three fix-round-3 gate re-reviews (blocking the slice)
Nothing has reviewed round 3. Security's **BLOCK stands** on the record until it re-runs its own ~120-probe harness against A1 and A2 and lifts it. Dispatch all three against `task-123-fix-round-3.md`, each in its own compose project (`-p raporo-gate-sec` etc.) so they don't race the same test database — three concurrent `pytest --create-db` runs on the default project will collide.

### 2. `docs/DEVELOPMENT.md` has 8 known-stale lines
The C1 polarity inversion landed after the docs were written. Route to tech-writer:
- **L53–54** — says the pre-boot sequence only runs for `runserver`/`gunicorn`/`uvicorn`/`daphne`. Polarity is now the opposite: it runs by default, a short list is exempt.
- **L62–64** — "Every other command is exec'd untouched: `pytest`, `ruff`, `manage.py <anything>`, `bash`." Three corrections: `manage.py <anything>` is now *except* `runserver`/`runserver_plus`/`testserver`; `bash` means bash *with no arguments*; and `python -c`, `python -m`, `sh -c '…'` now **do** get the pre-boot sequence.
- **L134** — "the deployable image". Now explicitly wrong (C2).
- **env table** — `RAPORO_ROLE` is new and undocumented. Needs a row beside `RAPORO_AUTO_MIGRATE`, including the never-put-it-in-compose warning.
- **L171–172** — "`runtime` target is what deploys". Nothing deploys it.
- **L173** — "for server commands only" → "for everything except an exempt tooling list".
- **L204–205** — cold start is now budgeted up to 2 minutes (30–100s in practice); one clause so nobody kills it early.
- **L206–208** — the entrypoint's diagnostics now go to **stderr**, and an exempt command prints nothing at all.

A `craft-editor` pass is also still owed on `README.md` and `docs/DEVELOPMENT.md` — neither has had one.

### 3. Then Tasks 4–14
`docs/superpowers/plans/2026-09-01-slice-1-foundation.md`. Task 4 is services on top of the models that now exist.

## Carried forward — deliberately not this slice

- **Slice 2 must know all of this before copying the append-only pattern** to StockMovement / Payment / CapitalEntry / Payout. It is written up in `LEDGER.md` under the database-engineer's round-2 re-review; the four that matter most:
  1. Do **not** copy the function-install operation — declare `dependencies = [("audit", "0002_append_only_trigger")]` and carry the trigger operation only, or your migration is irreversible once two tables are guarded.
  2. **Never assert a constraint violation after a `loaddata` in the same test.** Postgres' `check_constraints()` ends with `SET CONSTRAINTS ALL DEFERRED`, which is transaction-scoped, so every composite key goes quiet for the rest of that test and the assertion passes vacuously.
  3. Fixtures must be parent-first — our composite keys are `INITIALLY IMMEDIATE`, Django's own FKs are not, so intuitions from other Django projects do not transfer.
  4. An append-only table cannot be wiped. "Restore over an existing database" is not a supported operation; restore means fresh DB → `migrate` → `loaddata`.
- **`MEDIA_ROOT` must stay outside `/app`.** C4 (root-owned application code) is only safe because of it. If a later change points `MEDIA_ROOT` back into the tree, C4 becomes an outage rather than a hardening nit. Deserves to be a written ops invariant.
- **Deploy-time, devops-engineer:** runtime DB role must not own these tables nor hold `TRUNCATE`; "no prod/staging database named `test_*`" as a written ops invariant; a standalone `compose.prod.yaml` (recorded shape: split by *file*, never a profile or an override-merge — `DEBUG=True` should be absent from the file you deploy, not switched off in it).
- **Security, at first HTML page:** no CSP, `SECURE_PROXY_SSL_HEADER` or `CSRF_TRUSTED_ORIGINS` in `config/settings/prod.py`. Harmless while `/healthz` is the only view.
- **Product/privacy, before any account-deletion UI:** `User` erasure / anonymisation under Rwanda Law 058/2021. Flagged independently by two reviewers. Soft delete alone does not satisfy erasure while PII columns remain.
- A DB `CheckConstraint` for the username shape — accepted as residual.

## Blocked on Elvis (cannot be automated)

**Add to `.env.example`:**
```
DJANGO_MEDIA_ROOT=/var/lib/raporo/media
```
Agents are refused by the `Read(./.env*)` deny rule — confirmed this session that it blocks even a blind append. Dev is unaffected (`base.py` falls back to `/var/tmp/raporo-media`); prod requires it with no fallback, deliberately, so a deploy fails fast rather than scattering uploads.

## Process notes for the next session

- **Presence is not verification.** This round's two worst defects — a set-operator guard that delegated to a nonexistent superclass method, and a system check filtered out at runtime by a framework default — were both *present, correctly named, and referenced in documentation*. A grep found both and reported them closed. Verify by executing the thing, and for a guard specifically: it is unverified until you have watched it **refuse** something.
- Agents cannot run `git add`/`commit`/`push`. Elvis commits, and only once every agent has reported.
- Three agent sessions have now been killed mid-flight by the host process exiting. Each time, file changes had landed and only the reports were lost. Check disk before re-dispatching.
