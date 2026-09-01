# Setting up on a new machine

This page covers the **AI-team** setup: Claude Code, the agents and skills, plugins, and Headroom.
Running and testing the application itself is [DEVELOPMENT.md](DEVELOPMENT.md).

Everything portable lives in git. Only two things are machine-level and installed by the bootstrap script: the Claude Code CLI and Headroom.

## Fresh machine, two commands

```bash
git clone <repo-url> && cd raporo
./scripts/setup.sh
```

Then just run `claude` in the project. Verify anytime with `./scripts/setup.sh --check` or `headroom doctor`.

## What travels with the repo (nothing to reinstall)

| Piece | Location | Loaded by Claude Code |
| --- | --- | --- |
| Rules & principles | `CLAUDE.md` | every session, automatically |
| Team (subagents) | `.claude/agents/*.md` | automatically |
| Workflows (skills) | `.claude/skills/*/SKILL.md` | as `/new-feature`, `/bug-fix`, `/production-readiness`, `/adr` |
| Shared settings & permissions | `.claude/settings.json` | automatically |
| Vendored design/meta skills | `.claude/skills/` (see `VENDORED.md`) | automatically |
| MCP servers (Figma) | `.mcp.json` | automatically (approve on first use) |
| Browser automation skill | `.claude/skills/playwright-cli/` | automatically (binary installed by `setup.sh`) |
| Plugin declarations | `.claude/settings.json` (`extraKnownMarketplaces`, `enabledPlugins`) | marketplaces auto-register; binaries installed by `setup.sh` |
| Bootstrap | `scripts/setup.sh` | run manually once per machine |

Machine-local overrides go in `.claude/settings.local.json` and `CLAUDE.local.md` — both gitignored, never committed.

## Headroom (token compression)

Headroom is a local proxy that compresses tool outputs, file contents, and history before they reach the Anthropic API (roughly 15–20% savings for coding agents per their benchmarks), while preserving prompt-cache prefixes. It's a Python CLI, so it lives on the machine, not in the repo — `scripts/setup.sh` installs it via `uv` and wires it into the project with `headroom init claude`.

How the wiring works (all machine-local, none of it committed):

- `headroom init claude` writes `.claude/settings.local.json` (gitignored): sets `ANTHROPIC_BASE_URL` to the local proxy **for this project only**, and installs SessionStart/PreToolUse hooks that auto-start the proxy — so a reboot can never leave `claude` pointing at a dead proxy.
- `claude` outside this project is untouched; routing is project-scoped.
- `headroom doctor` — health check + lifetime tokens saved; `headroom dashboard` — savings dashboard.
- To disable on a machine: delete `.claude/settings.local.json` (or the headroom entries in it).

## Plugins

Declared project-scoped in `.claude/settings.json`; `scripts/setup.sh` installs the binaries per machine (plugin installs are user-scoped by design in Claude Code):

- **superpowers** — brainstorm → spec → plan → TDD methodology (`obra/superpowers`)
- **impeccable** — `/impeccable <command>` design audits (Paul Bakaus)
- **claude-mem** — automatic session memory across sessions (run `npx claude-mem install` once per machine to register its worker)
- **security-guidance** — Anthropic hook that pattern-checks every edit in real time
- **claude-code-setup** — Anthropic analyzer that recommends project automations

Deliberately NOT installed: code-review/security-review plugins (built-in `/code-review` and `/security-review` cover it) and MemPalace (conflicts with claude-mem — one memory system only).

## Browser automation (Playwright CLI)

We use the Playwright **CLI**, not the Playwright MCP server — Playwright's own recommendation for coding agents. The MCP server injects ~26 tool schemas (~3.6k tokens) into every session and streams page snapshots through the model; the CLI is invoked like git or npm and keeps snapshots/screenshots on disk (Microsoft benchmarks ≈4× fewer tokens). See ADR 0003.

- Agent skill vendored at `.claude/skills/playwright-cli/` (travels with the repo; see `VENDORED.md`).
- Binary installed per machine by `setup.sh` (`npm install -g @playwright/cli`).
- Browsers download on first use; if prompted, run once: `npx playwright install chromium`.

## MCP servers

- **Figma** (official remote, `https://mcp.figma.com/mcp`) — authenticate once per machine via `/mcp` in Claude Code (OAuth; no token in the repo).

## Requirements

- Linux, WSL2 (our primary: Ubuntu 24.04), or macOS
- Python ≥ 3.10 available (uv fetches 3.13 for Headroom automatically)
- `curl`, `git`

## Reusing this setup for a new project

This repo doubles as a starter template. For a new project, copy: `CLAUDE.md`, `.claude/`, `.mcp.json`, `scripts/setup.sh`, `docs/adr/0001-*.md`, and this file — then edit CLAUDE.md's project-specific sections. (Or make this repo a GitHub template and click "Use this template".)
