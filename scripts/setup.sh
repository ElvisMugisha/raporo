#!/usr/bin/env bash
# Bootstrap this project on a fresh machine (Linux / WSL / macOS).
# Idempotent: safe to re-run any time.
#
#   ./scripts/setup.sh            # install everything
#   ./scripts/setup.sh --check    # verify only, install nothing
set -euo pipefail

CHECK_ONLY=false
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=true

ok()   { printf '  \033[32m[ok]\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m[!!]\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m[XX]\033[0m %s\n' "$1"; FAILED=true; }
FAILED=false

export PATH="$HOME/.local/bin:$PATH"

echo "== Raporo bootstrap =="

# --- 1. Claude Code CLI -----------------------------------------------------
if command -v claude >/dev/null 2>&1; then
  ok "Claude Code $(claude --version 2>/dev/null | head -1)"
else
  if $CHECK_ONLY; then fail "Claude Code missing"; else
    warn "Claude Code missing — installing"
    curl -fsSL https://claude.ai/install.sh | bash
    ok "Claude Code installed (run 'claude' once to log in)"
  fi
fi

# --- 2. uv (installs and isolates Headroom) ---------------------------------
if command -v uv >/dev/null 2>&1; then
  ok "uv $(uv --version | head -1)"
else
  if $CHECK_ONLY; then fail "uv missing"; else
    warn "uv missing — installing"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ok "uv installed"
  fi
fi

# --- 3. Headroom (token-compression proxy) ----------------------------------
if command -v headroom >/dev/null 2>&1; then
  ok "Headroom $(headroom --version 2>/dev/null | head -1)"
else
  if $CHECK_ONLY; then fail "Headroom missing"; else
    warn "Headroom missing — installing"
    # A half-written install leaves uv reporting "Ignoring malformed tool
    # headroom-ai" and no `headroom` on PATH, which is silent: the proxy simply
    # never compresses anything. Measured on this machine 2026-09-04. Clear it
    # first so a re-run repairs rather than skips.
    uv tool uninstall headroom-ai >/dev/null 2>&1 || true
    uv tool install --python 3.14 "headroom-ai[all]" || \
      uv tool install "headroom-ai[all]" || warn "headroom install failed — run manually"
    command -v headroom >/dev/null 2>&1 \
      && ok "Headroom installed" \
      || fail "Headroom still not on PATH — ensure ~/.local/bin is in PATH"
  fi
fi

# --- 4. Headroom durable integration for this project -----------------------
# Writes machine-local routing (.claude/settings.local.json, gitignored):
# ANTHROPIC_BASE_URL -> local proxy + hooks that auto-start the proxy.
if command -v headroom >/dev/null 2>&1; then
  if [[ -f .claude/settings.local.json ]] && grep -q "headroom" .claude/settings.local.json 2>/dev/null; then
    ok "Headroom already wired into this project"
  else
    if $CHECK_ONLY; then fail "Headroom not wired into this project (run without --check)"; else
      headroom init claude
      ok "Headroom wired (project-local routing + auto-start hooks)"
    fi
  fi
fi

# --- 5. Plugin marketplaces + plugins (user-scope install, project-scope enable)
# Marketplaces/enabled-plugins are declared in .claude/settings.json (committed);
# the binaries still need one install per machine — that's this step.
if command -v claude >/dev/null 2>&1; then
  if $CHECK_ONLY; then
    claude plugin list 2>/dev/null | grep -q superpowers && ok "plugins installed" || fail "plugins not installed (run without --check)"
  else
    for m in obra/superpowers-marketplace pbakaus/impeccable thedotmack/claude-mem anthropics/claude-code; do
      claude plugin marketplace add "$m" >/dev/null 2>&1 || true
    done
    # The marketplace suffix must be the one `claude plugin list` reports, not the
    # plugin's nickname. Measured 2026-09-04: `superpowers@superpowers-marketplace`
    # and `security-guidance@claude-code-plugins` matched no installed plugin, so
    # both stayed DISABLED for the life of the project while `install` reported
    # success. Verify any change here with `claude plugin list`, not by re-reading
    # this file.
    for p in superpowers@claude-plugins-official impeccable@impeccable claude-mem@thedotmack \
             security-guidance@claude-plugins-official claude-code-setup@claude-plugins-official; do
      claude plugin install "$p" >/dev/null 2>&1 && ok "plugin $p" || warn "plugin $p — install manually: claude plugin install $p"
      claude plugin enable "$p" >/dev/null 2>&1 || true
    done
    # Enabled != loaded. This is the assertion the old code was missing.
    for p in superpowers security-guidance impeccable claude-mem claude-code-setup; do
      claude plugin list 2>/dev/null | grep -A3 "$p@" | grep -q 'enabled' \
        && ok "plugin $p enabled" || warn "plugin $p present but NOT enabled — check the marketplace suffix in .claude/settings.json"
    done
    # claude-mem needs its worker + hooks registered (one-time; interactive on first run)
    if command -v bun >/dev/null 2>&1 || npx --yes claude-mem status >/dev/null 2>&1; then
      ok "claude-mem runtime present"
    else
      warn "claude-mem worker not initialized — run once: npx claude-mem install"
    fi
  fi
fi

# --- 6. Playwright CLI (browser automation for the agents) ------------------
# CLI over MCP: no per-request tool schemas, snapshots stay on disk (ADR 0003).
if command -v playwright-cli >/dev/null 2>&1; then
  ok "Playwright CLI $(playwright-cli --version 2>/dev/null | head -1)"
else
  if $CHECK_ONLY; then fail "Playwright CLI missing"; else
    warn "Playwright CLI missing — installing"
    npm install -g @playwright/cli@latest
    ok "Playwright CLI installed (browsers download on first use: npx playwright install chromium)"
  fi
fi

# --- 7. Project agents/skills sanity check ----------------------------------
for d in .claude/agents .claude/skills; do
  if [[ -d "$d" ]] && [[ -n "$(ls -A "$d")" ]]; then
    ok "$d ($(ls "$d" | wc -l) entries)"
  else
    fail "$d missing or empty — bad clone?"
  fi
done

# --- 8. Summary ---------------------------------------------------------------
echo
if $FAILED; then
  echo "Bootstrap INCOMPLETE — fix the [XX] items above."
  exit 1
fi
cat <<'EOF'
Bootstrap complete. Daily usage: just run `claude` in this project —
Headroom routing and proxy auto-start are wired via .claude/settings.local.json.

  headroom doctor    # verify routing + see tokens saved
  headroom dashboard # savings dashboard

Team, skills, and rules load automatically from .claude/ and CLAUDE.md.
EOF
