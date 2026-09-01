from django.apps import AppConfig


class CommonConfig(AppConfig):
    """`common` holds abstract bases only - no models, so no migrations.

    It is installed so its system checks run on every `manage.py check`,
    `migrate` and test-database build.
    """

    name = "common"
    label = "common"

    def ready(self):
        from common import checks  # noqa: F401  (registers the system checks)
