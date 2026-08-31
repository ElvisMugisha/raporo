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
**Core delivery:** `product-owner` (spec, acceptance criteria, glossary) · `tech-lead` (plan, arbitration, merge gate) · `architect` (module layout, ADRs) · `ux-designer` (flows, states, tokens, a11y) · `backend-engineer` · `frontend-engineer` · `integration-engineer` (API contract, e2e) · `database-engineer` · `qa-engineer` (strategy, denial tests, exploratory).
**Gates at checkpoints:** `code-reviewer` (verdict on every diff) · `security-engineer` (threat model, auth/input changes, release) · `devops-engineer` (containers, pipeline, deploy) · `sre-observability` (instrumentation, alerting) · `data-reporting-engineer` (aggregations, period boundaries) · `performance-engineer` (budgets, hot paths).
**Advisory on demand:** `tech-writer` (docs, runbooks) · `craft-editor` (de-AI-ifies all prose) · `localization-engineer` (i18n) · `privacy-compliance` (GDPR).
Design-skill lanes are routed inside each agent (see `.claude/skills/VENDORED.md`).

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

## Stack
Not chosen yet. When it is: record it in an ADR, then add stack-specific rules to this section and stack-specific agents/skills as needed.
