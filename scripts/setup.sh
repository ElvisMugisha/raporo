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
    uv tool install --python 3.13 "headroom-ai[all]"
    ok "Headroom installed"
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

# --- 5. Project agents/skills sanity check ----------------------------------
for d in .claude/agents .claude/skills; do
  if [[ -d "$d" ]] && [[ -n "$(ls -A "$d")" ]]; then
    ok "$d ($(ls "$d" | wc -l) entries)"
  else
    fail "$d missing or empty — bad clone?"
  fi
done

# --- 6. Summary ---------------------------------------------------------------
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
