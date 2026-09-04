# Raporo — Living Roadmap & Tracker

> **The single source of "where are we".** Read this first in any session; update it whenever work lands.
> Statuses: ✅ done · 🔄 in progress · ⏳ pending · ⛔ blocked (say by what).
> Rules: never delete rows — flip statuses and date them. One line of note per flip. Details live in the linked docs, not here.

**📍 NOW:** Slice 1 — the **tenancy-hardening round has landed** (2026-09-04) and is **through four gates**: `security-engineer` APPROVE WITH NITS ("security says merge") · `database-engineer` APPROVE WITH NITS · `code-reviewer` **REQUEST CHANGES** (two items, both small) · `qa-engineer` pending. Three parallel tracks: the `org` column + composite FK + `ScopePin`; the orgs domain (one-org-per-user, `store.access_all`, `permitted_stores()`, the first service layer, the denial matrix); and the period engine. A consolidated fix round across all gate findings is next.

**1538 tests pass.** Verified by execution 2026-09-04: ruff clean, `manage.py check` silent, no migration drift under either settings module, Python 3.14.7 / Django 6.1 / PostgreSQL 18.0006. Backup and restore rehearsed for the first time — `pg_dump` as `raporo_backup` and `pg_restore` into a fresh database both clean, and the restored append-only trigger was **watched refusing** an UPDATE and a DELETE.

**Done since the merge gate:** phone canonicalisation · `public_id` (UUIDv7) on seven tables · the three-role database split (privilege boundary watched refusing nine statements) · Python 3.14 + PostgreSQL 18 · the `privacy-compliance` ruling on Law 058/2021 (delivered — **no longer gates Task 4**) · **the document layer** (`387ae62`): [PRD.md](PRD.md), [ARCHITECTURE.md](ARCHITECTURE.md), [ARCHITECTURE-ESSENTIALS.md](ARCHITECTURE-ESSENTIALS.md), `AGENTS.md`.

**Read [ARCHITECTURE-ESSENTIALS.md](ARCHITECTURE-ESSENTIALS.md) in full before writing code** — 141 lines, and every claim is marked BUILT or DESIGNED. **DESIGNED means no code exists.**

⛔ **Still open:** row-level security is **entirely absent** (`pg_policies` → 0 rows, measured) — `9a697c3`'s claim of "RLS scaffolding" is false, and the `orgs_membership` bootstrap must be closed before policies ship or login is a total lockout. The 14 add/add conflicts with `origin/dev` are unresolved (merge-base is the initial commit; all docs/config, no source). **No CI exists.** The application schema has **zero CHECK constraints**. Tasks 5–14 (auth backend + throttle, 2FA, invites, i18n, password reset, error pages, CI) are untouched.

---

## Phase 0 — Team & tooling foundation ✅ (2026-08-31)

- ✅ 19-agent senior team + 5-phase pipeline (`.claude/agents/`, ADR 0004)
- ✅ Rules distributed into owning agents; gate skills `/production-readiness`, `/web-launch` (ADR 0005)
- ✅ Vendored skills + plugins + Playwright CLI (ADR 0002, 0003) · Headroom wired · setup.sh green
- ✅ Portable repo setup (CLAUDE.md, settings, bootstrap — ADR 0001)

## Phase A — Product understanding ✅ (2026-08-31 → 09-01)

- ✅ Stack decided: Django 6.1, Postgres, Docker (ADR 0006); frontend switched to **Django templates + HTMX** (ADR 0007, replaces React)
- ✅ Product brief: [PRODUCT.md](PRODUCT.md) (all decisions) · narrative: [PROJECT-DESCRIPTION.md](PROJECT-DESCRIPTION.md)
- ✅ Sample reports decoded (orders/deposits, credit, stock ledger, igishoro/inyungu)
- ✅ Brainstorm rounds closed: stores 1–5, investors + cycles + splits, write-offs, soft-delete + audit, invariant #1 (org/store isolation), i18n switcher, alerts, auth hardening + 2FA + invite links, future-API docs rule
- ✅ Biweekly confirmed by Elvis (2026-09-01): 1–15 and 16–end of month

## Phase B — Architecture & design 🔄

- ✅ **Step 1: architecture + project structure** — approved by Elvis 2026-09-01 (modular monolith at repo root, ledger/movements, session auth + hardening, app layout).
- ✅ **Step 2: database schema** — approved by Elvis 2026-09-01: D1 store-scoped products, D2 store-scoped customers, D3 cycle-owned variants; plus mandatory exchange-rate rule for any foreign-currency money (UI blocks without it; converted base amount always stored).
- ✅ **Step 3: written spec** — accepted 2026-09-01. Slice-1 plan: `docs/superpowers/plans/2026-09-01-slice-1-foundation.md` (14 tasks — password reset included as Task 9b after Elvis's email-required decision).
- ✅ **Step 4: tenancy-hardening design** — accepted 2026-09-02. Five specs in `docs/superpowers/specs/` (~5,400 lines) + ADRs [0008](adr/0008-denormalised-organization-on-store-scoped-rows.md)–[0011](adr/0011-org-wide-store-access-is-a-permission-code.md). **ADR 0008–0011 and every Amendment section correct earlier text.**
- ✅ **Step 5: document layer** — approved by Elvis 2026-09-03, committed `387ae62`. [PRD.md](PRD.md) (product, acceptance criteria, what v1 does not do) · [ARCHITECTURE.md](ARCHITECTURE.md) (the full picture + the intended tree) · [ARCHITECTURE-ESSENTIALS.md](ARCHITECTURE-ESSENTIALS.md) (141 lines, read in full before coding) · `AGENTS.md` (a pointer, not a second source of truth).
- ⏳ ADR: report rendering tech (HTML→PDF/image) — decided during slice 4 design.
- ⏳ ADR: audit-log destruction at end of retention — `database-engineer` drafting. Range partitioning **rejected** on eight measurements; the replacement is a versioned append-only `_V2` permitting DELETE only past an eleven-year floor. See LEDGER Session 7.

## Phase C — Build (six slices; each runs the full `/new-feature` pipeline: define → design → build+gates → harden → ship)

| #   | Slice               | Scope anchor                                                                                                                                                                             | Status                                                                                                                                                 |
| --- | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | Foundation          | accounts (username/email/phone), max-security auth, 2FA-ready login flow, invite links, orgs, stores (1–5), custom RBAC, audit/soft-delete core, i18n + header switcher, Docker skeleton | 🔄 **in progress.** Task 0 ✅ · Tasks 1+2+3 ✅ merged (4 fix rounds, 8 gate reviews, tech-lead MERGE WITH FOLLOW-UPS) · phone canonicalisation ✅ · `public_id` ✅ · DB role split ✅ · platform bump ✅ · document layer ✅. **Tenancy-hardening round:** `org` column ✅ · `ScopePin` ✅ · one-org-per-user ✅ · `store.access_all` + `permitted_stores()` ✅ · E007/E101 ✅ · **RLS ⏳** · org-leading indexes ⏳ · connections/timeouts ⏳. **Task 4 (services) ✅** · denial matrix ✅ · period engine ✅. Tasks 5–14 ⏳ |
| 2   | Products & stock    | per-store stock, variants/packs, restocks (cost + optional expiry), floor=cost rule, reference/latest prices, write-offs with reasons                                                    | ⏳                                                                                                                                                     |
| 3   | Selling & owing     | sales (negotiated ≥ floor), orders + deposits + ✅ lifecycle, customers, credit book ("who owes us"), payments in/out                                                                    | ⏳                                                                                                                                                     |
| 4   | The Report          | period engine (org TZ, biweekly 1–15/16–end), per-store + consolidated org reports, branded, WhatsApp-shareable image + PDF                                                              | ⏳                                                                                                                                                     |
| 5   | Money intelligence  | expenses, investors (capital accounts, optional user link), cycles (co-investor % shares, profit splits, payouts), detail-page analytics                                                 | ⏳                                                                                                                                                     |
| 6   | Alerts & automation | low-stock + expiry alerts, scheduled report sending (Celery beat arrives), org branding settings                                                                                         | ⏳                                                                                                                                                     |

### Cross-slice decisions taken mid-build (must not be lost)
- ✅ **Branding chain (2026-09-01):** store → org → Raporo default; per-store `use_own_branding` toggle (empty = inherit); store name always local; logo/colors/typography inherit; consolidated report uses org branding. Recorded in PRODUCT.md + design spec §4-orgs.
  - ✅ **Slice 1 (2026-09-01):** `Store.brand` (JSONB, default `{}`) + `Store.use_own_branding` (bool, default False) landed at `apps/orgs/models.py`, folded into `orgs/0001_initial` while it was still uncommitted — no extra migration spent.
  - ⏳ **Slice 4:** implement `resolve_branding(store)` and use it in every report/share-card/PDF path.
  - ⏳ **Slice 6:** settings UI for editing org and store branding, including the toggle.

## Phase D — Launch ⏳

- ⏳ `/production-readiness` full pass · `/web-launch` for public pages · deploy (Dokploy/Coolify VPS, **hosted in Rwanda** per Elvis 2026-09-03)
- ⛔ **Launch blockers from the privacy ruling, none of them code, and the clock is ~6–10 weeks.** Register the company at RDB **first** so every artefact names the company, not Elvis · then NCSA registration as data controller **and** processor (free, 30-day statutory issuance; the 2021 law's transition expired 15 Oct 2023, so there is no grace period) · a named DPO · a DPA annex in the Terms, plus a mechanism proving which version each organization accepted · disclose in the privacy notice that the audit trail retains trading names for the full retention period.
- ⏳ Retention: erase identifiers 30 days after closure; financial records and the trail for **ten years from 1 January following the fiscal year** (a 2026 record survives to 31 Dec 2036), stated in the Terms as the organization's own instruction. Needs a scheduled runner — there is no Celery yet, and this is the first real need for one.
- ⏳ Email is Gmail (Elvis 2026-09-03). **Google Workspace recommended over consumer Gmail**: a consumer account carries no data-processing agreement, and Raporo is a processor for its customers. Sending caps (~500/day consumer, ~2,000 Workspace) are shared by reset, invites and slice-6 scheduled delivery.

## Later / explicitly deferred

Social login · SMS OTP · USD base option · DRF API + OpenAPI docs (when mobile/integration is real — service layer keeps it cheap) · POS/e-commerce integrations · >5 stores · full offline entry · global launch

## Standing notes

- **Dev environment (verified 2026-09-01):** `cp .env.example .env` → `docker compose build` → `docker compose up --wait` reaches a migrated, healthy app; `/healthz` returns 200. Full guide: [DEVELOPMENT.md](DEVELOPMENT.md). The container entrypoint runs `manage.py check` before boot for every command it does not recognise as tooling, and migrates only when `RAPORO_AUTO_MIGRATE=1` **and** the command is a positively-identified server (dev only). `pytest` is never pre-booted, whatever `RAPORO_ROLE` says.
- **`.env` needs four things agents cannot write** (`Read(./.env*)` is denied, so Elvis adds them by hand): `DJANGO_MEDIA_ROOT=/var/lib/raporo/media` plus the three role passwords `RAPORO_APP_PASSWORD`, `RAPORO_MIGRATE_PASSWORD`, `RAPORO_BACKUP_PASSWORD`. The three secrets are present in the local `.env`; `.env.example` still needs all four documented. Dev falls back to `/var/tmp/raporo-media`; prod requires it with no fallback.
- **On a database volume predating the role split**, the suite fails with `FATAL: password authentication failed for user "raporo_owner"`. Documented remedy, verified 2026-09-03: `docker compose exec db /docker-entrypoint-initdb.d/10-raporo-roles.sh` then `docker compose up -d --wait`.
- **Elvis's rulings, 2026-09-03:** a row's period comes from a user-settable `business_date`, not the recording instant · **one timezone per organization, enforced** — `Store.timezone` is not added · production hosts **in Rwanda** · the report carries `Sales` and `Received` as two numbers, never added. Consequences in [LEDGER](superpowers/slice-1-workspace/LEDGER.md). Production hostname not yet decided; email provider is Gmail (Workspace recommended, for the DPA).
- Commits/merges are **human actions** (settings deny agent git writes) — when a step lands, Elvis commits, then flip the status here in the same change.
- Every slice's gates (code-reviewer, security-engineer, data-reporting-engineer where relevant, tech-lead merge) must produce output — no silent skips.
