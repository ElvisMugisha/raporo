"""orgs: organization, stores, roles + permission catalog, memberships."""

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.db.models import Q

from apps.orgs.models import (
    Membership,
    Organization,
    Role,
    Store,
    StoreAccess,
    organization_logo_path,
)
from apps.orgs.permissions import PERMISSIONS, PRESETS
from common.managers import HardDeleteForbidden

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# Organization
# --------------------------------------------------------------------------


def test_organization_defaults_are_rwanda_first(org):
    assert org.base_currency == "RWF"
    assert org.timezone == "Africa/Kigali"
    assert org.brand == {}


def test_organization_slug_is_unique(org):
    with pytest.raises(ValidationError):
        Organization(name="Copycat", slug=org.slug).full_clean()


def test_organization_slug_uniqueness_is_enforced_by_the_database(org):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Organization.objects.create(name="Copycat", slug=org.slug)


def test_a_soft_deleted_organization_releases_its_slug(org, actor):
    """A plain unique=True here would 500 the next signup with that name."""
    org.soft_delete(by=actor)

    Organization(name="New Owner", slug=org.slug).full_clean()
    reborn = Organization.objects.create(name="New Owner", slug=org.slug)

    assert reborn.pk != org.pk


@pytest.mark.parametrize("bad", ["rwf", "RW", "RWFX", "R1F", ""])
def test_organization_rejects_a_bogus_currency_code(bad):
    with pytest.raises(ValidationError) as exc:
        Organization(name="X", slug="x", base_currency=bad).full_clean()

    assert "base_currency" in exc.value.error_dict


@pytest.mark.parametrize("bad", ["Africa/Kigaly", "CAT", "", "UTC+2"])
def test_organization_rejects_an_unknown_timezone(bad):
    """Period boundaries depend on this being a real zoneinfo key."""
    with pytest.raises(ValidationError) as exc:
        Organization(name="X", slug="x", timezone=bad).full_clean()

    assert "timezone" in exc.value.error_dict


def test_organization_accepts_another_real_timezone():
    """Another zone Raporo reports in - not merely another zone that exists.

    `Europe/Brussels` used to stand here and it no longer passes: the reporting
    timezone is a curated allowlist now (Rwanda plus the East African
    neighbours), because `zoneinfo.available_timezones()` accepted `localtime`
    and `Etc/GMT+5`. The full contract, and every value watched being refused,
    is in `tests/test_timezone_allowlist.py`.
    """
    Organization(name="X", slug="x", timezone="Africa/Nairobi").full_clean()


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def test_store_name_is_unique_within_an_org(org, store):
    with pytest.raises(ValidationError):
        Store(org=org, name=store.name).full_clean()


def test_store_name_uniqueness_is_enforced_by_the_database(org, store):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Store.objects.create(org=org, name=store.name)


def test_the_same_store_name_is_fine_in_another_org(other_org, store):
    Store(org=other_org, name=store.name).full_clean()


def test_a_soft_deleted_store_name_can_be_reused(org, store, actor):
    store.soft_delete(by=actor)

    Store(org=org, name="Main").full_clean()  # the constraint only covers live rows
    reborn = Store.objects.create(org=org, name="Main")

    assert reborn.pk != store.pk
    assert Store.objects.filter(org=org).count() == 1


def test_store_carries_the_only_org_pointer():
    """Invariant #1: business data reaches its org through the store."""
    assert Store._meta.get_field("org").related_model is Organization


# --------------------------------------------------------------------------
# Roles & permission catalog
# --------------------------------------------------------------------------


EXPECTED_PERMISSIONS = {
    "member.manage",
    "role.manage",
    "invite.create",
    "store.manage",
    "store.access_all",
    "sale.record",
    "sale.below_floor_override",
    "stock.restock",
    "stock.write_off",
    "expense.record",
    "cycle.manage",
    "report.generate",
    "audit.view",
}


def test_permission_catalog_is_exactly_the_agreed_set():
    assert PERMISSIONS == EXPECTED_PERMISSIONS
    assert isinstance(PERMISSIONS, frozenset)


def test_presets_are_owner_manager_seller():
    assert set(PRESETS) == {"Owner", "Manager", "Seller"}
    assert PRESETS["Owner"] == PERMISSIONS
    assert PRESETS["Seller"] == frozenset({"sale.record"})
    for name, codes in PRESETS.items():
        assert codes <= PERMISSIONS, name


def test_role_rejects_an_unknown_permission_code(org):
    role = Role(org=org, name="Weird", permissions=["sale.record", "sale.delete_everything"])

    with pytest.raises(ValidationError) as exc:
        role.full_clean()

    assert "permissions" in exc.value.error_dict


@pytest.mark.parametrize(
    "bad",
    ["sale.record", {"sale.record": True}, [1], ["sale.record", "sale.record"]],
)
def test_role_permissions_must_be_a_list_of_unique_codes(org, bad):
    with pytest.raises(ValidationError) as exc:
        Role(org=org, name="Weird", permissions=bad).full_clean()

    assert "permissions" in exc.value.error_dict


def test_role_accepts_catalog_codes_and_answers_has(org):
    role = Role(org=org, name="Seller", permissions=sorted(PRESETS["Seller"]))
    role.full_clean()

    assert role.has("sale.record")
    assert not role.has("store.manage")
    assert not role.has("")


def test_role_name_is_unique_within_an_org(org):
    Role.objects.create(org=org, name="Owner", permissions=sorted(PERMISSIONS), is_preset=True)

    with pytest.raises(ValidationError):
        Role(org=org, name="Owner").full_clean()


def test_role_defaults_to_no_permissions_and_not_preset(org):
    role = Role.objects.create(org=org, name="Empty")

    assert role.permissions == []
    assert role.is_preset is False


# --------------------------------------------------------------------------
# Membership & store access
# --------------------------------------------------------------------------


@pytest.fixture
def owner_role(org):
    return Role.objects.create(
        org=org, name="Owner", permissions=sorted(PERMISSIONS), is_preset=True
    )


def test_membership_is_unique_per_user_and_org(actor, org, owner_role):
    Membership.objects.create(user=actor, org=org, role=owner_role)

    with pytest.raises(ValidationError):
        Membership(user=actor, org=org, role=owner_role).full_clean()


def test_membership_uniqueness_is_enforced_by_the_database(actor, org, owner_role):
    Membership.objects.create(user=actor, org=org, role=owner_role)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Membership.objects.create(user=actor, org=org, role=owner_role)


def test_membership_rejects_a_role_from_another_org(actor, org, other_org):
    foreign_role = Role.objects.create(org=other_org, name="Owner", permissions=[])

    with pytest.raises(ValidationError) as exc:
        Membership(user=actor, org=org, role=foreign_role).full_clean()

    assert "role" in exc.value.error_dict


def test_store_access_rejects_a_store_from_another_org(actor, org, owner_role, foreign_store):
    membership = Membership.objects.create(user=actor, org=org, role=owner_role)

    with pytest.raises(ValidationError) as exc:
        StoreAccess(membership=membership, store=foreign_store).full_clean()

    assert "store" in exc.value.error_dict


def test_store_access_is_unique_per_membership_and_store(actor, org, owner_role, store):
    membership = Membership.objects.create(user=actor, org=org, role=owner_role)
    StoreAccess.objects.create(membership=membership, store=store)

    with pytest.raises(ValidationError):
        StoreAccess(membership=membership, store=store).full_clean()


def test_store_access_accepts_a_store_in_the_same_org(actor, org, owner_role, store):
    membership = Membership.objects.create(user=actor, org=org, role=owner_role)

    StoreAccess(membership=membership, store=store).full_clean()


# --------------------------------------------------------------------------
# Cross-cutting: every orgs table is soft-deletable and audited
# --------------------------------------------------------------------------


ORG_MODELS = [Organization, Store, Role, Membership, StoreAccess]

#: (model, constraint name) for every "unique among live rows" rule. A plain
#: unique constraint here would make a soft-deleted row block its own name for
#: good, and `_default_manager` cannot see deleted rows, so `full_clean()` would
#: never notice - hence this structural assertion.
LIVE_UNIQUE_CONSTRAINTS = [
    (Organization, "orgs_organization_unique_live_slug"),
    (Store, "orgs_store_unique_live_name_per_org"),
    (Role, "orgs_role_unique_live_name_per_org"),
    (Membership, "orgs_membership_unique_live_user_per_org"),
    # One live membership per user (schema plan §J). This tuple *is* the
    # check: it asserts the constraint exists AND that its condition is
    # exactly `LIVE`, so a future migration that drops either turns the suite
    # red. A system check would be the wrong tool - "this one model declares
    # this one constraint" is a test, not a rule over a class of models.
    (Membership, "orgs_membership_unique_live_user"),
    (StoreAccess, "orgs_storeaccess_unique_live_membership_store"),
]


@pytest.mark.parametrize(
    ("model", "name"),
    LIVE_UNIQUE_CONSTRAINTS,
    ids=[f"{m.__name__}.{n}" for m, n in LIVE_UNIQUE_CONSTRAINTS],
)
def test_unique_constraints_only_cover_live_rows(model, name):
    constraint = next(c for c in model._meta.constraints if c.name == name)

    assert constraint.condition == Q(deleted_at__isnull=True)


@pytest.mark.parametrize("model", ORG_MODELS, ids=[m.__name__ for m in ORG_MODELS])
def test_orgs_models_are_soft_deletable_and_audited(model):
    field_names = {f.name for f in model._meta.get_fields()}

    assert {"created_at", "created_by", "updated_at", "updated_by"} <= field_names
    assert {"deleted_at", "deleted_by"} <= field_names
    assert hasattr(model, "all_objects")


@pytest.mark.parametrize("model", ORG_MODELS, ids=[m.__name__ for m in ORG_MODELS])
def test_orgs_models_refuse_hard_delete(model, org):
    with pytest.raises(HardDeleteForbidden):
        model.objects.all().delete()


def test_orgs_models_are_not_store_scoped(org, store):
    """Store/Role/Membership are org-level: querying them needs no store."""
    assert Store.objects.filter(org=org).count() == 1
    assert Role.objects.count() == 0


# --------------------------------------------------------------------------
# Store branding (slice-4 semantics; fields only for now)
# --------------------------------------------------------------------------


def test_store_branding_defaults_to_inheriting_the_org(store):
    assert store.use_own_branding is False
    assert store.brand == {}


def test_store_brand_must_be_a_mapping(org):
    with pytest.raises(ValidationError) as exc:
        Store(org=org, name="Odd", brand=["not", "a", "mapping"]).full_clean()

    assert "brand" in exc.value.error_dict


# --------------------------------------------------------------------------
# Cross-organization integrity, enforced by the database
# --------------------------------------------------------------------------


def test_membership_cannot_take_a_role_from_another_org(actor, org, other_org):
    """Reproduced: `objects.create()` never calls `clean()`, so this got in."""
    foreign_role = Role.objects.create(org=other_org, name="Owner", permissions=[])

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Membership.objects.create(user=actor, org=org, role=foreign_role)


def test_store_access_cannot_reach_a_store_in_another_org(
    actor, org, owner_role, foreign_store
):
    membership = Membership.objects.create(user=actor, org=org, role=owner_role)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            StoreAccess.objects.create(membership=membership, store=foreign_store)


def test_store_access_derives_its_org_from_the_membership(actor, org, owner_role, store):
    membership = Membership.objects.create(user=actor, org=org, role=owner_role)

    access = StoreAccess.objects.create(membership=membership, store=store)

    assert access.org_id == org.pk


def test_store_access_full_clean_does_not_demand_the_derived_org(
    actor, org, owner_role, store
):
    membership = Membership.objects.create(user=actor, org=org, role=owner_role)

    StoreAccess(membership=membership, store=store).full_clean()


def test_the_same_org_composite_keys_exist_in_postgres(db):
    """The guarantee lives in the schema, not only in `clean()`."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT conname FROM pg_constraint
            WHERE contype = 'f' AND conname IN (
                'orgs_membership_role_same_org_fk',
                'orgs_storeaccess_membership_same_org_fk',
                'orgs_storeaccess_store_same_org_fk',
                'audit_auditlog_store_same_org_fk'
            )
            ORDER BY conname
            """
        )
        found = [row[0] for row in cursor.fetchall()]

    assert found == [
        "audit_auditlog_store_same_org_fk",
        "orgs_membership_role_same_org_fk",
        "orgs_storeaccess_membership_same_org_fk",
        "orgs_storeaccess_store_same_org_fk",
    ]


# --------------------------------------------------------------------------
# C3 - the logo upload cannot become stored XSS
# --------------------------------------------------------------------------


def png_bytes(size=(2, 2)) -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_real_png_is_accepted(org):
    org.logo = SimpleUploadedFile("logo.png", png_bytes(), content_type="image/png")

    org.full_clean()


def test_an_svg_disguised_as_a_png_is_rejected(org):
    """The dangerous case: an SVG carries script, and served from our own origin
    that is stored XSS."""
    org.logo = SimpleUploadedFile(
        "logo.png", b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>",
        content_type="image/png",
    )

    with pytest.raises(ValidationError) as exc:
        org.full_clean()

    assert "logo" in exc.value.error_dict


def test_an_svg_extension_is_rejected(org):
    org.logo = SimpleUploadedFile("logo.svg", png_bytes(), content_type="image/svg+xml")

    with pytest.raises(ValidationError) as exc:
        org.full_clean()

    assert "logo" in exc.value.error_dict


def test_an_oversized_image_is_rejected(org):
    org.logo = SimpleUploadedFile(
        "logo.png", b"x" * (2 * 1024 * 1024 + 1), content_type="image/png"
    )

    with pytest.raises(ValidationError) as exc:
        org.full_clean()

    assert "logo" in exc.value.error_dict


def test_the_stored_logo_filename_is_random(org):
    """The uploaded name is attacker-controlled; only the extension survives."""
    path = organization_logo_path(org, "../../../etc/passwd.png")

    assert path.startswith("org-logos/")
    assert path.endswith(".png")
    assert ".." not in path
    assert path != organization_logo_path(org, "logo.png")


def test_media_root_is_outside_the_source_tree():
    assert not str(settings.MEDIA_ROOT).startswith(str(settings.BASE_DIR))
    assert settings.FILE_UPLOAD_MAX_MEMORY_SIZE <= 2 * 1024 * 1024


# --------------------------------------------------------------------------
# One live membership per user (schema plan §J)
# --------------------------------------------------------------------------


def test_a_user_may_hold_only_one_live_membership(actor, org, owner_role, other_org):
    """Elvis's ruling: a user belongs to exactly one organization.

    `create()` never calls `clean()`, so this is the database talking - which
    is the only layer that holds under a race.
    """
    Membership.objects.create(user=actor, org=org, role=owner_role)
    foreign_role = Role.objects.create(org=other_org, name="Owner", permissions=[])

    with pytest.raises(IntegrityError) as exc:
        with transaction.atomic():
            Membership.objects.create(user=actor, org=other_org, role=foreign_role)

    assert "orgs_membership_unique_live_user" in str(exc.value)


def test_the_two_membership_constraints_name_two_different_incidents(
    actor, org, owner_role, other_org
):
    """A same-org duplicate and a second-org membership are different events.

    One is a double-invite (benign, retry-safe); the other is a policy refusal
    that needs a human. The names are the only thing an on-call engineer has,
    so both must be reachable. Precedence comes from index creation order and
    flips on a database restored from `pg_dump` (which emits indexes
    alphabetically) - so this asserts that each name *can* fire, never which
    one wins.
    """
    Membership.objects.create(user=actor, org=org, role=owner_role)

    with pytest.raises(IntegrityError) as same_org:
        with transaction.atomic():
            Membership.objects.create(user=actor, org=org, role=owner_role)

    assert "orgs_membership_unique_live_user_per_org" in str(same_org.value)


def test_a_soft_deleted_membership_releases_the_user(
    actor, org, owner_role, other_org, other_actor
):
    membership = Membership.objects.create(user=actor, org=org, role=owner_role)
    membership.soft_delete(by=other_actor)
    foreign_role = Role.objects.create(org=other_org, name="Owner", permissions=[])

    Membership.objects.create(user=actor, org=other_org, role=foreign_role)

    assert Membership.objects.filter(user=actor).count() == 1
    assert Membership.all_objects.filter(user=actor).count() == 2


def test_leaving_and_rejoining_the_first_org_works(actor, org, owner_role, other_org, other_actor):
    foreign_role = Role.objects.create(org=other_org, name="Owner", permissions=[])

    first = Membership.objects.create(user=actor, org=org, role=owner_role)
    first.soft_delete(by=other_actor)
    second = Membership.objects.create(user=actor, org=other_org, role=foreign_role)

    # While B is live, re-joining A is refused: leaving B comes first.
    with pytest.raises(IntegrityError) as exc:
        with transaction.atomic():
            Membership.objects.create(user=actor, org=org, role=owner_role)
    assert "orgs_membership_unique_live_user" in str(exc.value)

    second.soft_delete(by=other_actor)
    third = Membership.objects.create(user=actor, org=org, role=owner_role)

    assert Membership.objects.filter(user=actor).count() == 1
    assert {m.pk for m in Membership.all_objects.filter(user=actor)} == {
        first.pk,
        second.pk,
        third.pk,
    }


def test_the_one_org_violation_reports_on_the_user_field(actor, org, owner_role, other_org):
    """`violation_error_code="unique"` is load-bearing, not cosmetic.

    A constraint carrying a `condition` always raises the bare message, and
    `validate_constraints()` only re-files it onto a field when the code is
    `"unique"` and the constraint names exactly one field. Without the code the
    message lands in `__all__` as a form-wide banner instead of beside the
    input the operator got wrong.
    """
    Membership.objects.create(user=actor, org=org, role=owner_role)
    foreign_role = Role.objects.create(org=other_org, name="Owner", permissions=[])

    with pytest.raises(ValidationError) as exc:
        Membership(user=actor, org=other_org, role=foreign_role).full_clean()

    assert "user" in exc.value.error_dict
    assert "already belongs to an organization" in str(exc.value.error_dict["user"][0])


def test_a_same_org_duplicate_still_reports_the_specific_message(actor, org, owner_role):
    """Makes §J.2's ruling load-bearing: drop the per-org constraint and this
    test goes red, because the one-org message says nothing about a double
    invite into the same organization."""
    Membership.objects.create(user=actor, org=org, role=owner_role)

    with pytest.raises(ValidationError) as exc:
        Membership(user=actor, org=org, role=owner_role).full_clean()

    messages = [str(m) for m in exc.value.messages]
    assert any("already a member of this organization" in m for m in messages)


def test_the_one_org_index_is_partial_in_postgres(db):
    """A conditional UniqueConstraint is a partial index, so it is in
    `pg_indexes` and NOT in `pg_constraint`. Assert the predicate itself:
    without it, a soft-deleted membership would hold the user for ever."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'orgs_membership_unique_live_user'"
        )
        row = cursor.fetchone()
        cursor.execute(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conname = 'orgs_membership_unique_live_user'"
        )
        in_pg_constraint = cursor.fetchone()[0]

    assert row is not None, "the one-org unique index does not exist"
    assert "WHERE (deleted_at IS NULL)" in row[0]
    assert in_pg_constraint == 0
