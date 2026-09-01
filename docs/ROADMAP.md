# Raporo — Living Roadmap & Tracker

> **The single source of "where are we".** Read this first in any session; update it whenever work lands.
> Statuses: ✅ done · 🔄 in progress · ⏳ pending · ⛔ blocked (say by what).
> Rules: never delete rows — flip statuses and date them. One line of note per flip. Details live in the linked docs, not here.

**📍 NOW:** Slice 1 build ready to start (2026-09-01): plan has 14 tasks, execution mode = subagent-driven (default per tech-lead recommendation). Waiting on Elvis's commit+push of Phase A/B docs, then Task 0 begins.

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
- ⏳ ADR: report rendering tech (HTML→PDF/image) — decided during slice 4 design.

## Phase C — Build (six slices; each runs the full `/new-feature` pipeline: define → design → build+gates → harden → ship)

| #   | Slice               | Scope anchor                                                                                                                                                                             | Status                                                                                                                                                 |
| --- | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | Foundation          | accounts (username/email/phone), max-security auth, 2FA-ready login flow, invite links, orgs, stores (1–5), custom RBAC, audit/soft-delete core, i18n + header switcher, Docker skeleton | ⏳ **plan ready** — `docs/superpowers/plans/2026-09-01-slice-1-foundation.md` (14 tasks incl. emailed password reset; email required for all accounts) |
| 2   | Products & stock    | per-store stock, variants/packs, restocks (cost + optional expiry), floor=cost rule, reference/latest prices, write-offs with reasons                                                    | ⏳                                                                                                                                                     |
| 3   | Selling & owing     | sales (negotiated ≥ floor), orders + deposits + ✅ lifecycle, customers, credit book ("who owes us"), payments in/out                                                                    | ⏳                                                                                                                                                     |
| 4   | The Report          | period engine (org TZ, biweekly 1–15/16–end), per-store + consolidated org reports, branded, WhatsApp-shareable image + PDF                                                              | ⏳                                                                                                                                                     |
| 5   | Money intelligence  | expenses, investors (capital accounts, optional user link), cycles (co-investor % shares, profit splits, payouts), detail-page analytics                                                 | ⏳                                                                                                                                                     |
| 6   | Alerts & automation | low-stock + expiry alerts, scheduled report sending (Celery beat arrives), org branding settings                                                                                         | ⏳                                                                                                                                                     |

### Cross-slice decisions taken mid-build (must not be lost)
- ✅ **Branding chain (2026-09-01):** store → org → Raporo default; per-store `use_own_branding` toggle (empty = inherit); store name always local; logo/colors/typography inherit; consolidated report uses org branding. Recorded in PRODUCT.md + design spec §4-orgs.
  - ⏳ **Slice 1 (now):** add `Store.brand` (JSONB, default `{}`) + `Store.use_own_branding` (bool, default False) — folded into the *uncommitted* `orgs/0001_initial` so no extra migration is spent.
  - ⏳ **Slice 4:** implement `resolve_branding(store)` and use it in every report/share-card/PDF path.
  - ⏳ **Slice 6:** settings UI for editing org and store branding, including the toggle.

## Phase D — Launch ⏳

- ⏳ `/production-readiness` full pass · `/web-launch` for public pages · deploy (Dokploy/Coolify VPS) · Rwanda Law 058/2021 privacy pass

## Later / explicitly deferred

Social login · SMS OTP · USD base option · DRF API + OpenAPI docs (when mobile/integration is real — service layer keeps it cheap) · POS/e-commerce integrations · >5 stores · full offline entry · global launch

## Standing notes

- Commits/merges are **human actions** (settings deny agent git writes) — when a step lands, Elvis commits, then flip the status here in the same change.
- Every slice's gates (code-reviewer, security-engineer, data-reporting-engineer where relevant, tech-lead merge) must produce output — no silent skips.
