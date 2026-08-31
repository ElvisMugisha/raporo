# Setting up on a new machine

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
| MCP servers (when we add any) | `.mcp.json` | automatically (approve on first use) |
| Bootstrap | `scripts/setup.sh` | run manually once per machine |

Machine-local overrides go in `.claude/settings.local.json` and `CLAUDE.local.md` — both gitignored, never committed.

## Headroom (token compression)

Headroom is a local proxy that compresses tool outputs, file contents, and history before they reach the Anthropic API (roughly 15–20% savings for coding agents per their benchmarks), while preserving prompt-cache prefixes. It's a Python CLI, so it lives on the machine, not in the repo — `scripts/setup.sh` installs it via `uv` and wires it into the project with `headroom init claude`.

How the wiring works (all machine-local, none of it committed):
- `headroom init claude` writes `.claude/settings.local.json` (gitignored): sets `ANTHROPIC_BASE_URL` to the local proxy **for this project only**, and installs SessionStart/PreToolUse hooks that auto-start the proxy — so a reboot can never leave `claude` pointing at a dead proxy.
- `claude` outside this project is untouched; routing is project-scoped.
- `headroom doctor` — health check + lifetime tokens saved; `headroom dashboard` — savings dashboard.
- To disable on a machine: delete `.claude/settings.local.json` (or the headroom entries in it).

## Requirements

- Linux, WSL2 (our primary: Ubuntu 24.04), or macOS
- Python ≥ 3.10 available (uv fetches 3.13 for Headroom automatically)
- `curl`, `git`

## Reusing this setup for a new project

This repo doubles as a starter template. For a new project, copy: `CLAUDE.md`, `.claude/`, `.mcp.json`, `scripts/setup.sh`, `docs/adr/0001-*.md`, and this file — then edit CLAUDE.md's project-specific sections. (Or make this repo a GitHub template and click "Use this template".)
