"""Apply `scripts/db/runtime-privileges.sql` - phase 2 of the role split.

Run it as the schema owner, immediately after every `migrate`:

    python manage.py migrate --database=migrator
    python manage.py grant_runtime_privileges --database=migrator

`docker/entrypoint.sh` does both on a dev boot, and a deploy's migration job
must do both: they are one step expressed as two commands.

Why a management command and not psql
-------------------------------------
The alternative was `manage.py dbshell --database=migrator <
scripts/db/runtime-privileges.sql`, which needs a `psql` binary. `psql` is in
the `dev` image only - the `runtime` image deliberately ships no database
client and no shell tooling - so a psql-based step would either force a client
into a served image or make the privilege step conditional on which image ran
it. A conditional security step is how controls end up inert. This runs
anywhere `manage.py migrate` runs, as the same identity, over the same
connection, with no extra binary.

Why the SQL is a file and not a string in here
----------------------------------------------
So it can be reviewed as SQL, applied by an operator with psql during an
incident, and diffed against `scripts/db/roles.sql` (phase 1) without reading
Python. The file ships in the image: see the COPY in `docker/Dockerfile`.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

#: Relative to BASE_DIR, which is the repository root in dev (bind-mounted at
#: /app) and /app in every image. One path, both places.
SQL_PATH = Path("scripts") / "db" / "runtime-privileges.sql"


class Command(BaseCommand):
    help = (
        "Grant raporo_app and raporo_backup their runtime privileges, and "
        "revoke the ones they must not have. Idempotent; run after migrate."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default="migrator",
            help=(
                "Connection alias to apply the SQL through. Must be a role "
                "that owns the tables - the default `migrator` alias is "
                "raporo_owner. Defaults to 'migrator'."
            ),
        )

    def handle(self, *args, **options):
        alias = options["database"]

        # `migrator` is absent from DATABASES wherever the owner credential is
        # not injected (see config/settings/base.py), so this is the common
        # failure and it deserves the better message: Django's own
        # ConnectionDoesNotExist does not mention the variables to set.
        if alias not in connections:
            raise CommandError(
                f"No database connection named '{alias}'. Runtime privileges "
                "are granted by the schema owner, which reaches the database "
                "through the `migrator` alias; that alias exists only where "
                "RAPORO_MIGRATE_USER and RAPORO_MIGRATE_PASSWORD are both set. "
                "Set them, or pass --database=<alias> for a connection whose "
                "role owns the tables."
            )

        sql_path = Path(settings.BASE_DIR) / SQL_PATH
        try:
            sql = sql_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CommandError(
                f"Cannot read {sql_path}: {exc}. This file is the privilege "
                "definition itself; without it the runtime role keeps whatever "
                "privileges it currently has, so this command refuses to "
                "report success."
            ) from exc

        connection = connections[alias]

        # One transaction for the whole file: a half-applied privilege set is
        # worse than none, because the half that applied is the REVOKE.
        with transaction.atomic(using=alias):
            with connection.cursor() as cursor:
                cursor.execute(sql)

                cursor.execute("SELECT current_user")
                (applied_as,) = cursor.fetchone()

                # Report the result rather than the intent. `\dp` in a psql
                # session is the same query; having the command print it means
                # a deploy log records what the runtime role can actually do,
                # which is the thing a later incident wants to know.
                cursor.execute(
                    """
                    SELECT table_name,
                           string_agg(privilege_type, ',' ORDER BY privilege_type)
                    FROM information_schema.table_privileges
                    WHERE grantee = 'raporo_app' AND table_schema = 'public'
                    GROUP BY table_name
                    ORDER BY table_name
                    """
                )
                privileges = cursor.fetchall()

        self.stdout.write(
            f"Applied {SQL_PATH} to '{alias}' as '{applied_as}'. "
            f"raporo_app privileges on {len(privileges)} tables:"
        )
        for table_name, granted in privileges:
            self.stdout.write(f"  {table_name:<34} {granted}")
