"""The system checks that stop a later task from disarming invariant #1.

Each rogue model below is a shape a slice-2 author could plausibly write. The
checks have to reject them at startup, because none of them fails a query.
"""

import uuid

import pytest
from django.apps import apps as global_apps
from django.conf import settings as django_settings
from django.core.checks import Error, Tags, run_checks
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
