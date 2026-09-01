# 0002. Tooling selection: memory, routing, design skills, review plugins

Date: 2026-08-31
Status: Accepted

## Context
We evaluated a candidate list of plugins/skills for the AI team: OmniRoute, claude-mem, MemPalace, claude-code-setup, task-observer, Superpowers, Impeccable, Emil Kowalski's skills, taste-skill, ui-ux-pro-max, Anthropic frontend-design, Figma MCP, Playwright MCP, and standalone code-review/security-review plugins. Several candidates overlap: claude-mem and MemPalace are competing memory systems that both hook every session; the four design skills all inject UI rules and can contradict each other; OmniRoute occupies the same `ANTHROPIC_BASE_URL` slot as Headroom; review plugins duplicate built-in `/code-review` and `/security-review`. Every always-on plugin also adds per-session context cost, which works against our Headroom token-saving goal.

## Decision
We will run **claude-mem** as the single memory system (hands-off session capture; most mature Claude Code integration) and not MemPalace. We **skip OmniRoute** — Headroom alone covers compression, we don't currently need multi-provider routing, and chaining it risks bypassing subscription auth; revisit if multi-provider becomes real (chain it BEHIND Headroom via `ANTHROPIC_TARGET_API_URL`, never in place of it). We **vendor** all portable skills into `.claude/skills/` (frontend-design, taste, ui-ux-pro-max, four Emil Kowalski skills, task-observer) with descriptions edited to scope each design skill to one lane (see `.claude/skills/VENDORED.md`). We install as **plugins** only what can't be vendored: superpowers, impeccable, claude-mem, security-guidance, claude-code-setup. We skip standalone code-review/security-review plugins in favor of the built-ins.

## Consequences
Easier: one memory store, no contradictory design rules, full clone-portability of skills, lower per-session token overhead than installing everything as plugins. Harder: vendored skills need manual upstream syncs (tracked by commit SHA in VENDORED.md); plugins still need one `setup.sh` run per machine; claude-mem stores data user-scoped (`~/.claude-mem/`), so memories don't travel with the repo. Triggers to revisit: needing multi-provider routing or free-tier pooling (→ OmniRoute behind Headroom), claude-mem recall proving too lossy (→ MemPalace), or a design skill's upstream diverging significantly.
