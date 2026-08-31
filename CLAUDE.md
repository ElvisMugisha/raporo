# Raporo

## What this repo is
Product code plus a fully portable AI-team setup. Everything the team needs — agents, skills, rules, settings, MCP config, bootstrap — is committed here. Clone on any machine, run `scripts/setup.sh`, and the whole team works identically.

## Non-negotiable principles
1. **Production-grade only.** No placeholder code, no TODO-and-move-on, no "works on my machine". Every change ships with tests and passes them.
2. **Small, reviewed steps.** One logical change per commit. Feature branches off `dev`; `main` is always releasable.
3. **Security by default.** Never commit secrets. Validate all external input. Least privilege everywhere.
4. **Decisions are written down.** Anything architectural gets an ADR in `docs/adr/` (use the `adr` skill).
5. **Token discipline.** Keep this file and skills lean. Search before reading whole files. Prefer subagents for broad exploration so raw file dumps stay out of the main context.

## The team (subagents in `.claude/agents/`)
- `architect` — system design, trade-offs, ADRs. Use before building anything non-trivial.
- `code-reviewer` — strict review of diffs before merge.
- `security-auditor` — security review of changes and dependencies.
- `test-engineer` — test strategy, writing and hardening tests.
- `devops-engineer` — CI/CD, Docker, environments, releases.
- `docs-writer` — README, ADR polish, changelogs, user docs.

## Workflows (skills in `.claude/skills/`)
- `/new-feature` — spec → design → TDD → review → docs. The default way to build.
- `/bug-fix` — reproduce → failing test → fix → verify.
- `/production-readiness` — the ship checklist. Run before any merge to `main`.
- `/adr` — record an architecture decision.

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
- Headroom (token-compression proxy) is installed per-machine by `scripts/setup.sh`; see `docs/SETUP.md`.
- Machine-local overrides go in `.claude/settings.local.json` and `CLAUDE.local.md` (both gitignored) — never in the shared files.

## Stack
Not chosen yet. When it is: record it in an ADR, then add stack-specific rules to this section and stack-specific agents/skills as needed.
