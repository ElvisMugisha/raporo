# 0005. Engineering rules live inside the owning agent, checklists become gate skills

Date: 2026-08-31
Status: Accepted

## Context

We collected a large catalog of production rules to enforce: code quality (clean/DRY/SoC), resilience (timeouts, retries, idempotency, races, queues), security (the full never-fall-below list from auth to headers), data (indexing, N+1, pooling, backups), platform (IaC, semver, feature flags, DR, CDN), observability (incidents, trend alerts), and a web-launch list (SEO, conversion, trust, error pages). Dumping all of it into CLAUDE.md would load thousands of tokens into every session — against the token-discipline principle — and rules far from their enforcement point get ignored. We also evaluated three more skills: stop-slop (de-AI-ifier), find-skills (auto-installs skills), and UI/UX pattern cheat sheets.

## Decision

Rules are placed where they are enforced, and load only then: domain rules go inside the owning agent file (resilience/scale in backend-engineer, security baseline in security-engineer, data rules in database-engineer, platform in devops-engineer, incident discipline in sre-observability, caching/latency in performance-engineer, contract terms in integration-engineer, XSS/404/SEO hooks in frontend-engineer, DRY/SoC in code-reviewer, simplicity/hierarchy/conversion in ux-designer). Checklist-shaped rules become blocking gate skills: the new `/web-launch` (SEO, conversion, trust, hygiene for public pages) joins `/production-readiness`, which now requires it for indexable pages. CLAUDE.md gains only a one-line pointer. Rejected: a monolithic RULES.md loaded every session (token cost, no enforcement point); stop-slop (craft-editor owns the prose lane — two rule sources would conflict); find-skills (auto-installing third-party instructions defeats our evaluate-and-pin curation and is a supply-chain risk); UI-pattern cheat-sheet skills (subset of the vendored ui-ux-pro-max databases).

## Consequences

Easier: each role carries its own non-negotiables, so rules arrive exactly when relevant and review of a rule change is review of one role; per-session context cost stays flat. Harder: cross-cutting rules touching several roles must be added in each (accepted — the sibling check in task-observer's log format covers this); nobody sees "all rules" in one place (the agent files are the canonical set; grep `.claude/agents/`). Revisit if the stack decision (pending ADR) brings stack-specific rules — same placement principle applies — or if a rules audit finds agents drifting from their own standards.
