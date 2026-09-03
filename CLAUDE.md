# Raporo

## What this repo is

Product code plus a fully portable AI-team setup. Everything the team needs — agents, skills, rules, settings, MCP config, bootstrap — is committed here. Clone on any machine, run `scripts/setup.sh`, and the whole team works identically.

## Non-negotiable principles

1. **Production-grade only.** No placeholder code, no TODO-and-move-on, no "works on my machine". Every change ships with tests and passes them.
2. **Small, reviewed steps.** One logical change per commit. Feature branches off `dev`; `main` is always releasable.
3. **Security by default.** Never commit secrets. Validate all external input. Least privilege everywhere.
4. **Decisions are written down.** Anything architectural gets an ADR in `docs/adr/` (use the `adr` skill).
5. **Token discipline.** Keep this file and skills lean. Search before reading whole files. Prefer subagents for broad exploration so raw file dumps stay out of the main context.

## The team (subagents in `.claude/agents/` — 19 roles, each a 20-year veteran of its craft; pipeline in `/new-feature`)

**Core delivery:** `product-owner` (spec, acceptance criteria, glossary) · `tech-lead` (plan, arbitration, merge gate) · `architect` (module layout, ADRs) · `ux-designer` (flows, states, tokens, a11y) · `backend-engineer` · `frontend-engineer` · `integration-engineer` (seam contract, e2e) · `database-engineer` · `qa-engineer` (strategy, denial tests, exploratory).
**Gates at checkpoints:** `code-reviewer` (verdict on every diff) · `security-engineer` (threat model, auth/input changes, release) · `devops-engineer` (containers, pipeline, deploy) · `sre-observability` (instrumentation, alerting) · `data-reporting-engineer` (aggregations, period boundaries) · `performance-engineer` (budgets, hot paths).
**Advisory on demand:** `tech-writer` (docs, runbooks) · `craft-editor` (de-AI-ifies all prose) · `localization-engineer` (i18n) · `privacy-compliance` (Rwanda Law 058/2021).
Design-skill lanes are routed inside each agent (see `.claude/skills/VENDORED.md`).

## Where are we? (read first, in this order)

1. `docs/ROADMAP.md` — the living tracker: phases, slices, statuses, the 📍NOW line. Update statuses in the same change that lands the work.
2. `docs/ARCHITECTURE-ESSENTIALS.md` — **under 150 lines, read it in full before writing code.** An index of the load-bearing decisions with verdicts. Every claim is marked BUILT or DESIGNED; **DESIGNED means no code exists — do not rely on it.**
3. `docs/PRD.md` — what we are building, for whom, the acceptance criteria, and what v1 explicitly does not do.
4. Then, only as needed: `docs/ARCHITECTURE.md` (the full picture, ~1200 lines), `docs/adr/` (eleven ADRs — **0008–0011 and every Amendment section correct earlier text**), `docs/superpowers/specs/` (five design documents), and `docs/superpowers/slice-1-workspace/LEDGER.md` (every ruling with its cost-if-wrong).

**A claim in a document is a hypothesis. Verify by execution.** Four controls in slice 1 were present, correctly named, documented, and never executed. A guard is unverified until you have watched it refuse something, and a grep is a presence check, not verification.

## Workflows (skills in `.claude/skills/`)

- `/new-feature` — spec → design → TDD → review → docs. The default way to build.
- `/bug-fix` — reproduce → failing test → fix → verify.
- `/production-readiness` — the ship checklist. Run before any merge to `main`.
- `/web-launch` — SEO/conversion/trust checklist for public-facing pages.
- `/adr` — record an architecture decision.
  Engineering rules live inside the owning agent (resilience in backend-engineer, security baseline in security-engineer, data rules in database-engineer, platform in devops-engineer, …) — they load only when that role runs.

## Design skills (vendored; lanes are scoped — see `.claude/skills/VENDORED.md`)

- `frontend-design` — aesthetic direction & structure (non-templated design intent).
- `taste` — visual identity & consistent design language.
- `ui-ux-pro-max` — layout, typography, palettes, UX patterns (database-backed).
- `animate` / `emil-design-eng` / `review-animations` / `improve-animations` — motion & polish.
- `/impeccable <command>` (plugin) — on-demand design audits.
- `task-observer` — watches sessions, proposes new skills.

## Plugins (declared in `.claude/settings.json`; installed per machine by setup.sh)

superpowers (methodology), impeccable (design audits), claude-mem (session memory),
security-guidance (real-time edit checks), claude-code-setup (automation recommendations).
Built-in `/code-review` and `/security-review` cover review — no extra plugins for those.

## Environment notes

- Primary dev environment: WSL Ubuntu 24.04 (`claude` CLI lives there).
- Browser automation: vendored `playwright-cli` skill — CLI over MCP for token cost (ADR 0003).
- Headroom (token-compression proxy) is installed per-machine by `scripts/setup.sh`; see `docs/SETUP.md`.
- Machine-local overrides go in `.claude/settings.local.json` and `CLAUDE.local.md` (both gitignored) — never in the shared files.

## Stack (ADR 0006; versions measured 2026-09-03)

- **Python 3.14.7 · Django 6.1 · PostgreSQL 18.** `uuid.uuid7()` and virtual `GeneratedField` are both available and confirmed.
- **The database has three roles, and this is load-bearing.** `raporo_app` serves requests (owns nothing, no `TRUNCATE`, no write on the audit trail or on `django_migrations`); `raporo_owner` runs `migrate` via the `migrator` alias; `raporo_backup` is the `pg_dump` identity (`pg_dump` as `raporo_app` fails). Identity is chosen by **connection alias**, never by a mutable setting. `dbshell` connects as `raporo_app` deliberately — a plan taken as the owner is not the plan production runs.
- **`.env` needs three secrets agents cannot write** (`Read(./.env*)` is denied): `RAPORO_APP_PASSWORD`, `RAPORO_MIGRATE_PASSWORD`, `RAPORO_BACKUP_PASSWORD`. If compose refuses to interpolate, that is why. On a database volume that predates the role split: `docker compose exec db /docker-entrypoint-initdb.d/10-raporo-roles.sh` then `docker compose up -d --wait`.
- **Never edit a `_V1` SQL constant** — add `_V2` plus a new migration. `tests/test_db_stability.py` hashes every `RunSQL` statement in every migration, so a guard migration cannot be committed green.
- Backend: Python, **Django 6.1 (non-negotiable)**. Use 6.1's new features deliberately; every package must support it; otherwise latest stable. DRF only when a real API consumer (mobile/integration) exists — see ADR 0007.
- Data: PostgreSQL. Redis + Celery/beat only once a real async/scheduled need exists.
- Frontend: **Django templates + HTMX** (ADR 0007 — supersedes the earlier React choice; React/DRF-SPA references anywhere are stale). Server-rendered pages, HTMX fragment swaps, no JS framework, no Node build. Business logic lives in a **service layer** (views thin) so DRF endpoints can be added when a mobile app/API consumer becomes real. Explain anything new to Elvis in plain language.
- Everything dockerized: docker compose for dev; prod images per devops-engineer standards.
- The product is period-based sales reporting (full brief: `docs/PRODUCT.md`): period boundaries and timezones are correctness-critical everywhere (data-reporting-engineer gates them).
- Rwanda-first: base currency Rwf, languages EN/Kinyarwanda/FR, privacy law = Rwanda Law No. 058/2021.
