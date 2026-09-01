"""Test settings: dev settings plus the throwaway app that hosts concrete
stand-ins for the abstract bases in `common/`.

`tests.testapp` is installed ONLY here so its tables never reach a real
database and `makemigrations --check` (which runs on `config.settings.dev`)
stays clean.
"""

from .dev import *  # noqa: F403
from .dev import INSTALLED_APPS as _DEV_INSTALLED_APPS

INSTALLED_APPS = [*_DEV_INSTALLED_APPS, "tests.testapp"]
