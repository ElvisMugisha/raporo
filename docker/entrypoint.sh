#!/usr/bin/env bash
# Container entrypoint for the `web` image.
#
# It exists so that `docker compose up` on a fresh clone reaches a *serving*
# app instead of a server sitting on an empty schema. It does the smallest
# amount of work that guarantees that, and nothing else:
#
#   1. `manage.py check` runs before the application server starts;
#   2. `manage.py migrate` runs only when the environment opts in;
#   3. one-off developer commands (pytest, ruff, admin commands, a debug
#      shell) are exec'd untouched, because pytest manages its own database
#      and a `manage.py shell` must not silently mutate the schema.
#
# ---------------------------------------------------------------------------
# Why the dispatch is shaped the way it is
# ---------------------------------------------------------------------------
# An earlier version asked "does any argument look like a server?" and ran the
# pre-boot sequence if so. That failed in both directions: `pytest -k
# runserver` migrated a live database, while `gunicorn`, `/usr/local/bin/
# gunicorn`, `hypercorn`, `granian` and `sh -c "gunicorn ..."` skipped the
# only pre-boot guard there is, silently.
#
# So the polarity is inverted. **The pre-boot sequence is the default.** A
# command is exempt only if it is on an explicit list of things known to be
# one-off tooling. Anything unrecognised — a server this file has never heard
# of, a shell wrapper, a future launcher — gets the guard. It can be noisy;
# it cannot be bypassed by accident.
#
# `RAPORO_ROLE` is the explicit override for the cases inference cannot reach:
#   RAPORO_ROLE=server   force the pre-boot sequence (deploys, exotic launchers)
#   RAPORO_ROLE=tooling  force it off, loudly (a CI step that wraps its command
#                        in a shell and genuinely must not touch the database)
#   unset / auto         infer from the command, failing closed
#
# Do NOT put RAPORO_ROLE=server in a compose service's `environment:`.
# `docker compose run --rm web pytest` inherits the service environment, so it
# would force the guard back onto pytest — the exact bug this file was fixed
# for. It belongs on a deploy workload spec, where one-off jobs are their own
# spec and do not inherit the server's.
set -euo pipefail

log() { printf 'entrypoint: %s\n' "$*" >&2; }

# --- 0. There must be a command ----------------------------------------------
# `exec` with an empty word list is a no-op that *returns*, so without this an
# argument-less container falls straight through the guard into the pre-boot
# sequence and then exits 0, having migrated a database on the way past.
if [ "$#" -eq 0 ]; then
    log "no command given"
    exit 64  # EX_USAGE
fi

# --- 1. Does this container need the pre-boot sequence? ----------------------
# Matches on basename "$1" only: the command is argv[0], and an absolute path
# in a Kubernetes `command:` is the same command as its bare name.
needs_preboot() {
    case "$(basename -- "$1")" in
        # Test runners and linters. pytest creates and drops its own database
        # and must never race a migrate.
        pytest | py.test | ruff | coverage)
            return 1
            ;;

        # A bare interactive shell is a debug session. A shell *with* arguments
        # is a wrapper around an unknown payload — `sh -c "gunicorn ..."` is a
        # real deployment shape — so it is not exempt. Use RAPORO_ROLE=tooling
        # if a wrapped command truly must skip the guard.
        sh | bash | dash | ash | zsh)
            if [ "$#" -eq 1 ]; then
                return 1
            fi
            return 0
            ;;

        # `python manage.py <subcommand>`: Django's own admin commands are
        # one-offs and exempt, except the ones that start a server. Any other
        # use of the interpreter (`python -m granian`, `python -c ...`) is an
        # unknown payload and is not exempt.
        python | python3 | python3.* | manage.py)
            local script sub
            case "$(basename -- "$1")" in
                manage.py) script="manage.py"; sub="${2:-}" ;;
                *)         script="$(basename -- "${2:-}")"; sub="${3:-}" ;;
            esac
            [ "${script}" = "manage.py" ] || return 0
            case "${sub}" in
                runserver | runserver_plus | testserver) return 0 ;;
                *) return 1 ;;
            esac
            ;;

        # Unrecognised: gunicorn, uvicorn, daphne, hypercorn, granian,
        # waitress-serve, and whatever replaces them. Fail closed.
        *)
            return 0
            ;;
    esac
}

case "${RAPORO_ROLE:-auto}" in
    server)
        preboot=1
        log "RAPORO_ROLE=server — running pre-boot sequence"
        ;;
    tooling)
        preboot=0
        log "RAPORO_ROLE=tooling — skipping pre-boot sequence for: $1"
        ;;
    auto)
        if needs_preboot "$@"; then preboot=1; else preboot=0; fi
        ;;
    *)
        log "RAPORO_ROLE must be 'server', 'tooling' or unset (got '${RAPORO_ROLE}')"
        exit 64  # EX_USAGE
        ;;
esac

# Exempt commands are exec'd in silence: the container's stdout belongs to the
# command, and "pytest is not a server" is not news. Everything the entrypoint
# *does* is logged, to stderr.
if [ "${preboot}" -eq 0 ]; then
    exec "$@"
fi

# --- 2. Refuse to serve a misconfigured app ----------------------------------
# Uncontroversial, cheap, and needs no database connection. It is also where
# the checks registered in `common/checks.py` actually get run before traffic
# arrives; a check that nothing executes protects nothing.
log "running manage.py check"
python manage.py check

# --- 3. Migrate only where migrating on boot is actually safe ----------------
# Auto-migrate is a dev convenience, not a deploy strategy: with more than one
# replica a rolling deploy has every new container racing the same migration
# lock, and an unreviewed schema change would ship with the code that happened
# to carry it. So it is opt-in and OFF by default in the image; compose.yaml
# turns it on for local dev only, where there is exactly one container and the
# alternative is a broken first run. Deploys run `migrate` as their own
# reviewed, gated step and leave RAPORO_AUTO_MIGRATE unset.
if [ "${RAPORO_AUTO_MIGRATE:-0}" = "1" ]; then
    log "RAPORO_AUTO_MIGRATE=1 — applying migrations"
    python manage.py migrate --noinput
else
    log "RAPORO_AUTO_MIGRATE not set — skipping migrate"
fi

exec "$@"
