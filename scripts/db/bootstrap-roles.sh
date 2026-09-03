#!/usr/bin/env bash
# Create (or repair) the raporo_owner / raporo_app / raporo_backup roles.
# Runs `scripts/db/roles.sql` as a superuser; that file is where the design
# lives and this one only delivers credentials to it.
#
# THIS IS HALF THE JOB. It sets up everything that exists before the schema
# does: the roles, who may connect, who owns `public`, and the default
# privileges future tables inherit. The privileges on the tables that exist
# *now* are phase 2, and they run as the owner after `migrate`:
#
#     python manage.py migrate --database=migrator
#     python manage.py grant_runtime_privileges --database=migrator
#
# On a fresh dev volume both happen automatically (this script at initdb, the
# two commands from docker/entrypoint.sh). Anywhere else, run both.
#
# TWO CALLERS, ONE SCRIPT
# ---------------------------------------------------------------------------
#   1. Fresh dev volume. compose.yaml mounts this file into the `db` service at
#      /docker-entrypoint-initdb.d/10-raporo-roles.sh, so it runs once, during
#      `initdb`, before `web` has connected to anything. That ordering is what
#      makes the ALTER DEFAULT PRIVILEGES in roles.sql cover *every* table the
#      first `migrate` creates.
#
#   2. Any database that already exists — an existing dev volume, staging,
#      production, a restored backup. Re-run it; it is idempotent and it
#      repairs drift:
#
#          docker compose exec db /docker-entrypoint-initdb.d/10-raporo-roles.sh
#
#      or, against a managed instance, from a host with psql and the superuser
#      credential (never from the application container — see below):
#
#          PGHOST=... PGUSER=<superuser> PGPASSWORD=... PGDATABASE=raporo \
#          RAPORO_APP_PASSWORD=... RAPORO_MIGRATE_PASSWORD=... \
#          RAPORO_BACKUP_PASSWORD=... scripts/db/bootstrap-roles.sh
#
# WHY THE APPLICATION CONTAINER DOES NOT RUN THIS
# ---------------------------------------------------------------------------
# It needs a superuser credential. Putting that in the web service's
# environment so the entrypoint could bootstrap its own roles would hand a
# compromised web process the one credential that can undo the entire split —
# which is the situation this work exists to end. Bootstrapping is an operator
# or pipeline step with its own credential, and it happens once per database.
set -euo pipefail

log() { printf 'raporo-roles: %s\n' "$*" >&2; }

die() {
    log "$*"
    exit 78 # EX_CONFIG
}

# --- credentials --------------------------------------------------------------
# Required, and each one is checked by name so a missing variable names itself
# instead of producing a role with an empty password.
for var in RAPORO_APP_PASSWORD RAPORO_MIGRATE_PASSWORD RAPORO_BACKUP_PASSWORD; do
    if [ -z "${!var:-}" ]; then
        die "${var} is unset or empty — set it in .env (dev) or the platform's secret store (deploy). Refusing to create a role without a password."
    fi
done

# --- where the SQL is ---------------------------------------------------------
# In the initdb case this script sits in /docker-entrypoint-initdb.d while the
# SQL is mounted at /opt/raporo/roles.sql, because the postgres entrypoint runs
# *every* file in that directory and would execute roles.sql itself, without
# the psql variables it needs. Hence two mounts and this lookup.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for candidate in \
    "${RAPORO_ROLES_SQL:-}" \
    "${script_dir}/roles.sql" \
    "/opt/raporo/roles.sql"; do
    if [ -n "${candidate}" ] && [ -r "${candidate}" ]; then
        roles_sql="${candidate}"
        break
    fi
done
[ -n "${roles_sql:-}" ] || die "cannot find roles.sql (looked at \$RAPORO_ROLES_SQL, ${script_dir}/roles.sql, /opt/raporo/roles.sql)"

# --- connection ---------------------------------------------------------------
# Standard libpq variables win. `POSTGRES_USER`/`POSTGRES_DB` are the fallback
# because those are what the postgres image exports during initdb, where this
# script has no other way to know which superuser and database to use.
export PGUSER="${PGUSER:-${POSTGRES_USER:-postgres}}"
export PGDATABASE="${PGDATABASE:-${POSTGRES_DB:-postgres}}"

# `on`/`off` for psql's \if. Grants CREATEDB to raporo_owner so pytest can
# build and drop test_raporo. Off unless asked for: production creates no
# databases, and the migration credential is the one that travels through CI.
case "${RAPORO_DB_DEV_EXTRAS:-0}" in
    1 | true | yes | on) dev_extras=on ;;
    0 | false | no | off | "") dev_extras=off ;;
    *) die "RAPORO_DB_DEV_EXTRAS must be 1 or 0 (got '${RAPORO_DB_DEV_EXTRAS}')" ;;
esac

log "applying ${roles_sql} to database '${PGDATABASE}' as '${PGUSER}' (dev_extras=${dev_extras})"

# --no-password: fail rather than block on an interactive prompt if the
# credential is wrong. This runs unattended in initdb and in pipelines.
psql \
    --no-password \
    --no-psqlrc \
    --quiet \
    --set=ON_ERROR_STOP=1 \
    --set=owner_pw="${RAPORO_MIGRATE_PASSWORD}" \
    --set=app_pw="${RAPORO_APP_PASSWORD}" \
    --set=backup_pw="${RAPORO_BACKUP_PASSWORD}" \
    --set=dev_extras="${dev_extras}" \
    --file="${roles_sql}"

log "done"
