# 0001. AI team, skills, and rules live in the repo; machine tools via bootstrap script

Date: 2026-08-31
Status: Accepted

## Context
We build with Claude Code as a team of AI agents and want identical capability on every machine: clone the repo anywhere and the full team, workflows, rules, and token-saving setup work immediately. Claude Code supports project-scoped configuration (`.claude/`, `CLAUDE.md`, `.mcp.json`) that travels with git, but some tooling — the Claude CLI itself and Headroom (a local token-compression proxy, a Python application) — is inherently machine-level and cannot live in a repository. Without a deliberate structure, agents and skills drift into user-scoped config (`~/.claude/`) and become invisible to other machines and teammates.

## Decision
We will keep every piece of AI-team configuration project-scoped and committed: agents in `.claude/agents/`, skills in `.claude/skills/`, shared settings in `.claude/settings.json`, MCP servers in `.mcp.json`, and rules in `CLAUDE.md`. Nothing team-relevant goes into user scope. Machine-level tools are installed by the idempotent, committed `scripts/setup.sh`, so a fresh machine needs exactly one command after cloning. Headroom is adopted as the token-compression layer, wrapped with `--code-memory none` so it adds no user-scoped state.

Rejected alternatives: user-scoped config with a dotfiles repo (breaks per-project variation, invisible to collaborators); a Claude Code plugin/marketplace for our team (heavier to maintain, and marketplaces still need per-machine trust — revisit if we run many repos off this template); committing Headroom itself (impossible — it's a machine-level proxy).

## Consequences
Easier: onboarding a machine or collaborator (clone + one script); evolving the team in code review like any other change; reusing this repo as a template for future projects. Harder: machine-level tool versions can still drift between machines (mitigated by `setup.sh --check`); the Headroom proxy must be running for `claude` to work once wrapped (documented in SETUP.md). We are committed to reviewing changes to `.claude/` with the same rigor as production code. Revisit if Claude Code plugins become the better distribution mechanism across many repos.
