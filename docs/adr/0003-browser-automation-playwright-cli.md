# 0003. Browser automation via Playwright CLI, not Playwright MCP

Date: 2026-08-31
Status: Accepted

## Context
ADR 0002's change set added the Playwright MCP server to `.mcp.json` for browser automation (testing, screenshots, UI verification). Since then, Playwright's own guidance for coding agents shifted: the MCP server registers ~26 tool schemas (~3.6k tokens) into every session and streams accessibility snapshots through the model context, while the CLI (`@playwright/cli`, shipped with Playwright 1.58) is invoked like any shell command and writes snapshots/screenshots to disk — Microsoft benchmarks ≈4× fewer tokens for the same tasks. Per-session token cost works directly against our token-discipline principle (CLAUDE.md #5) and the Headroom investment. We also evaluated two more candidates in this round: voltagent/awesome-design-md (73 brand DESIGN.md files, MIT) and img2threejs (29 MB image-to-Three.js skill, Apache-2.0).

## Decision
We use the **Playwright CLI** for browser automation and drop the Playwright MCP server from `.mcp.json`. The agent skill from microsoft/playwright-cli is vendored at `.claude/skills/playwright-cli/` (tracked in `VENDORED.md`), so usage guidance travels with the repo; `scripts/setup.sh` installs the `@playwright/cli` binary per machine. Rejected: keeping Playwright MCP (per-session schema cost, snapshots inflate context; MCP only wins in shell-less sandboxes, which is not our environment); vendoring awesome-design-md (it is a reference catalog, not a skill — when Raporo's visual identity is defined we copy the one DESIGN.md we need, not 73); vendoring img2threejs (29 MB for a 3D capability the product has no stated need for; it stays user-scoped on machines that want it — revisit if the stack includes 3D).

## Consequences
Easier: browser automation with near-zero context overhead until actually used; skill portability via clone; one less MCP server to approve. Harder: one more per-machine binary (mitigated by `setup.sh --check`); first browser download (~120 MB) happens on demand; the vendored skill needs manual upstream syncs like the rest of `VENDORED.md`. Triggers to revisit: an agent environment without shell access (→ Playwright MCP there), Raporo needing 3D (→ img2threejs), visual identity work starting (→ pull one DESIGN.md from awesome-design-md as reference).
