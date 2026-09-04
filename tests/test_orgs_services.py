"""Task 4: `register_owner`, the store roster, and member management.

The two tests that need real connections rather than reasoning are at the
bottom: the 1-5 store cap under two concurrent creates, and the proof that
`create_store` actually takes the organization row lock. A cap enforced by a
count is not enforced at all unless something serialises the count.
"""

import threading

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connections, transaction

from apps.audit.models import AuditLog
from apps.orgs.exceptions import (
    LastStore,
    PermissionRequired,
    PrivilegeEscalation,
    StoreLimitReached,
    StoreNotEmpty,
    StoreNotPermitted,
    WouldLockOutTheOrganization,
)
from apps.orgs.models import (
    MAX_STORES_PER_ORG,
    Membership,
    Organization,
    Role,
    Store,
    StoreAccess,
)
from apps.orgs.permissions import PERMISSIONS, PRESETS, REPORT_GENERATE, STORE_ACCESS_ALL
from apps.orgs.services import (
    AccessAllHoldsNoRows,
    create_store,
    grant_store_access,
    membership_for,
    permitted_stores,
    provisioning,
    register_owner,
    revoke_store_access,
    set_membership_role,
    soft_delete_store,
)
from tests.testapp.models import Sale

pytestmark = pytest.mark.django_db

FOUNDER = {
    "username": "eva",
    "email": "eva@example.rw",
    "phone": "250788000010",
    "password": "S3cure!passphrase",
    "org_name": "Eva Shop",
}


def make_user(suffix):
    return get_user_model().objects.create_user(
        username=f"u{suffix}",
        email=f"u{suffix}@example.rw",
        phone=f"2507880001{suffix:02d}",
        password="S3cure!passphrase",
    )


@pytest.fixture
def shop(db):
    return register_owner(**FOUNDER)


# --------------------------------------------------------------------------
# register_owner
# --------------------------------------------------------------------------


def test_register_owner_creates_everything_in_one_go(shop):
    assert shop.org.name == "Eva Shop"
    assert shop.org.slug == "eva-shop"
    assert shop.org.base_currency == "RWF"
    assert shop.org.timezone == "Africa/Kigali"
    assert shop.store.name == provisioning.DEFAULT_STORE_NAME
    assert set(shop.roles) == set(PRESETS)
    assert all(role.is_preset for role in shop.roles.values())
    assert frozenset(shop.membership.role.permissions) == PRESETS["Owner"]
    assert Membership.objects.filter(user=shop.user).count() == 1
    assert membership_for(shop.user).pk == shop.membership.pk


def test_the_founder_gets_no_store_access_rows(shop):
    """ADR 0011: the grant is the role, and a row that does not control access
    is a decoy for anyone auditing who can reach a store."""
    assert StoreAccess.all_objects.count() == 0
    assert permitted_stores(shop.membership).store_pks == (shop.store.pk,)


def test_register_owner_writes_the_audit_trail(shop):
    actions = list(AuditLog.objects.values_list("action", flat=True))

    assert "org.created" in actions
    assert "store.created" in actions
    assert "membership.created" in actions
    assert "user.registered" in actions
    assert actions.count("role.created") == len(PRESETS)


def test_no_registration_audit_row_echoes_the_new_users_identifiers(shop):
    """Privacy ruling C3, and C4 for the ip. The trail names people with
    pointers, so erasure of the referent is enough.

    Asserted over the *values*, not over the serialised JSON, because of a
    residual the redactor's docstring already accepts and this test reproduced:
    a sole trader's org name is often her own name, so `slug` legitimately
    holds `eva-shop` for a founder called `eva`. An org's commercial identity
    is not one of her identifiers - but a substring check cannot tell the
    difference, and tightening it would only push authors into renaming keys.
    """
    values = []
    for changes in AuditLog.objects.values_list("changes", flat=True):
        values.extend(str(value).lower() for value in changes.values())

    for identifier in (FOUNDER["email"], FOUNDER["username"], FOUNDER["password"]):
        assert identifier.lower() not in values

    flat = str(list(AuditLog.objects.values_list("changes", flat=True)))
    # Unambiguous wherever they appear, so these are substring-checked.
    assert FOUNDER["email"] not in flat
    assert FOUNDER["phone"] not in flat
    assert FOUNDER["password"] not in flat
    assert not AuditLog.objects.exclude(ip=None).exists()


def test_register_owner_rolls_back_entirely_when_one_part_fails(monkeypatch):
    """All-or-nothing, watched rather than assumed: a half-registered founder
    is an account that can log in and reach nothing."""
    real_record = provisioning.audit.record

    def fail_on_membership(action, **kwargs):
        if action == "membership.created":
            raise RuntimeError("boom")
        return real_record(action, **kwargs)

    monkeypatch.setattr(provisioning.audit, "record", fail_on_membership)

    with pytest.raises(RuntimeError):
        register_owner(**FOUNDER)

    assert get_user_model().objects.count() == 0
    assert Organization.all_objects.count() == 0
    assert Store.all_objects.count() == 0
    assert Role.all_objects.count() == 0
    assert Membership.all_objects.count() == 0
    assert AuditLog.objects.count() == 0


def test_a_replayed_registration_creates_no_second_organization(shop):
    """The account's unique identifiers are the idempotency key."""
    with pytest.raises((ValidationError, Exception)):
        register_owner(**FOUNDER)

    assert Organization.objects.count() == 1
    assert get_user_model().objects.count() == 1


def test_two_organizations_with_the_same_name_get_different_slugs(shop):
    second = register_owner(
        username="jean",
        email="jean@example.rw",
        phone="250788000011",
        password="S3cure!passphrase",
        org_name="Eva Shop",
    )

    assert second.org.slug == "eva-shop-2"
    assert Organization.objects.filter(slug="eva-shop").count() == 1


def test_an_unsluggable_org_name_still_gets_a_slug(db):
    result = register_owner(
        username="ndoli",
        email="ndoli@example.rw",
        phone="250788000012",
        password="S3cure!passphrase",
        org_name="---",
    )

    assert result.org.slug == "org"


@pytest.mark.parametrize("bad", ["", "   ", "x" * 121])
def test_register_owner_validates_the_org_name_at_the_boundary(db, bad):
    with pytest.raises(ValueError):
        register_owner(**{**FOUNDER, "org_name": bad})


def test_register_owner_refuses_a_non_string_org_name(db):
    with pytest.raises(TypeError):
        register_owner(**{**FOUNDER, "org_name": ["Eva Shop"]})


def test_register_owner_refuses_a_bogus_language(db):
    with pytest.raises(ValidationError):
        register_owner(**{**FOUNDER, "language": "xx"})


# --------------------------------------------------------------------------
# create_store
# --------------------------------------------------------------------------


def test_create_store_adds_a_store_and_audits_it(shop):
    store = create_store(shop.membership, "Kimironko")

    assert store.org_id == shop.org.pk
    assert store.created_by_id == shop.user.pk
    assert Store.objects.filter(org=shop.org).count() == 2
    row = AuditLog.objects.filter(action="store.created", store=store).get()
    assert row.changes["store_name"] == "Kimironko"


def test_the_store_cap_is_five(shop):
    for i in range(MAX_STORES_PER_ORG - 1):  # "Main" already exists
        create_store(shop.membership, f"S{i}")

    with pytest.raises(StoreLimitReached):
        create_store(shop.membership, "S5")

    assert Store.objects.filter(org=shop.org).count() == MAX_STORES_PER_ORG


def test_a_retired_store_frees_a_slot(shop):
    for i in range(MAX_STORES_PER_ORG - 1):
        create_store(shop.membership, f"S{i}")
    with pytest.raises(StoreLimitReached):
        create_store(shop.membership, "S5")

    soft_delete_store(shop.membership, Store.objects.get(org=shop.org, name="S0"))

    assert create_store(shop.membership, "S5").pk


def test_create_store_needs_the_store_manage_code(shop):
    seller = Membership.objects.create(
        user=make_user(21), org=shop.org, role=shop.roles["Seller"]
    )

    with pytest.raises(PermissionRequired):
        create_store(seller, "Kimironko")

    assert Store.objects.filter(org=shop.org).count() == 1


def test_create_store_refuses_a_duplicate_live_name(shop):
    with pytest.raises(ValidationError):
        create_store(shop.membership, provisioning.DEFAULT_STORE_NAME)


@pytest.mark.parametrize("bad", ["", "  ", "x" * 121])
def test_create_store_validates_the_name(shop, bad):
    with pytest.raises(ValueError):
        create_store(shop.membership, bad)


def test_create_store_refuses_a_non_string_name(shop):
    with pytest.raises(TypeError):
        create_store(shop.membership, 7)


def test_create_store_refuses_to_add_to_a_retired_organization(shop):
    shop.org.soft_delete(by=shop.user)

    with pytest.raises(Organization.DoesNotExist):
        create_store(shop.membership, "Kimironko")


# --------------------------------------------------------------------------
# soft_delete_store — the parent/child policy
# --------------------------------------------------------------------------


def test_retiring_a_store_retracts_the_access_rows_that_named_it(shop):
    second = create_store(shop.membership, "Kimironko")
    member = Membership.objects.create(
        user=make_user(22), org=shop.org, role=shop.roles["Seller"]
    )
    grant_store_access(shop.membership, member, second)
    assert permitted_stores(member).store_pks == (second.pk,)  # positive first

    soft_delete_store(shop.membership, second)

    assert permitted_stores(member).store_pks == ()
    assert StoreAccess.objects.filter(membership=member).count() == 0
    assert StoreAccess.all_objects.filter(membership=member).count() == 1


def test_retiring_a_store_refuses_while_live_rows_point_at_it(shop):
    """`PROTECT` never fires - hard delete is forbidden - so this is a service
    invariant, checked over every registered store-scoped model so a model
    added in slice 2 is covered the day it is written."""
    second = create_store(shop.membership, "Kimironko")
    Sale.objects.for_store(second).create(reference="S-1")

    with pytest.raises(StoreNotEmpty) as exc:
        soft_delete_store(shop.membership, second)

    assert "testapp.Sale" in str(exc.value)
    assert Store.objects.filter(pk=second.pk).exists()


def test_retiring_a_store_is_allowed_once_its_rows_are_retired(shop):
    second = create_store(shop.membership, "Kimironko")
    sale = Sale.objects.for_store(second).create(reference="S-1")
    sale.soft_delete(by=shop.user)

    assert soft_delete_store(shop.membership, second).deleted_at is not None


def test_the_last_store_cannot_be_retired(shop):
    with pytest.raises(LastStore):
        soft_delete_store(shop.membership, shop.store)


def test_retiring_a_store_twice_is_a_no_op(shop):
    second = create_store(shop.membership, "Kimironko")
    soft_delete_store(shop.membership, second)

    again = soft_delete_store(shop.membership, second)

    assert again.pk == second.pk
    assert AuditLog.objects.filter(action="store.retired").count() == 1


def test_retiring_a_store_needs_the_store_manage_code(shop):
    second = create_store(shop.membership, "Kimironko")
    seller = Membership.objects.create(
        user=make_user(23), org=shop.org, role=shop.roles["Seller"]
    )

    with pytest.raises(PermissionRequired):
        soft_delete_store(seller, second)


def test_retiring_another_organizations_store_is_not_found(shop):
    rival = register_owner(
        username="jean",
        email="jean@example.rw",
        phone="250788000013",
        password="S3cure!passphrase",
        org_name="Rival Shop",
    )

    with pytest.raises(StoreNotPermitted):
        soft_delete_store(shop.membership, rival.store)

    assert Store.objects.filter(pk=rival.store.pk).exists()


# --------------------------------------------------------------------------
# grant / revoke store access
# --------------------------------------------------------------------------


@pytest.fixture
def manager(shop):
    """A Manager confined to the first store: `member.manage`, one store."""
    membership = Membership.objects.create(
        user=make_user(31), org=shop.org, role=shop.roles["Manager"]
    )
    StoreAccess.objects.create(membership=membership, store=shop.store)
    return membership_for(membership.user)


@pytest.fixture
def colleague(shop):
    return Membership.objects.create(
        user=make_user(32), org=shop.org, role=shop.roles["Seller"]
    )


def test_granting_store_access_is_idempotent(shop, colleague):
    first = grant_store_access(shop.membership, colleague, shop.store)
    second = grant_store_access(shop.membership, colleague, shop.store)

    assert first.pk == second.pk
    assert StoreAccess.objects.filter(membership=colleague).count() == 1
    assert AuditLog.objects.filter(action="store_access.granted").count() == 1


def test_granting_by_public_id_resolves_through_the_actors_own_set(shop, colleague):
    access = grant_store_access(shop.membership, colleague, public_id=shop.store.public_id)

    assert access.store_id == shop.store.pk


def test_granting_needs_exactly_one_way_of_naming_the_store(shop, colleague):
    with pytest.raises(TypeError):
        grant_store_access(shop.membership, colleague)
    with pytest.raises(TypeError):
        grant_store_access(
            shop.membership, colleague, shop.store, public_id=shop.store.public_id
        )


def test_a_manager_cannot_grant_a_store_it_cannot_reach_itself(shop, manager, colleague):
    """Otherwise a manager confined to A1 hands A2 to a colleague and borrows
    their account."""
    second = create_store(shop.membership, "Kimironko")
    assert grant_store_access(manager, colleague, shop.store).pk  # positive first

    with pytest.raises(StoreNotPermitted):
        grant_store_access(manager, colleague, second)

    assert not StoreAccess.objects.filter(membership=colleague, store=second).exists()


def test_granting_needs_the_member_manage_code(shop, colleague):
    seller = Membership.objects.create(
        user=make_user(33), org=shop.org, role=shop.roles["Seller"]
    )

    with pytest.raises(PermissionRequired):
        grant_store_access(seller, colleague, shop.store)


def test_granting_across_organizations_is_not_found(shop, colleague):
    rival = register_owner(
        username="jean",
        email="jean@example.rw",
        phone="250788000014",
        password="S3cure!passphrase",
        org_name="Rival Shop",
    )

    with pytest.raises(StoreNotPermitted):
        grant_store_access(shop.membership, rival.membership, shop.store)
    with pytest.raises(StoreNotPermitted):
        grant_store_access(shop.membership, colleague, rival.store)


def test_granting_a_row_to_an_access_all_role_is_refused(shop):
    other_owner = Membership.objects.create(
        user=make_user(34), org=shop.org, role=shop.roles["Owner"]
    )

    with pytest.raises(AccessAllHoldsNoRows):
        grant_store_access(shop.membership, other_owner, shop.store)

    assert StoreAccess.all_objects.count() == 0


def test_revoking_is_idempotent_and_audited(shop, colleague):
    grant_store_access(shop.membership, colleague, shop.store)

    assert revoke_store_access(shop.membership, colleague, shop.store) is True
    assert revoke_store_access(shop.membership, colleague, shop.store) is False
    assert AuditLog.objects.filter(action="store_access.revoked").count() == 1
    assert permitted_stores(colleague).store_pks == ()


# --------------------------------------------------------------------------
# set_membership_role — escalation, demotion, lock-out
# --------------------------------------------------------------------------


def test_the_owner_may_promote_a_member(shop, colleague):
    updated = set_membership_role(shop.membership, colleague, shop.roles["Manager"])

    assert updated.role_id == shop.roles["Manager"].pk
    row = AuditLog.objects.filter(action="membership.role_changed").get()
    assert row.changes["role_id_after"] == shop.roles["Manager"].pk


def test_a_manager_cannot_promote_anyone_into_the_owner_role(shop, manager, colleague):
    """The recorded escalation, closed. `member.manage` without `role.manage`
    still let a Manager move a member - or themselves - into the Owner role,
    which now carries `store.access_all`."""
    with pytest.raises(PermissionRequired):
        set_membership_role(manager, colleague, shop.roles["Owner"])


def test_a_role_manager_cannot_grant_more_than_it_holds(shop, colleague):
    """The general rule, independent of preset shapes: you cannot grant what
    you do not hold."""
    deputy_role = Role.objects.create(
        org=shop.org,
        name="Umuyobozi",
        permissions=sorted(PRESETS["Manager"] | {"role.manage"}),
    )
    deputy = Membership.objects.create(user=make_user(35), org=shop.org, role=deputy_role)

    with pytest.raises(PrivilegeEscalation) as exc:
        set_membership_role(deputy, colleague, shop.roles["Owner"])

    assert STORE_ACCESS_ALL in str(exc.value)
    assert colleague.role_id == shop.roles["Seller"].pk


def test_promoting_to_an_access_all_role_retracts_the_stale_rows(shop, colleague):
    """Otherwise the rows are inert while the role holds the override and
    become the membership's entire store set the instant it is demoted."""
    grant_store_access(shop.membership, colleague, shop.store)

    updated = set_membership_role(shop.membership, colleague, shop.roles["Owner"])

    assert StoreAccess.objects.filter(membership=updated).count() == 0
    assert permitted_stores(membership_for(colleague.user)).store_pks == (shop.store.pk,)


def test_demoting_away_from_access_all_refuses_a_missing_store_list(shop, colleague):
    set_membership_role(shop.membership, colleague, shop.roles["Owner"])
    colleague.refresh_from_db()

    with pytest.raises(ValueError):
        set_membership_role(shop.membership, colleague, shop.roles["Manager"])

    assert colleague.role_id == shop.roles["Owner"].pk


def test_demoting_away_from_access_all_states_the_new_store_set(shop, colleague):
    second = create_store(shop.membership, "Kimironko")
    set_membership_role(shop.membership, colleague, shop.roles["Owner"])
    colleague.refresh_from_db()

    updated = set_membership_role(
        shop.membership, colleague, shop.roles["Manager"], stores=[second]
    )

    assert permitted_stores(membership_for(updated.user)).store_pks == (second.pk,)


def test_the_last_role_manager_cannot_be_demoted(shop):
    """Repairing a lock-out needs `role.manage`, which is what would no longer
    exist."""
    plain = Role.objects.create(org=shop.org, name="Plain", permissions=[REPORT_GENERATE])

    with pytest.raises(WouldLockOutTheOrganization):
        set_membership_role(shop.membership, shop.membership, plain, stores=[])

    shop.membership.refresh_from_db()
    assert frozenset(shop.membership.role.permissions) == PERMISSIONS


def test_setting_a_role_from_another_organization_is_not_found(shop, colleague):
    rival = register_owner(
        username="jean",
        email="jean@example.rw",
        phone="250788000015",
        password="S3cure!passphrase",
        org_name="Rival Shop",
    )

    with pytest.raises(StoreNotPermitted):
        set_membership_role(shop.membership, colleague, rival.roles["Manager"])


# --------------------------------------------------------------------------
# The cap under concurrency. Two real connections, not reasoning.
# --------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_the_store_cap_holds_under_two_concurrent_creates():
    """An org holding four stores, two simultaneous creates: exactly five."""
    shop = register_owner(**FOUNDER)
    for i in range(MAX_STORES_PER_ORG - 2):  # "Main" + 3 = 4
        create_store(shop.membership, f"S{i}")
    assert Store.objects.filter(org=shop.org).count() == MAX_STORES_PER_ORG - 1

    membership = membership_for(shop.user)
    ready = threading.Barrier(2, timeout=20)
    outcomes = []

    def worker(name):
        try:
            ready.wait()
            create_store(membership, name)
            outcomes.append("created")
        except StoreLimitReached:
            outcomes.append("refused")
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            outcomes.append(f"error: {exc!r}")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(f"C{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["created", "refused"], outcomes
    assert Store.objects.filter(org=shop.org).count() == MAX_STORES_PER_ORG


@pytest.mark.django_db(transaction=True)
def test_create_store_waits_for_the_organization_row_before_counting():
    """The mechanism, deterministically, and isolated on purpose.

    The organization is already **full**, so the worker's create is refused and
    inserts nothing. That matters: an insert into `orgs_store` takes a
    `FOR KEY SHARE` lock on its parent organization row all by itself, so a
    worker that *writes* would block on a held `FOR UPDATE` whether or not
    `create_store` asked for the lock. With nothing to insert, the only thing
    that can make the worker wait is `select_for_update()` - so this fails on
    the first run if that call is removed, rather than one run in fifty.
    """
    shop = register_owner(**FOUNDER)
    for i in range(MAX_STORES_PER_ORG - 1):
        create_store(shop.membership, f"S{i}")
    assert Store.objects.filter(org=shop.org).count() == MAX_STORES_PER_ORG

    membership = membership_for(shop.user)
    finished = threading.Event()
    outcome = []

    def worker():
        try:
            create_store(membership, "Kimironko")
            outcome.append("created")
        except StoreLimitReached:
            outcome.append("refused")
        except BaseException as exc:  # noqa: BLE001
            outcome.append(f"error: {exc!r}")
        finally:
            finished.set()
            connections.close_all()

    thread = threading.Thread(target=worker)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=shop.org.pk)
        thread.start()
        blocked = not finished.wait(timeout=1.5)

    thread.join(timeout=30)

    assert blocked, "create_store counted the stores without taking the org lock"
    assert outcome == ["refused"], outcome
    assert Store.objects.filter(org=shop.org).count() == MAX_STORES_PER_ORG
