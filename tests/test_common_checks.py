"""The system checks that stop a later task from disarming invariant #1.

Each rogue model below is a shape a slice-2 author could plausibly write. The
checks have to reject them at startup, because none of them fails a query.
"""

import pytest
from django.apps import apps as global_apps
from django.conf import settings as django_settings
from django.core.checks import Error, Tags, run_checks
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.db import connection, models
from django.db.models import Q
from django.test.utils import isolate_apps

from common import checks
from common.checks import audit_store_scoped_models, check_store_scoped_models
from common.managers import StoreScopedManager
from common.models import AuditedModel, SoftDeleteModel, StoreScopedModel
from tests.testapp.models import (
    Category,
    Product,
    Sale,
    SaleLine,
    ScopedThing,
    ScopedThingOwnMeta,
    Thing,
)

LIVE = Q(deleted_at__isnull=True)


def ids(errors: list[Error]) -> set[str]:
    return {error.id for error in errors}


def test_the_real_models_pass():
    assert check_store_scoped_models(None) == []
    assert audit_store_scoped_models(
        [ScopedThing, ScopedThingOwnMeta, Thing, Category, Product, Sale, SaleLine]
    ) == []


def test_non_scoped_models_are_ignored():
    assert audit_store_scoped_models([Thing, Category, object, 42]) == []


@isolate_apps("tests.testapp")
def test_the_check_honours_the_app_configs_it_is_given():
    """`manage.py check <app>` reports that app, and does not report others.

    Asserting only `== []` for an unrelated app proves nothing: every real model
    passes every check, so that assertion holds just as well with the
    `app_configs` filter deleted. The rogue model below is what the two
    directions can disagree about - it exists only in the isolated registry, so
    a check that ignored `app_configs` and fell back to the whole project could
    not report it, and the "given the app" assertion would fail.
    """

    class RogueInTheAppUnderCheck(StoreScopedModel):
        code = models.CharField(max_length=20, unique=True)  # common.E005

        class Meta:
            app_label = "testapp"

    testapp_config = RogueInTheAppUnderCheck._meta.apps.get_app_config("testapp")
    accounts_config = global_apps.get_app_config("accounts")

    # Premise: the rogue model is the only model in the app config we pass.
    assert list(testapp_config.get_models()) == [RogueInTheAppUnderCheck]

    assert "common.E005" in ids(check_store_scoped_models([testapp_config]))
    assert check_store_scoped_models([accounts_config]) == []


@isolate_apps("tests.testapp")
def test_the_check_stays_silent_about_apps_it_was_not_given(monkeypatch):
    """The other direction, made mutation-sensitive.

    Here the broken model *is* reachable from the registry the whole-project run
    walks, so a check that dropped the `app_configs` filter would report it while
    being asked only about `accounts`.
    """

    class RogueNobodyAskedAbout(StoreScopedModel):
        code = models.CharField(max_length=20, unique=True)  # common.E005

        class Meta:
            app_label = "testapp"

    monkeypatch.setattr(checks, "global_apps", RogueNobodyAskedAbout._meta.apps)

    # Premise: the whole-project run does see it.
    assert "common.E005" in ids(check_store_scoped_models(None))

    assert check_store_scoped_models([global_apps.get_app_config("accounts")]) == []


# --------------------------------------------------------------------------
# E001 / E002 - the default manager
# --------------------------------------------------------------------------


@isolate_apps("tests.testapp")
def test_a_model_that_overrides_objects_is_rejected():
    class RogueManagerModel(StoreScopedModel):
        objects = models.Manager()

        class Meta:
            app_label = "testapp"

    assert "common.E001" in ids(audit_store_scoped_models([RogueManagerModel]))


@isolate_apps("tests.testapp")
def test_an_extra_manager_that_steals_the_default_is_rejected():
    """The hole a negative-only E002 missed.

    Django skips the inherited `default_manager_name` as soon as a model
    declares *any* local manager, and then sorts depth-0 managers first - so
    this innocuous-looking `recent` becomes `_default_manager`.
    """

    class ExtraManagerModel(StoreScopedModel):
        recent = models.Manager()

        class Meta:
            app_label = "testapp"

    assert ExtraManagerModel._default_manager.name == "recent"
    assert "common.E002" in ids(audit_store_scoped_models([ExtraManagerModel]))


@isolate_apps("tests.testapp")
def test_a_model_that_makes_the_guarded_manager_the_default_is_rejected():
    class GuardedDefaultModel(StoreScopedModel):
        objects = StoreScopedManager()

        class Meta:
            app_label = "testapp"
            default_manager_name = "objects"

    assert "common.E002" in ids(audit_store_scoped_models([GuardedDefaultModel]))


def test_the_real_models_keep_the_unguarded_default_manager():
    assert ScopedThing._default_manager.name == "all_objects"
    # Even the subclass that declares its own Meta without inheriting the base's.
    assert ScopedThingOwnMeta._meta.default_manager_name is None
    assert ScopedThingOwnMeta._default_manager.name == "all_objects"


# --------------------------------------------------------------------------
# E003 - the store foreign key
# --------------------------------------------------------------------------


@isolate_apps("tests.testapp")
def test_a_model_that_repoints_the_store_fk_is_rejected():
    class WrongStoreModel(StoreScopedModel):
        store = models.ForeignKey(
            "orgs.Organization", on_delete=models.PROTECT, related_name="+"
        )

        class Meta:
            app_label = "testapp"

    assert "common.E003" in ids(audit_store_scoped_models([WrongStoreModel]))


@isolate_apps("tests.testapp")
def test_a_model_that_makes_the_store_optional_is_rejected():
    class OptionalStoreModel(StoreScopedModel):
        store = models.ForeignKey(
            "orgs.Store", null=True, on_delete=models.PROTECT, related_name="+"
        )

        class Meta:
            app_label = "testapp"

    assert "common.E003" in ids(audit_store_scoped_models([OptionalStoreModel]))


# --------------------------------------------------------------------------
# E004 - no traversable relation may reach store-scoped rows
# --------------------------------------------------------------------------


@isolate_apps("tests.testapp")
def test_a_forward_fk_that_creates_an_accessor_on_the_parent_is_rejected():
    """`category.products.all()` - the reproduced two-org read."""

    class Parent(SoftDeleteModel, AuditedModel):
        class Meta:
            app_label = "testapp"

    class ChildWithAccessor(StoreScopedModel):
        parent = models.ForeignKey(Parent, on_delete=models.PROTECT, related_name="products")

        class Meta:
            app_label = "testapp"

    assert "common.E004" in ids(audit_store_scoped_models([ChildWithAccessor]))


@isolate_apps("tests.testapp")
def test_a_reverse_accessor_into_a_store_scoped_model_is_rejected():
    """`sale.lines.all()` - the reproduced cross-store child read."""

    class ScopedParent(StoreScopedModel):
        class Meta:
            app_label = "testapp"

    class ScopedChild(StoreScopedModel):
        parent = models.ForeignKey(ScopedParent, on_delete=models.PROTECT, related_name="lines")

        class Meta:
            app_label = "testapp"

    errors = audit_store_scoped_models([ScopedParent, ScopedChild])

    assert "common.E004" in ids(errors)
    assert any("lines" in error.msg for error in errors)


# --------------------------------------------------------------------------
# E005 - uniqueness on a store-scoped table is per store, among live rows
# --------------------------------------------------------------------------


@isolate_apps("tests.testapp")
def test_a_global_unique_field_is_rejected():
    """The reproduced existence oracle: `full_clean()` on a globally unique
    field told tenant B that tenant A's code was taken."""

    class Coded(StoreScopedModel):
        code = models.CharField(max_length=20, unique=True)

        class Meta:
            app_label = "testapp"

    assert "common.E005" in ids(audit_store_scoped_models([Coded]))


@isolate_apps("tests.testapp")
def test_a_unique_constraint_without_store_is_rejected():
    class CodedConstraint(StoreScopedModel):
        code = models.CharField(max_length=20)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["code"], condition=LIVE, name="testapp_coded_code"
                )
            ]

    assert "common.E005" in ids(audit_store_scoped_models([CodedConstraint]))


@isolate_apps("tests.testapp")
def test_a_unique_constraint_that_ignores_soft_delete_is_rejected():
    class CodedForever(StoreScopedModel):
        code = models.CharField(max_length=20)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["store", "code"], name="testapp_coded_forever"
                )
            ]

    assert "common.E005" in ids(audit_store_scoped_models([CodedForever]))


@isolate_apps("tests.testapp")
def test_unique_together_is_rejected():
    class OldStyle(StoreScopedModel):
        code = models.CharField(max_length=20)

        class Meta:
            app_label = "testapp"
            unique_together = [("store", "code")]

    assert "common.E005" in ids(audit_store_scoped_models([OldStyle]))


@isolate_apps("tests.testapp")
def test_a_per_store_live_unique_constraint_passes():
    class Proper(StoreScopedModel):
        code = models.CharField(max_length=20)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["store", "code"], condition=LIVE, name="testapp_proper_code"
                )
            ]

    assert audit_store_scoped_models([Proper]) == []


# --------------------------------------------------------------------------
# E006 - only store-scoped models may point at store-scoped models
# --------------------------------------------------------------------------


@isolate_apps("tests.testapp")
def test_an_org_level_model_pointing_at_a_store_scoped_model_is_rejected():
    class ScopedTarget(StoreScopedModel):
        class Meta:
            app_label = "testapp"

    class OrgLevelPointer(SoftDeleteModel, AuditedModel):
        favourite = models.ForeignKey(
            ScopedTarget, on_delete=models.PROTECT, related_name="+"
        )

        class Meta:
            app_label = "testapp"

    errors = audit_store_scoped_models([ScopedTarget, OrgLevelPointer])

    assert "common.E006" in ids(errors)


# --------------------------------------------------------------------------
# E100 - the pre-boot refusal to run against a `test_`-named database
#
# Every test here drives the check *registry*, never the function. That is the
# whole point: `check_database_is_not_test_named(None)` returned the right
# answer all along, while `manage.py check` never called it - the check was
# registered under `Tags.database`, and `CheckRegistry.run_checks` drops those
# unless an alias is passed explicitly. A direct-call test would have passed on
# the broken code and proved nothing.
# --------------------------------------------------------------------------

E100 = "common.E100"


def e100_errors(**kwargs) -> list[Error]:
    """The E100 errors `manage.py check` (no arguments) would report."""
    return [error for error in run_checks(**kwargs) if error.id == E100]


def test_e100_fires_through_the_registry_on_a_test_named_database(db, settings):
    """The suite runs against a database Django named `test_raporo`, so turning
    the production flag on here *is* the production misconfiguration."""
    assert connection.settings_dict["NAME"].startswith("test_")  # premise
    settings.ENFORCE_NON_TEST_DATABASE = True

    errors = e100_errors()

    assert [error.id for error in errors] == [E100]
    assert "test_" in errors[0].msg


def test_manage_py_check_refuses_to_pass_on_a_test_named_database(db, settings):
    """End to end through the management command, which is what a container's
    pre-boot step runs: a non-zero exit, not a warning in a log."""
    settings.ENFORCE_NON_TEST_DATABASE = True

    with pytest.raises(SystemCheckError) as exc:
        call_command("check")

    assert E100 in str(exc.value)


def test_e100_is_not_tagged_database_because_that_tag_is_skipped_by_default(db, settings):
    """The regression itself, pinned: a database-tagged check is invisible to a
    bare `manage.py check`, so this guard must not carry that tag."""
    settings.ENFORCE_NON_TEST_DATABASE = True

    assert Tags.database not in checks.check_database_is_not_test_named.tags
    assert e100_errors(databases=None) == e100_errors(databases=["default"])


def test_e100_is_silent_when_the_flag_is_off(db):
    """Dev and test settings never set it, so the suite's own `test_` database
    is not an error - if it were, nobody could run `manage.py check` locally."""
    assert not getattr(django_settings, "ENFORCE_NON_TEST_DATABASE", False)  # premise

    assert e100_errors() == []


# Django warns about any DATABASES override, and it is right to in general.
# Here the override builds fresh dicts (it never mutates the live connection's
# settings) and the test opens no connection, so the warning is noise.
@pytest.mark.filterwarnings("ignore:Overriding setting DATABASES:UserWarning")
def test_e100_is_silent_on_a_normally_named_database(django_db_setup, settings):
    """The other half: the guard must not refuse a legitimate database.

    Uses `django_db_setup` rather than `db` on purpose - swapping `DATABASES`
    closes open connections, which would tear down a test's own transaction.
    """
    settings.ENFORCE_NON_TEST_DATABASE = True
    settings.DATABASES = {
        alias: {**config, "NAME": "raporo"}
        for alias, config in settings.DATABASES.items()
    }

    assert e100_errors() == []


def test_e100_names_the_offending_alias_and_stays_actionable(db, settings):
    settings.ENFORCE_NON_TEST_DATABASE = True

    error = e100_errors()[0]

    assert "default" in error.msg
    assert connection.settings_dict["NAME"] in error.msg
    assert error.hint
