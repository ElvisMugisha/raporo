"""The system checks that stop a later task from disarming invariant #1.

Each rogue model below is a shape a slice-2 author could plausibly write. The
checks have to reject them at startup, because none of them fails a query.
"""

import importlib
import uuid

import pytest
from django.apps import apps as global_apps
from django.conf import settings as django_settings
from django.core.checks import Error, Tags, run_checks
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.db import connection, models
from django.db.models import Q
from django.db.models.functions import Lower
from django.test.utils import isolate_apps

from common import checks
from common.checks import audit_store_scoped_models, check_store_scoped_models
from common.managers import StoreScopedManager
from common.models import (
    ORG_FIELD,
    PUBLIC_ID_FIELD,
    AuditedModel,
    SoftDeleteModel,
    StoreScopedModel,
)
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
# E005 - uniqueness on a store-scoped table is *kinded*: per store, per
# organization across its stores, or a composite-FK target. Every other shape
# is a startup error. These tests walk the decision table in
# `docs/superpowers/specs/2026-09-02-schema-hardening-plan.md` §B.2 row by row:
# each row gets a test that fires and, where the row is an "OK", a test that
# proves a legitimate shape does not fire.
#
# About `org`: step 2 of the tenancy-hardening sequence moves the column onto
# `StoreScopedModel`. Declaring it unconditionally on the throwaway models
# below would turn that step into a fileful of import-time `FieldError`s (a
# local field cannot clash with an inherited abstract one), so
# `scoped_with_org()` declares it only while the base does not. E005 itself
# never asks the model whether `org` exists - it classifies a constraint by the
# column names the constraint references - which is what makes the rule correct
# in both worlds.
# --------------------------------------------------------------------------

BASE_CARRIES_ORG = any(field.name == ORG_FIELD for field in StoreScopedModel._meta.fields)


def scoped_with_org(name: str, *, constraints):
    """A throwaway concrete store-scoped model that has an `org` column.

    Carries its own premise assertions: exactly one `org` column whichever
    world we are in, and the constraints under test really reached `Meta`. A
    constraint test that silently declared nothing would otherwise pass.
    """
    attributes = {
        "__module__": __name__,
        "code": models.CharField(max_length=20),
        "number": models.CharField(max_length=20),
        "Meta": type("Meta", (), {"app_label": "testapp", "constraints": constraints}),
    }
    if not BASE_CARRIES_ORG:
        attributes[ORG_FIELD] = models.ForeignKey(
            "orgs.Organization", on_delete=models.PROTECT, related_name="+"
        )
    model = type(name, (StoreScopedModel,), attributes)

    columns = [field.name for field in model._meta.concrete_fields]
    assert columns.count(ORG_FIELD) == 1, columns
    assert [c.name for c in model._meta.constraints] == [c.name for c in constraints]
    return model


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


# --- the `public_id` surrogate: the one non-pk `unique=True` E005 allows ----


@isolate_apps("tests.testapp")
def test_the_inherited_public_id_surrogate_is_not_an_e005():
    """Premise first: the field really is `unique=True`, so this is not a test
    that passes because there was nothing to exempt."""

    class Plain(StoreScopedModel):
        class Meta:
            app_label = "testapp"

    assert Plain._meta.get_field(PUBLIC_ID_FIELD).unique is True

    assert audit_store_scoped_models([Plain]) == []


@isolate_apps("tests.testapp")
def test_a_second_unique_uuid_field_is_still_rejected():
    """The exemption is for `public_id`, not for UUID columns in general.

    Same type, same `editable=False`, same default shape - only the name
    differs. A globally unique `token` is precisely the cross-tenant existence
    oracle E005 exists for, and a type-only exemption would wave it through.
    """

    class Tokened(StoreScopedModel):
        token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

        class Meta:
            app_label = "testapp"

    assert "common.E005" in ids(audit_store_scoped_models([Tokened]))


def public_id_shaped(field):
    """A detached field, named as if `PublicIdModel` had declared it."""
    field.set_attributes_from_name(PUBLIC_ID_FIELD)
    return field


SURROGATE_LOOKALIKES = {
    "editable": models.UUIDField(default=uuid.uuid7, unique=True),
    "no default": models.UUIDField(editable=False, unique=True),
    "nullable": models.UUIDField(default=uuid.uuid7, editable=False, unique=True, null=True),
    "not a UUID column": models.CharField(
        max_length=36, default="", editable=False, unique=True
    ),
}


@pytest.mark.parametrize(
    "field", SURROGATE_LOOKALIKES.values(), ids=SURROGATE_LOOKALIKES
)
def test_only_the_real_surrogate_shape_is_exempt(field):
    """Each way the exemption could be widened, refused.

    Driven through the predicate rather than through the registry, and that is
    forced rather than chosen: a store-scoped model *cannot* redeclare
    `public_id`, because Django raises `FieldError` when a local field clashes
    with an inherited abstract one. There is no rogue model to write. The
    positive case below closes the loop by asserting the predicate accepts the
    field the shipped base actually declares, so weakening `PublicIdModel`
    breaks this pair too.
    """
    assert checks.is_public_id_surrogate(public_id_shaped(field)) is False


def test_the_shipped_surrogate_is_what_the_predicate_accepts():
    assert checks.is_public_id_surrogate(ScopedThing._meta.get_field(PUBLIC_ID_FIELD))


@isolate_apps("tests.testapp")
def test_a_named_unique_constraint_on_the_public_id_is_rejected():
    """The identifier's uniqueness is declared on the field, in one place.

    A `Meta.constraints` entry for it is a second declaration of the same fact
    that a subclass declaring its own `Meta` can silently drop - the
    `ScopedThingOwnMeta` accident - and it references no tenant column, so the
    generic rule would have to make an exception for it anyway.
    """

    class Reconstrained(StoreScopedModel):
        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=[PUBLIC_ID_FIELD], name="testapp_reconstrained_public_id_uniq"
                )
            ]

    errors = audit_store_scoped_models([Reconstrained])

    assert "common.E005" in ids(errors)
    assert any("PublicIdModel" in (error.hint or "") for error in errors)


# --- shape 2: unique per organization, across its stores -------------------


@isolate_apps("tests.testapp")
def test_a_per_org_across_stores_constraint_passes_when_it_says_so():
    """An invoice number unique across an organization's five shops - the
    legitimate shape the old E005 rejected outright."""
    model = scoped_with_org(
        "InvoiceNumbered",
        constraints=[
            models.UniqueConstraint(
                fields=[ORG_FIELD, "number"],
                condition=LIVE,
                name="testapp_invoicenumbered_unique_live_number_per_org",
            )
        ],
    )

    assert audit_store_scoped_models([model]) == []


@isolate_apps("tests.testapp")
def test_an_expression_based_per_org_constraint_resolves_and_passes():
    """`_expression_names` already walks `F()`/`Lower()` trees; the kinded rule
    must keep using it, or a functional org-wide key reads as tenant-less."""
    model = scoped_with_org(
        "CaseFolded",
        constraints=[
            models.UniqueConstraint(
                Lower("number"),
                ORG_FIELD,
                condition=LIVE,
                name="testapp_casefolded_unique_live_number_per_org",
            )
        ],
    )

    assert audit_store_scoped_models([model]) == []


@isolate_apps("tests.testapp")
def test_a_per_org_constraint_that_also_includes_store_is_rejected():
    """The name lies about what the database enforces.

    `_per_org` on a `(org, store, number)` index says "one per organization"
    while the index permits one per *store* - and the name is what an operator
    reads out of an `IntegrityError` and out of `psql`.
    """
    model = scoped_with_org(
        "NameLies",
        constraints=[
            models.UniqueConstraint(
                fields=[ORG_FIELD, "store", "number"],
                condition=LIVE,
                name="testapp_namelies_unique_live_number_per_org",
            )
        ],
    )

    errors = audit_store_scoped_models([model])

    assert "common.E005" in ids(errors)
    assert any("_per_org" in error.msg for error in errors)


@isolate_apps("tests.testapp")
def test_a_per_store_key_that_leads_with_org_passes():
    """The shape slice 2 will type on every table, and it must stay legal.

    Once `org` is on the base, `(org, store, name)` is the index the planner
    wants under RLS - measured 1.045 ms -> 0.146 ms when the tenant predicate
    becomes an Index Cond instead of a per-row Filter. It is a *per store* key
    that happens to lead with the organization, so it needs no `_per_org`
    declaration: only `org` *without* `store` is organization-wide.
    """
    model = scoped_with_org(
        "OrgLeadingPerStore",
        constraints=[
            models.UniqueConstraint(
                fields=[ORG_FIELD, "store", "code"],
                condition=LIVE,
                name="testapp_orgleadingperstore_unique_live_code",
            )
        ],
    )

    assert audit_store_scoped_models([model]) == []


@isolate_apps("tests.testapp")
def test_a_per_store_constraint_that_calls_itself_per_org_is_rejected():
    """The mirror check in its purest form: no `org` column in sight, and the
    name still claims organization-wide scope."""

    class MislabelledPerStore(StoreScopedModel):
        code = models.CharField(max_length=20)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["store", "code"],
                    condition=LIVE,
                    name="testapp_mislabelled_unique_live_code_per_org",
                )
            ]

    assert "common.E005" in ids(audit_store_scoped_models([MislabelledPerStore]))


@isolate_apps("tests.testapp")
def test_an_org_wide_constraint_must_declare_itself_in_its_name():
    """`UniqueConstraint(fields=["org", "name"])` is what a developer types by
    habit when they meant per store. Inferring intent from the shape turns that
    typo into a constraint that rejects a legitimate row in the second shop,
    in production, months later. The suffix is the declaration."""
    model = scoped_with_org(
        "UndeclaredOrgWide",
        constraints=[
            models.UniqueConstraint(
                fields=[ORG_FIELD, "number"],
                condition=LIVE,
                name="testapp_undeclaredorgwide_unique_live_number",
            )
        ],
    )

    assert "common.E005" in ids(audit_store_scoped_models([model]))


@isolate_apps("tests.testapp")
def test_an_org_wide_constraint_still_has_to_be_limited_to_live_rows():
    """The relaxation is the tenant column, not soft delete: a tombstone must
    not reserve an invoice number for good."""
    model = scoped_with_org(
        "OrgWideForever",
        constraints=[
            models.UniqueConstraint(
                fields=[ORG_FIELD, "number"],
                name="testapp_orgwideforever_unique_number_per_org",
            )
        ],
    )

    assert "common.E005" in ids(audit_store_scoped_models([model]))


# --- shape 3: the composite-FK target -------------------------------------


@isolate_apps("tests.testapp")
def test_a_composite_fk_target_on_org_is_exempt_from_the_live_rows_rule():
    """Two measured reasons, both in the plan: PostgreSQL refuses a partial
    unique index as a foreign-key target, and `(id, org)` grants no existence
    oracle the primary key did not already grant."""
    model = scoped_with_org(
        "FkTargetOrg",
        constraints=[
            models.UniqueConstraint(
                fields=["id", ORG_FIELD], name="testapp_fktargetorg_id_org_uniq"
            )
        ],
    )

    assert audit_store_scoped_models([model]) == []


@isolate_apps("tests.testapp")
def test_a_composite_fk_target_on_store_is_exempt_too():
    class FkTargetStore(StoreScopedModel):
        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["id", "store"], name="testapp_fktargetstore_id_store_uniq"
                )
            ]

    assert audit_store_scoped_models([FkTargetStore]) == []


@isolate_apps("tests.testapp")
def test_a_composite_fk_target_with_an_undeclared_name_is_rejected():
    """The exemption from the live-rows rule is granted to a *declared* FK
    target, so the declaration has to be on the database object."""
    model = scoped_with_org(
        "UnnamedFkTarget",
        constraints=[
            models.UniqueConstraint(
                fields=["id", ORG_FIELD], name="testapp_unnamedfktarget_unique_id_org"
            )
        ],
    )

    assert "common.E005" in ids(audit_store_scoped_models([model]))


@isolate_apps("tests.testapp")
def test_an_id_leading_constraint_over_a_non_tenant_column_is_rejected():
    """`(id, code)` is a redundant index, not a constraint: every constraint
    containing the primary key is already implied by the primary key. The name
    here is deliberately the blessed suffix, so what is refused is the shape."""

    class IdAndCode(StoreScopedModel):
        code = models.CharField(max_length=20)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["id", "code"], name="testapp_idandcode_id_store_uniq"
                )
            ]

    errors = audit_store_scoped_models([IdAndCode])

    assert "common.E005" in ids(errors)
    # The message matters here: with the blessed suffix on the name, a rule
    # that had lost the shape test would still report *a* naming error and this
    # test would pass for the wrong reason. Measured - that is what the
    # mutation run did before this assertion was added.
    assert any("implied by the primary key" in error.msg for error in errors)


@isolate_apps("tests.testapp")
def test_a_composite_fk_target_may_carry_nothing_but_its_tenant_column():
    """`(id, store, code)` wears the FK target's name while enforcing a
    business key that nobody conditioned on live rows."""

    class WideFkTarget(StoreScopedModel):
        code = models.CharField(max_length=20)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["id", "store", "code"],
                    name="testapp_widefktarget_id_store_uniq",
                )
            ]

    assert "common.E005" in ids(audit_store_scoped_models([WideFkTarget]))


@isolate_apps("tests.testapp")
def test_a_conditioned_composite_fk_target_is_rejected():
    """The one shape whose failure mode is a broken migration rather than a
    leak: `ADD FOREIGN KEY` against a partial unique index gives "there is no
    unique constraint matching given keys for referenced table" (measured in
    the plan, §A.5). Conditioning this constraint disarms its only purpose, so
    the exemption is refused rather than extended."""

    class ConditionedFkTarget(StoreScopedModel):
        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["id", "store"],
                    condition=LIVE,
                    name="testapp_conditionedfktarget_id_store_uniq",
                )
            ]

    assert "common.E005" in ids(audit_store_scoped_models([ConditionedFkTarget]))


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
# E007 - the denormalised `org` pointer, and the one spelling of it
#
# The column is what the composite key `(store_id, org_id) -> orgs_store (id,
# org_id)` references, so every way of weakening it disarms the only guard a
# data migration or a psql session cannot forget. Each rogue below is a change
# a slice-2 author could plausibly make for a good-sounding local reason.
# --------------------------------------------------------------------------


@isolate_apps("tests.testapp")
def test_a_model_that_removes_the_org_column_is_rejected():
    """The most direct disarm, and Django permits it: a field inherited from an
    *abstract* base can be removed by setting the name to None. Measured -
    `org = None` is not a FieldError, it is a table with no organization
    pointer and therefore no composite key."""

    class NoOrg(StoreScopedModel):
        org = None

        class Meta:
            app_label = "testapp"

    errors = audit_store_scoped_models([NoOrg])

    assert "common.E007" in ids(errors)
    assert any("same_org_fk" in error.msg for error in errors)


@isolate_apps("tests.testapp")
def test_a_model_that_makes_the_org_optional_is_rejected():
    """Nullable is not a weaker guarantee, it is *no* guarantee: PostgreSQL
    MATCH SIMPLE skips a composite foreign key entirely when either column is
    NULL."""

    class OptionalOrg(StoreScopedModel):
        org = models.ForeignKey(
            "orgs.Organization",
            null=True,
            on_delete=models.PROTECT,
            related_name="+",
            editable=False,
            db_index=False,
            db_constraint=False,
        )

        class Meta:
            app_label = "testapp"

    errors = audit_store_scoped_models([OptionalOrg])

    assert "common.E007" in ids(errors)
    assert any("MATCH SIMPLE" in error.msg for error in errors)


@isolate_apps("tests.testapp")
def test_a_model_that_makes_the_org_editable_is_rejected():
    class EditableOrg(StoreScopedModel):
        org = models.ForeignKey(
            "orgs.Organization",
            on_delete=models.PROTECT,
            related_name="+",
            db_index=False,
            db_constraint=False,
        )

        class Meta:
            app_label = "testapp"

    assert "common.E007" in ids(audit_store_scoped_models([EditableOrg]))


@isolate_apps("tests.testapp")
def test_a_model_that_adds_a_real_foreign_key_on_org_is_rejected():
    """`db_constraint=True` guarantees nothing the composite key does not, and
    takes `FOR KEY SHARE` on the organization row for every insert into every
    store-scoped table - so `create_store`'s row lock would block every sale in
    the organization."""

    class RealFkOrg(StoreScopedModel):
        org = models.ForeignKey(
            "orgs.Organization",
            on_delete=models.PROTECT,
            related_name="+",
            editable=False,
            db_index=False,
        )

        class Meta:
            app_label = "testapp"

    errors = audit_store_scoped_models([RealFkOrg])

    assert "common.E007" in ids(errors)
    assert any("db_constraint=True" in error.msg for error in errors)


@isolate_apps("tests.testapp")
def test_a_model_that_indexes_org_on_its_own_is_rejected():
    """The plausible one: "add an index for the planner". Every index that
    serves a tenant predicate leads with `org`, so a single-column one is a
    redundant prefix of all of them and pure write cost."""

    class IndexedOrg(StoreScopedModel):
        org = models.ForeignKey(
            "orgs.Organization",
            on_delete=models.PROTECT,
            related_name="+",
            editable=False,
            db_constraint=False,
            db_index=True,
        )

        class Meta:
            app_label = "testapp"

    assert "common.E007" in ids(audit_store_scoped_models([IndexedOrg]))


@isolate_apps("tests.testapp")
def test_a_model_that_exposes_a_reverse_accessor_from_the_org_is_rejected():
    """E004 fires here too; E007 repeats it where the column is described,
    because E004's message is about accessors and this one is about the
    column."""

    class AccessibleOrg(StoreScopedModel):
        org = models.ForeignKey(
            "orgs.Organization",
            on_delete=models.PROTECT,
            related_name="things",
            editable=False,
            db_index=False,
            db_constraint=False,
        )

        class Meta:
            app_label = "testapp"

    errors = audit_store_scoped_models([AccessibleOrg])

    assert "common.E007" in ids(errors)
    assert "common.E004" in ids(errors)


@isolate_apps("tests.testapp")
def test_a_model_that_repoints_the_org_fk_is_rejected():
    class WrongTarget(StoreScopedModel):
        org = models.ForeignKey(
            "orgs.Store",
            on_delete=models.PROTECT,
            related_name="+",
            editable=False,
            db_index=False,
            db_constraint=False,
        )

        class Meta:
            app_label = "testapp"

    assert "common.E007" in ids(audit_store_scoped_models([WrongTarget]))


@isolate_apps("tests.testapp")
def test_a_model_that_spells_the_column_organization_is_rejected():
    """Not a store-scoped-model rule: `organization` is refused on *any* model,
    because two spellings mean composite keys whose two sides name the same
    concept differently - and the short one is frozen inside four shipped,
    SHA-256-pinned statements."""

    class LongSpelling(SoftDeleteModel, AuditedModel):
        organization = models.ForeignKey(
            "orgs.Organization", on_delete=models.PROTECT, related_name="+"
        )

        class Meta:
            app_label = "testapp"

    errors = audit_store_scoped_models([LongSpelling])

    # Premise: this model is not store-scoped, so nothing else here looks at it.
    assert not issubclass(LongSpelling, StoreScopedModel)
    assert ids(errors) == {"common.E007"}


def test_the_real_models_carry_the_org_column_in_the_required_shape():
    """The other direction, on the models that actually ship: E007 must not
    fire on the shape `StoreScopedModel` declares, or nobody could boot."""
    assert [
        error
        for error in check_store_scoped_models(None)
        if error.id == "common.E007"
    ] == []


# --------------------------------------------------------------------------
# E005, re-verified now that the `org` column exists
#
# E005 was built as a *kinded* rule whose classification is purely over the
# column names a constraint references, so two of its three shapes - the
# org-rooted `_per_org` key and the `(id, org)` composite-FK target - were
# unreachable while no store-scoped model had an `org` column: Django's own
# `models.E012` rejected the constraint first, for naming a field that does not
# exist. They are reachable now, with no change to `common/checks.py`. That was
# the claim; these execute it.
# --------------------------------------------------------------------------


@isolate_apps("tests.testapp")
def test_the_org_column_is_now_inherited_rather_than_declared_per_test():
    """Premise for everything below: `scoped_with_org()` declares nothing of its
    own any more, so the shapes it builds are the shapes real models will have.
    """
    assert BASE_CARRIES_ORG is True

    model = scoped_with_org(
        "InheritsOrg",
        constraints=[
            models.UniqueConstraint(
                fields=[ORG_FIELD, "number"],
                condition=LIVE,
                name="testapp_inheritsorg_unique_live_number_per_org",
            )
        ],
    )
    field = model._meta.get_field(ORG_FIELD)

    assert field.editable is False
    assert field.db_constraint is False
    assert audit_store_scoped_models([model]) == []


@isolate_apps("tests.testapp")
def test_an_org_rooted_key_over_the_real_column_is_accepted():
    """Shape 2, now over the inherited column: an invoice number unique across
    an organization's five shops."""

    class RealInvoice(StoreScopedModel):
        number = models.CharField(max_length=20)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=[ORG_FIELD, "number"],
                    condition=LIVE,
                    name="testapp_realinvoice_unique_live_number_per_org",
                )
            ]

    # Premise: the column is the inherited one, not a local declaration.
    assert RealInvoice._meta.get_field(ORG_FIELD).db_constraint is False
    assert audit_store_scoped_models([RealInvoice]) == []


@isolate_apps("tests.testapp")
def test_an_id_org_composite_fk_target_over_the_real_column_is_accepted():
    """Shape 3, now over the inherited column - and still exempt from the
    live-rows rule, because PostgreSQL refuses a partial unique index as a
    foreign-key target."""

    class RealParent(StoreScopedModel):
        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["id", ORG_FIELD], name="testapp_realparent_id_org_uniq"
                )
            ]

    assert audit_store_scoped_models([RealParent]) == []


@isolate_apps("tests.testapp")
def test_a_bare_key_is_still_refused_now_that_the_column_exists():
    """The relaxation above must not have widened into "anything goes": a
    constraint naming neither tenant column is still enforced across every
    tenant, and `full_clean()` would report another tenant's value as taken."""

    class StillGlobal(StoreScopedModel):
        number = models.CharField(max_length=20)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["number"],
                    condition=LIVE,
                    name="testapp_stillglobal_unique_live_number",
                )
            ]

    errors = audit_store_scoped_models([StillGlobal])

    assert "common.E005" in ids(errors)
    assert any("every tenant" in error.msg for error in errors)


@isolate_apps("tests.testapp")
def test_an_org_rooted_key_that_does_not_declare_itself_is_still_refused():
    """The half of shape 2 that is easy to lose in a relaxation: `org` without
    `store` is organization-wide, and that has to be *declared* in the name,
    because it is also what a per-store key looks like when `org` was typed by
    habit."""

    class UndeclaredOrgWide(StoreScopedModel):
        number = models.CharField(max_length=20)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=[ORG_FIELD, "number"],
                    condition=LIVE,
                    name="testapp_undeclaredorgwide_unique_live_number",
                )
            ]

    assert "common.E005" in ids(audit_store_scoped_models([UndeclaredOrgWide]))


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
    the production flag on here *is* the production misconfiguration.

    One error per offending alias, rather than "exactly one error": how many
    aliases the test runner has renamed to `test_raporo` depends on which
    aliases the session has touched (`settings.DATABASES[alias]` and
    `connections[alias].settings_dict` are the same dict, so setting up the
    `migrator` alias for a multi-connection test renames it here too). Pinning
    the count made this test fail because an unrelated test elsewhere in the
    suite opened a second connection.
    """
    assert connection.settings_dict["NAME"].startswith("test_")  # premise
    test_named = [
        alias
        for alias, config in settings.DATABASES.items()
        if str(config.get("NAME") or "").startswith("test_")
    ]
    assert test_named  # premise: there is something to report
    settings.ENFORCE_NON_TEST_DATABASE = True

    errors = e100_errors()

    assert [error.id for error in errors] == [E100] * len(test_named)
    assert all("test_" in error.msg for error in errors)
    assert {alias for alias in test_named if any(alias in e.msg for e in errors)} == set(
        test_named
    )


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


# --------------------------------------------------------------------------
# E101 - SECURE_SSL_REDIRECT with no SECURE_PROXY_SSL_HEADER is a redirect loop
#
# Behind a TLS-terminating proxy Django sees `http`, redirects to `https`, and
# the proxy forwards the retry as `http` again: an infinite loop and a total
# outage on the first deploy. The production hostname is undecided, so the
# header's *value* cannot be written down here - the refusal can. Same shape as
# E100: `Tags.security` (never `Tags.database`, which a bare `manage.py check`
# drops), pure `settings` inspection, no database connection, and every test
# below drives `run_checks()` rather than the function, because a direct call is
# exactly the tautology that shipped E100 inert for two review rounds.
#
# The gate is the condition itself: dev and test settings never set
# `SECURE_SSL_REDIRECT`, so the check is silent in the suite without a second
# flag to keep in step.
# --------------------------------------------------------------------------

E101 = "common.E101"


def e101_errors(**kwargs) -> list[Error]:
    """The E101 errors `manage.py check` (no arguments) would report."""
    return [error for error in run_checks(**kwargs) if error.id == E101]


def prod_settings(monkeypatch, **environment):
    """`config.settings.prod`, re-imported under a controlled environment.

    Reading the real module rather than restating its values is the point: a
    test that asserted "True and None fail the check" would pass while prod
    settings said something else entirely.
    """
    monkeypatch.setenv("DJANGO_MEDIA_ROOT", "/srv/raporo/media")
    for name in ("DJANGO_SECURE_PROXY_SSL_HEADER",):
        if name not in environment:
            monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return importlib.reload(importlib.import_module("config.settings.prod"))


def test_e101_fires_when_ssl_redirect_has_no_proxy_header(settings):
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_PROXY_SSL_HEADER = None

    errors = e101_errors()

    assert [error.id for error in errors] == [E101]
    assert "SECURE_PROXY_SSL_HEADER" in errors[0].msg
    assert errors[0].hint


def test_manage_py_check_refuses_to_boot_into_a_redirect_loop(settings):
    """End to end through the management command a pre-boot step runs: a
    non-zero exit, not a warning in a log. Django's own `security.W008` is a
    `--deploy`-only *warning*, and a warning does not fail `check`."""
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_PROXY_SSL_HEADER = None

    with pytest.raises(SystemCheckError) as exc:
        call_command("check")

    assert E101 in str(exc.value)


def test_e101_is_not_tagged_database_because_that_tag_is_skipped_by_default(settings):
    """The E100 regression, pinned for its sibling: a database-tagged check is
    invisible to a bare `manage.py check`."""
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_PROXY_SSL_HEADER = None

    assert Tags.database not in checks.check_ssl_redirect_trusts_a_proxy_header.tags
    assert Tags.security in checks.check_ssl_redirect_trusts_a_proxy_header.tags
    assert e101_errors(databases=None) == e101_errors(databases=["default"])


def test_e101_is_silent_in_the_suites_own_settings():
    """The gate: dev and test settings never redirect, so the check cannot fire
    inside the suite and needs no second flag to keep in step."""
    assert not getattr(django_settings, "SECURE_SSL_REDIRECT", False)  # premise

    assert e101_errors() == []


def test_e101_is_silent_once_a_proxy_header_is_declared(settings):
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

    assert e101_errors() == []


def test_e101_refuses_a_proxy_header_that_is_not_a_two_tuple(settings):
    """`SECURE_PROXY_SSL_HEADER = "HTTP_X_FORWARDED_PROTO"` is the plausible
    typo, and Django unpacks the setting without checking it: it would raise
    `ValueError` on the first request instead of at boot."""
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_PROXY_SSL_HEADER = "HTTP_X_FORWARDED_PROTO"

    assert [error.id for error in e101_errors()] == [E101]


def test_prod_settings_as_shipped_refuse_to_boot(monkeypatch, settings):
    """The live outage, closed as a refusal rather than a guessed value."""
    prod = prod_settings(monkeypatch)

    assert prod.SECURE_SSL_REDIRECT is True  # premise
    assert prod.SECURE_PROXY_SSL_HEADER is None
    settings.SECURE_SSL_REDIRECT = prod.SECURE_SSL_REDIRECT
    settings.SECURE_PROXY_SSL_HEADER = prod.SECURE_PROXY_SSL_HEADER

    assert [error.id for error in e101_errors()] == [E101]


def test_prod_settings_accept_the_header_the_operator_supplies(monkeypatch, settings):
    prod = prod_settings(
        monkeypatch, DJANGO_SECURE_PROXY_SSL_HEADER="HTTP_X_FORWARDED_PROTO,https"
    )

    assert prod.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
    settings.SECURE_SSL_REDIRECT = prod.SECURE_SSL_REDIRECT
    settings.SECURE_PROXY_SSL_HEADER = prod.SECURE_PROXY_SSL_HEADER

    assert e101_errors() == []


@pytest.mark.parametrize(
    "value",
    [
        "HTTP_X_FORWARDED_PROTO",  # no value half
        "X_FORWARDED_PROTO,https",  # not a WSGI META key
        "HTTP_X_FORWARDED_PROTO,https,extra",  # three parts
        ",https",  # no header name
        "HTTP_X_FORWARDED_PROTO,",  # no expected value
        "HTTP_X FORWARDED_PROTO,https",  # whitespace in the key
        "HTTP_X_FORWARDED_PROTO,ht tps",  # whitespace in the value
    ],
)
def test_prod_settings_refuse_a_malformed_proxy_header(monkeypatch, value):
    """Input validated at the boundary: a half-written header is a silent
    `ValueError` on the first request, or a `None` that reads as "unset"."""
    with pytest.raises(ImproperlyConfigured):
        prod_settings(monkeypatch, DJANGO_SECURE_PROXY_SSL_HEADER=value)


def test_prod_settings_treat_an_empty_proxy_header_as_unset(monkeypatch, settings):
    """Fail closed: a blank value is "not configured", so E101 still fires
    rather than a `("", "")` tuple that would trust every request."""
    prod = prod_settings(monkeypatch, DJANGO_SECURE_PROXY_SSL_HEADER="   ")

    assert prod.SECURE_PROXY_SSL_HEADER is None
    settings.SECURE_SSL_REDIRECT = prod.SECURE_SSL_REDIRECT
    settings.SECURE_PROXY_SSL_HEADER = prod.SECURE_PROXY_SSL_HEADER

    assert [error.id for error in e101_errors()] == [E101]


def test_the_health_probe_is_exempt_from_the_ssl_redirect(monkeypatch, settings, client):
    """The same outage one layer out, and the reason the exemption exists.

    A container health probe reaches the app over plain HTTP on loopback and
    sends no `X-Forwarded-Proto`, so `SECURE_SSL_REDIRECT` answers it with a
    301 to a `https://127.0.0.1` nothing is listening on. The probe never
    succeeds, the container is never healthy, and the deploy fails with a
    perfectly working application inside it.
    """
    prod = prod_settings(monkeypatch)
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_REDIRECT_EXEMPT = prod.SECURE_REDIRECT_EXEMPT

    assert client.get("/healthz").status_code == 200
    # Premise: the redirect really is on, so the line above is not vacuous.
    assert client.get("/i18n/setlang/").status_code == 301
