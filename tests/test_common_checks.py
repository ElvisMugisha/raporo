"""The system checks that stop a later task from disarming invariant #1.

Each rogue model below is a shape a slice-2 author could plausibly write. The
checks have to reject them at startup, because none of them fails a query.
"""

from django.core.checks import Error
from django.db import models
from django.db.models import Q
from django.test.utils import isolate_apps

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


def test_the_check_honours_the_app_configs_it_is_given():
    """`manage.py check <app>` must not report on unrelated apps."""
    from django.apps import apps

    accounts_only = [apps.get_app_config("accounts")]

    assert check_store_scoped_models(accounts_only) == []


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
