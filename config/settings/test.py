"""Test settings: dev settings plus the throwaway app that hosts concrete
stand-ins for the abstract bases in `common/`.

`tests.testapp` is installed ONLY here so its tables never reach a real
database and `makemigrations --check` (which runs on `config.settings.dev`)
stays clean.
"""

from django.core.exceptions import ImproperlyConfigured

from .dev import *  # noqa: F403
from .dev import DATABASES as _DEV_DATABASES
from .dev import INSTALLED_APPS as _DEV_INSTALLED_APPS

INSTALLED_APPS = [*_DEV_INSTALLED_APPS, "tests.testapp"]

# The suite runs as raporo_owner — the migrator identity — and that is not a
# convenience.
#
# `pytest-django` creates `test_raporo`, runs `migrate` inside it and drops it
# again. That needs CREATEDB and ownership of everything it creates, which is
# precisely what `raporo_app` must never have: the runtime role owns nothing
# and cannot create a database. The test suite is a *migrator* workload by
# construction, so it connects with the migrator credential.
#
# The consequence to keep in mind once policies land: as the table owner, with
# row-level security `ENABLE`d and not `FORCE`d, the suite is not subject to
# any policy. Existing tests are therefore unaffected — no fixture rewrites,
# no silent zero-row results — and RLS tests must take the app role explicitly
# inside their own transaction, or they prove nothing. That is deliberate; the
# alternative (`FORCE`) makes every data-migration backfill silently update
# zero rows.
#
# Only `default` is switched. The `migrator` alias stays as base.py built it,
# which makes both aliases identical here; Django's test runner keys databases
# by (HOST, PORT, ENGINE, test name), so the two share one `test_raporo` and no
# second database is created.
if "migrator" not in _DEV_DATABASES:
    raise ImproperlyConfigured(
        "The test suite needs the migrator credential: it creates and drops "
        "test_raporo and runs migrate, which raporo_app cannot do by design. "
        "Set RAPORO_MIGRATE_USER=raporo_owner and RAPORO_MIGRATE_PASSWORD in "
        "the environment (docker compose already does for the `web` service; "
        "a CI test step must inject them too)."
    )

DATABASES = {
    **_DEV_DATABASES,
    "default": {
        **_DEV_DATABASES["default"],
        "USER": _DEV_DATABASES["migrator"]["USER"],
        "PASSWORD": _DEV_DATABASES["migrator"]["PASSWORD"],
    },
}
