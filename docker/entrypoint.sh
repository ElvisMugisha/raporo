#!/usr/bin/env bash
# Container entrypoint for the `web` image.
#
# It exists so that `docker compose up` on a fresh clone reaches a *serving*
# app instead of a server sitting on an empty schema. It does the smallest
# amount of work that guarantees that, and nothing else:
#
#   1. `manage.py check` runs before the application server starts;
#   2. `manage.py migrate` runs only where migrating on boot is safe — the
#      environment must opt in *and* the command must be a recognised server;
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
# of, an opaque shell payload, a future launcher — gets the guard. It can be
# noisy; it cannot be bypassed by accident.
#
# Failing closed on an *unknown* payload is right. It was wrong for the two
# most common ways a pipeline runs the test suite, because those payloads are
# not unknown at all:
#
#     docker compose run --rm web python -m pytest
#     docker compose run --rm web bash -c 'pytest -q'
#
# Both used to reach `migrate`, on the dev database, moments before pytest
# built `test_raporo` on top of it. So the dispatch now runs in three passes:
#
#   pass 1  resolve argv through the wrappers it understands — `python -m X`,
#           `python script.py`, `sh -c "<one simple command>"` — down to the
#           payload that will really run. Anything it cannot resolve with
#           certainty resolves to nothing, which is treated as unknown.
#   pass 2  a never-list, checked *before* the RAPORO_ROLE dispatch: a test
#           runner is never pre-booted, whatever a valid role says. There is
#           no such thing as a legitimate `RAPORO_ROLE=server pytest`, and
#           `compose.prod.yaml` is expected to set `RAPORO_ROLE=server` on the
#           service — so this is structure rather than a comment asking people
#           to be careful. An *invalid* role still exits 64 here, because this
#           pass `exec`s and pass 3 never runs for a test runner.
#   pass 3  classify the resolved payload as `server`, `tooling` or `unknown`,
#           and let that decide the pre-boot sequence.
#
# `RAPORO_ROLE` is the explicit override for the cases inference cannot reach:
#   RAPORO_ROLE=server   force the pre-boot sequence (deploys, exotic launchers)
#   RAPORO_ROLE=tooling  force it off, loudly (a CI step that wraps its command
#                        in a shell and genuinely must not touch the database)
#   unset / auto         infer from the resolved command, failing closed
#
# ---------------------------------------------------------------------------
# `docker compose run` inherits the service `environment:` — twice over
# ---------------------------------------------------------------------------
# `RAPORO_ROLE=server` belongs in `compose.prod.yaml`, not in `compose.yaml`:
# the never-list is what makes an inherited `RAPORO_ROLE=server` safe, and dev
# is where `docker compose run --rm web pytest` inherits it.
#
# In prod the service really is a server, and one-off jobs are their own spec
# rather than a `run` against the web service, so nothing inherits the role
# that shouldn't. In dev the same service *is* how every one-off command is
# run, so the role would announce "server" for `ruff` and `manage.py` too and
# they would migrate on the way past. pytest itself would still be refused —
# that is the never-list's whole job — but the rest of the toolbox would not.
#
# `RAPORO_AUTO_MIGRATE=1` *is* in compose.yaml's `environment:`, and is
# inherited by every `docker compose run` for the same reason. That is why the
# classification above gates `migrate` and not just `check`: an inherited
# `RAPORO_AUTO_MIGRATE=1` is permission to migrate *when this container is a
# server*, and a command that only fell through to the guard because nothing
# recognised it is not that. Such a command still gets `check` — which is
# read-only and cheap — and is told on stderr why it did not migrate.
set -euo pipefail

log() { printf 'entrypoint: %s\n' "$*" >&2; }

# Refusing a typo'd role has two call sites — the never-list in pass 2, which
# `exec`s, and the dispatch in pass 3, which is where every other command
# meets it — so the message lives in one place.
bad_role() {
    log "RAPORO_ROLE must be 'server', 'tooling' or unset (got '${RAPORO_ROLE}')"
    exit 64  # EX_USAGE
}

# --- 0. There must be a command ----------------------------------------------
# `exec` with an empty word list is a no-op that *returns*, so without this an
# argument-less container falls straight through the guard into the pre-boot
# sequence and then exits 0, having migrated a database on the way past.
if [ "$#" -eq 0 ]; then
    log "no command given"
    exit 64  # EX_USAGE
fi

# --- 1. What is this container actually going to run? ------------------------
# `RESOLVED` is argv with the wrappers walked off, so that classification only
# ever looks at a real payload. An *empty* RESOLVED means "could not be
# resolved with certainty" and classifies as unknown, i.e. guarded. Nothing
# downstream executes RESOLVED — it decides, `exec "$@"` runs the original.
RESOLVED=()

resolve_argv() {
    local depth="$1"
    shift
    # Depth bounds the shell-in-shell walk. Nothing legitimate nests further,
    # and an unresolved walk is guarded, not exempt.
    [ "${depth}" -le 3 ] || return 0
    [ "$#" -gt 0 ] || return 0

    case "$(basename -- "$1")" in
        # `python -m pytest`, `python -u manage.py migrate`, `python -m
        # granian`: the payload is the module or script, not the interpreter.
        python | python3 | python3.*)
            shift
            while [ "$#" -gt 0 ]; do
                case "$1" in
                    # Options that consume the following word.
                    -W | -X | --check-hash-based-pycs)
                        shift 2 || return 0
                        ;;
                    # An inline program, or a program on stdin. Opaque.
                    -c | -)
                        return 0
                        ;;
                    -m)
                        shift
                        break
                        ;;
                    # -u, -B, -O, -Wall, -I, ... none change the payload.
                    -*)
                        shift
                        ;;
                    *)
                        break
                        ;;
                esac
            done
            # Nothing left: a bare interpreter is a REPL, an unknown payload.
            [ "$#" -gt 0 ] || return 0
            RESOLVED=("$@")
            ;;

        # A bare interactive shell is a debug session and resolves to itself.
        # `sh -c "..."` is resolved only when the string is one simple command
        # of ordinary characters: no `;`, `&&`, `|`, redirection, expansion,
        # quoting or glob. `bash -c 'pytest -q'` is the CI shape that matters;
        # `bash -c 'pytest && gunicorn ...'` is two payloads and stays unknown.
        # Every other shell form (`-i`, `-lc`, a script path) is unknown too.
        sh | bash | dash | ash | zsh)
            if [ "$#" -eq 1 ]; then
                RESOLVED=("$1")
                return 0
            fi
            [ "$2" = "-c" ] && [ "$#" -eq 3 ] || return 0
            case "$3" in
                "" | *[![:alnum:][:blank:]_.,:/=+-]*) return 0 ;;
            esac
            # Deliberate word split: the pattern above has already refused
            # every character that would make this do more than split.
            # shellcheck disable=SC2086
            set -- $3
            resolve_argv "$((depth + 1))" "$@"
            ;;

        # Already a payload: gunicorn, pytest, ruff, manage.py, tail, ...
        *)
            RESOLVED=("$@")
            ;;
    esac
}

resolve_argv 1 "$@"

# --- 2. Test runners are never pre-booted, whatever a valid role says --------
# pytest creates and drops its own database. A `migrate` here does not just
# waste time, it applies the schema to the *development* database seconds
# before pytest builds `test_raporo` — the failure this file was rewritten
# for. This is checked ahead of the RAPORO_ROLE dispatch on purpose: a
# service-level `RAPORO_ROLE=server` (which `compose.prod.yaml` is expected to
# set) must not be able to turn `docker compose run web pytest` back into a
# migration. The role's *value* is still checked here, because this block
# `exec`s and would otherwise be the one command whose typo'd role is ignored.
case "$(basename -- "${RESOLVED[0]:-}")" in
    pytest | py.test)
        case "${RAPORO_ROLE:-auto}" in
            # A *valid* role never changes this outcome; an explicit one that
            # got overruled is told so.
            server)
                log "ignoring RAPORO_ROLE=server: a test runner is never pre-booted"
                ;;
            tooling | auto) ;;
            # An *invalid* role is a typo, and the `exec` below is the last
            # chance to say so: pass 3, where every other command's role is
            # validated, never runs for a test runner. `run -e
            # RAPORO_ROLE=srever web pytest` is the likeliest place in the
            # whole file for that typo — it is what CI types — so it fails
            # loudly here instead of running the suite with the role ignored.
            *) bad_role ;;
        esac
        exec "$@"
        ;;
esac

# --- 3. Classify the resolved payload ----------------------------------------
# server  — positively identified as starting the application server. Gets the
#           full pre-boot sequence, and is the only kind allowed to migrate.
# tooling — positively identified as a one-off. Exec'd untouched.
# unknown — everything else, including an argv this file could not resolve.
#           Guarded, but not trusted with a migration.
classify_command() {
    case "$(basename -- "${1:-}")" in
        ruff)
            echo tooling
            ;;
        # Resolution reduces shells to the bare interactive form; a shell that
        # still has arguments here is an unresolved payload.
        sh | bash | dash | ash | zsh)
            if [ "$#" -eq 1 ]; then echo tooling; else echo unknown; fi
            ;;
        # Django's own admin commands are one-offs, except the ones that start
        # a server. `manage.py` with no subcommand prints usage and exits.
        manage.py)
            case "${2:-}" in
                runserver | runserver_plus | testserver) echo server ;;
                *) echo tooling ;;
            esac
            ;;
        # gunicorn, uvicorn, daphne, hypercorn, granian, waitress-serve, an
        # inline `python -c`, a REPL, and whatever replaces them.
        *)
            echo unknown
            ;;
    esac
}

case "${RAPORO_ROLE:-auto}" in
    server)
        kind=server
        log "RAPORO_ROLE=server — running pre-boot sequence"
        ;;
    tooling)
        kind=tooling
        log "RAPORO_ROLE=tooling — skipping pre-boot sequence for: $1"
        ;;
    auto)
        kind="$(classify_command "${RESOLVED[@]:-}")"
        # Name the *resolved* payload, not argv[0]: for `python -m granian` the
        # thing an operator has to go and look at is granian, not python.
        if [ "${kind}" = "unknown" ] && [ "${#RESOLVED[@]}" -gt 0 ]; then
            log "unrecognised command '$(basename -- "${RESOLVED[0]}")' — running pre-boot checks (fail closed)"
        elif [ "${kind}" = "unknown" ]; then
            log "could not resolve what '$1' will run — running pre-boot checks (fail closed)"
        fi
        ;;
    *)
        bad_role
        ;;
esac

# Exempt commands are exec'd in silence: the container's stdout belongs to the
# command, and "pytest is not a server" is not news. Everything the entrypoint
# *does* is logged, to stderr.
if [ "${kind}" = "tooling" ]; then
    exec "$@"
fi

# --- 4. Refuse to serve a misconfigured app ----------------------------------
# Uncontroversial, cheap, and needs no database connection. It is also where
# the checks registered in `common/checks.py` actually get run before traffic
# arrives; a check that nothing executes protects nothing.
log "running manage.py check"
python manage.py check

# --- 5. Migrate only where migrating on boot is actually safe ----------------
# Auto-migrate is a dev convenience, not a deploy strategy: with more than one
# replica a rolling deploy has every new container racing the same migration
# lock, and an unreviewed schema change would ship with the code that happened
# to carry it. So it is opt-in and OFF by default in the image; compose.yaml
# turns it on for local dev only, where there is exactly one container and the
# alternative is a broken first run. Deploys run `migrate` as their own
# reviewed, gated step and leave RAPORO_AUTO_MIGRATE unset.
#
# It also takes a `server`: because compose.yaml sets RAPORO_AUTO_MIGRATE in
# the service `environment:`, every `docker compose run` inherits it, and an
# unrecognised command must not spend that permission.
if [ "${RAPORO_AUTO_MIGRATE:-0}" != "1" ]; then
    log "RAPORO_AUTO_MIGRATE not set — skipping migrate"
elif [ "${kind}" != "server" ]; then
    log "RAPORO_AUTO_MIGRATE=1 but this is not a recognised server — skipping migrate (RAPORO_ROLE=server forces it)"
else
    log "RAPORO_AUTO_MIGRATE=1 — applying migrations"
    python manage.py migrate --noinput
fi

exec "$@"
