"""`permitted_stores()` and the two gates derived from it.

The function under test is the whole of store-level authorization. If it is
wrong, every store in one organization becomes readable and writable by every
member of it, and nothing below Python stops it - RLS checks the organization
and the organization is correct. So every "returns nothing" assertion here is
preceded by a positive one: under RLS an empty result is the *designed* failure
mode, which makes an empty answer and a working guard indistinguishable unless
you have watched the same call return the row for the right actor.
"""

import dataclasses
import uuid

import pytest
from django.core.exceptions import PermissionDenied

from apps.audit.models import AuditLog
from apps.orgs.exceptions import (
    MembershipNotActive,
    NoMembership,
    NoPermittedStores,
    PermissionRequired,
    StoreNotPermitted,
)
from apps.orgs.models import Membership, Role, Store, StoreAccess
from apps.orgs.permissions import (
    PRESETS,
    REPORT_GENERATE,
    SALE_RECORD,
    STORE_ACCESS_ALL,
    STORE_MANAGE,
)
from apps.orgs.services import (
    Via,
    check_permission,
    membership_for,
    org_for,
    permitted_stores,
    register_owner,
    require_permission,
    require_store,
    require_store_permission,
)
from tests.testapp.models import Sale

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# Fixtures: one real organization, built the way the product builds one
# --------------------------------------------------------------------------


def make_user(suffix, **kwargs):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=f"u{suffix}",
        email=f"u{suffix}@example.rw",
        phone=f"2507880000{suffix:02d}",
        password="S3cure!passphrase",
        **kwargs,
    )


@pytest.fixture
def shop(db):
    """An organization as `register_owner` makes one: three preset roles, one
    store, one Owner membership and - deliberately - no `StoreAccess` rows."""
    return register_owner(
        username="eva",
        email="eva@example.rw",
        phone="250788000010",
        password="S3cure!passphrase",
        org_name="Eva Shop",
    )


@pytest.fixture
def second_store(shop):
    return Store.objects.create(org=shop.org, name="Kimironko")


@pytest.fixture
def seller(shop):
    """A member of the same org, granted the first store only."""
    user = make_user(21)
    membership = Membership.objects.create(
        user=user, org=shop.org, role=shop.roles["Seller"]
    )
    StoreAccess.objects.create(membership=membership, store=shop.store)
    return membership_for(user)


@pytest.fixture
def rival(db):
    return register_owner(
        username="jean",
        email="jean@example.rw",
        phone="250788000011",
        password="S3cure!passphrase",
        org_name="Rival Shop",
    )


# --------------------------------------------------------------------------
# Who is acting
# --------------------------------------------------------------------------


def test_membership_for_returns_the_one_live_membership(shop):
    membership = membership_for(shop.user)

    assert membership.pk == shop.membership.pk
    assert membership.org_id == shop.org.pk
    assert org_for(shop.user).pk == shop.org.pk


def test_a_soft_deleted_membership_grants_no_access(shop, rival):
    """The hazard the database cannot close: a dead row read as authorization.

    The constraint deliberately ignores dead rows - that is what lets someone
    leave org A and join org B - so a resolver over `all_objects` taking
    `.first()` would let the dead membership in A grant access to A.
    """
    dead = shop.membership
    dead.soft_delete(by=shop.user)
    joined = Membership.objects.create(
        user=shop.user, org=rival.org, role=rival.roles["Seller"]
    )

    assert org_for(shop.user).pk == rival.org.pk
    assert membership_for(shop.user).pk == joined.pk
    # ... and the dead row is still there, which is why the manager matters.
    assert Membership.all_objects.filter(user=shop.user).count() == 2


def test_membership_for_refuses_a_user_with_no_organization(shop):
    stranger = make_user(31)

    with pytest.raises(NoMembership):
        membership_for(stranger)


def test_a_dead_membership_handed_to_a_gate_is_refused(shop):
    membership = shop.membership
    membership.soft_delete(by=shop.user)

    with pytest.raises(MembershipNotActive):
        permitted_stores(membership)


def test_the_gates_refuse_a_user_where_a_membership_belongs(shop):
    """A `User` argument would make the gate resolve the membership as a side
    job and re-query what every caller already holds."""
    with pytest.raises(TypeError):
        permitted_stores(shop.user)


# --------------------------------------------------------------------------
# permitted_stores — the access_all branch
# --------------------------------------------------------------------------


def test_the_owner_reaches_every_store_in_the_org(shop, second_store):
    result = permitted_stores(shop.membership)

    assert result.via is Via.ACCESS_ALL
    assert {s.pk for s in result.stores} == {shop.store.pk, second_store.pk}
    assert result.org_pk == shop.org.pk
    assert bool(result) is True


def test_the_owner_holds_no_store_access_rows(shop, second_store):
    """ADR 0011: the grant is the role. A row that does not control access
    would be a decoy for anyone auditing who can reach a store."""
    assert StoreAccess.all_objects.filter(membership=shop.membership).count() == 0


def test_a_new_store_is_reachable_immediately_with_no_propagation(shop):
    before = permitted_stores(shop.membership)
    fresh = Store.objects.create(org=shop.org, name="Nyabugogo")
    after = permitted_stores(shop.membership)

    assert fresh.pk not in before.store_pks
    assert fresh.pk in after.store_pks


def test_the_override_never_crosses_an_organization(shop, rival, second_store):
    result = permitted_stores(shop.membership)

    assert shop.store.pk in result.store_pks  # positive first
    assert rival.store.pk not in result.store_pks


def test_a_retired_store_leaves_the_owners_set(shop, second_store):
    assert second_store.pk in permitted_stores(shop.membership).store_pks

    second_store.soft_delete(by=shop.user)

    assert second_store.pk not in permitted_stores(shop.membership).store_pks


def test_access_all_widens_reach_and_grants_no_rights(shop):
    """A custom role may hold `store.access_all` with only `report.generate`:
    an accountant who reads every branch and writes nowhere."""
    accountant_role = Role.objects.create(
        org=shop.org,
        name="Umucungamari",
        permissions=[STORE_ACCESS_ALL, REPORT_GENERATE],
    )
    membership = Membership.objects.create(
        user=make_user(41), org=shop.org, role=accountant_role
    )
    Store.objects.create(org=shop.org, name="Remera")

    result = permitted_stores(membership)

    assert result.via is Via.ACCESS_ALL
    assert len(result) == 2
    assert check_permission(membership, REPORT_GENERATE) is True
    assert check_permission(membership, SALE_RECORD) is False


# --------------------------------------------------------------------------
# permitted_stores — the store_access branch
# --------------------------------------------------------------------------


def test_a_member_reaches_only_the_stores_it_was_granted(shop, seller, second_store):
    result = permitted_stores(seller)

    assert result.via is Via.STORE_ACCESS
    assert result.store_pks == (shop.store.pk,)
    assert second_store.pk not in result.store_pks


def test_a_member_may_be_granted_more_than_one_store(shop, seller, second_store):
    StoreAccess.objects.create(membership=seller, store=second_store)

    result = permitted_stores(seller)

    assert set(result.store_pks) == {shop.store.pk, second_store.pk}


def test_revoking_access_takes_effect_at_the_next_check_in_the_same_request(shop, seller):
    """The test that fails the moment anyone adds a memo (ADR 0011 rule 4)."""
    assert shop.store.pk in permitted_stores(seller).store_pks  # positive first

    StoreAccess.objects.get(membership=seller, store=shop.store).soft_delete(by=shop.user)

    assert permitted_stores(seller).store_pks == ()


def test_a_soft_deleted_store_access_row_does_not_resolve(shop, seller, second_store):
    access = StoreAccess.objects.create(membership=seller, store=second_store)
    assert second_store.pk in permitted_stores(seller).store_pks  # positive first

    access.soft_delete(by=shop.user)

    assert permitted_stores(seller).store_pks == (shop.store.pk,)


def test_an_empty_permitted_set_is_a_legitimate_answer(shop):
    membership = Membership.objects.create(
        user=make_user(51), org=shop.org, role=shop.roles["Seller"]
    )

    result = permitted_stores(membership)

    assert result.stores == ()
    assert bool(result) is False
    assert result.via is Via.STORE_ACCESS


def test_a_retired_role_grants_no_reach_at_all(shop, seller):
    """Fail closed. `Role` is PROTECTed and hard delete is forbidden, so a
    retired role leaves live memberships pointing at it - a reachable state,
    not a theoretical one."""
    assert permitted_stores(seller).store_pks == (shop.store.pk,)  # positive first

    seller.role.soft_delete(by=shop.user)
    seller.refresh_from_db()

    result = permitted_stores(seller)

    assert result.via is Via.NO_ROLE
    assert result.stores == ()
    assert check_permission(seller, SALE_RECORD) is False


# --------------------------------------------------------------------------
# StoreSet
# --------------------------------------------------------------------------


def test_the_store_set_is_frozen(shop):
    """An answer, not a working set: something appendable is something a later
    line of a view can widen."""
    result = permitted_stores(shop.membership)

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.stores = ()


def test_the_store_set_answers_membership_by_instance_and_pk(shop, second_store, rival):
    result = permitted_stores(shop.membership)

    assert shop.store in result
    assert shop.store.pk in result
    assert rival.store not in result
    assert "not-an-id" not in result
    assert True not in result


def test_by_public_id_finds_a_store_in_the_set(shop, second_store):
    result = permitted_stores(shop.membership)

    assert result.by_public_id(second_store.public_id).pk == second_store.pk
    assert result.by_public_id(str(second_store.public_id)).pk == second_store.pk


@pytest.mark.parametrize(
    "junk", ["", "nope", "1; DROP TABLE orgs_store", None, 7, "x" * 400]
)
def test_a_malformed_identifier_is_absent_rather_than_an_error(shop, junk):
    """It must land on the same answer as a well-formed id the actor may not
    reach, or the difference is an oracle."""
    result = permitted_stores(shop.membership)

    assert result.by_public_id(junk) is None


def test_the_store_set_returns_instances_so_the_pin_costs_nothing_extra(shop):
    result = permitted_stores(shop.membership)

    assert all(isinstance(store, Store) for store in result.stores)
    assert all(store.org_id == shop.org.pk for store in result.stores)


def test_pinning_a_query_to_the_permitted_set_works(shop, seller, second_store):
    mine = Sale.objects.for_store(shop.store).create(reference="S-1")
    Sale.objects.for_store(second_store).create(reference="S-2")

    pinned = permitted_stores(seller).pin(Sale.objects)

    assert [row.pk for row in pinned] == [mine.pk]


def test_pinning_an_empty_set_is_refused(shop):
    membership = Membership.objects.create(
        user=make_user(52), org=shop.org, role=shop.roles["Seller"]
    )

    with pytest.raises(NoPermittedStores):
        permitted_stores(membership).pin(Sale.objects)


# --------------------------------------------------------------------------
# Query cost — the acceptance criterion for "the owner path adds no query"
# --------------------------------------------------------------------------


def test_the_resolver_costs_one_query_on_both_branches(
    shop, seller, second_store, django_assert_num_queries
):
    owner = membership_for(shop.user)
    member = membership_for(seller.user)

    with django_assert_num_queries(1):
        permitted_stores(owner)
    with django_assert_num_queries(1):
        permitted_stores(member)


def test_the_resolver_needs_the_role_preloaded(shop, django_assert_num_queries):
    """`membership_for()` does `select_related("role")`; a caller that hands in
    a bare membership pays for it. Pinned so `TenantMiddleware` losing its
    `select_related` shows up here rather than in a profile."""
    bare = Membership.objects.get(pk=shop.membership.pk)

    with django_assert_num_queries(2):
        permitted_stores(bare)


# --------------------------------------------------------------------------
# require_store — 404, never 403
# --------------------------------------------------------------------------


def test_store_not_permitted_is_not_a_permission_denied():
    """A 403 confirms the row exists, which turns the override's complement
    into an existence oracle across sibling stores. Django renders
    `PermissionDenied` as 403, so this subclassing must never happen."""
    assert not issubclass(StoreNotPermitted, PermissionDenied)


def test_require_store_returns_a_store_the_actor_may_reach(shop, seller):
    assert require_store(seller, shop.store.public_id).pk == shop.store.pk


def test_require_store_refuses_a_sibling_store(shop, seller, second_store):
    assert require_store(seller, shop.store.public_id).pk == shop.store.pk  # positive first

    with pytest.raises(StoreNotPermitted):
        require_store(seller, second_store.public_id)


def test_require_store_refuses_another_organizations_store(shop, rival):
    with pytest.raises(StoreNotPermitted):
        require_store(shop.membership, rival.store.public_id)


@pytest.mark.parametrize("junk", ["", "nope", str(uuid.uuid7()), "../../etc/passwd"])
def test_require_store_refuses_junk_identically(shop, junk):
    with pytest.raises(StoreNotPermitted):
        require_store(shop.membership, junk)


def test_a_store_denial_is_audited_under_the_actors_own_org(shop, rival):
    """Never the target's: writing into another tenant's trail is a
    cross-tenant write, which RLS refuses at the worst possible moment."""
    with pytest.raises(StoreNotPermitted):
        require_store(shop.membership, rival.store.public_id)

    row = AuditLog.objects.filter(action="store.access_denied").get()

    assert row.org_id == shop.org.pk
    assert row.actor_id == shop.user.pk
    assert row.store_id is None
    assert row.changes["requested_store_public_id"] == str(rival.store.public_id)
    assert row.ip is None


def test_a_denial_audit_row_holds_no_identifiers(shop, rival):
    with pytest.raises(StoreNotPermitted):
        require_store(shop.membership, rival.store.public_id)

    changes = AuditLog.objects.filter(action="store.access_denied").get().changes
    flat = str(changes)

    assert shop.user.email not in flat
    assert shop.user.username not in flat
    assert str(shop.user.phone) not in flat


# --------------------------------------------------------------------------
# The permission axis, and the order the two gates run in
# --------------------------------------------------------------------------


def test_require_permission_allows_what_the_role_grants(shop):
    require_permission(shop.membership, STORE_MANAGE)


def test_require_permission_refuses_what_it_does_not(shop, seller):
    assert check_permission(seller, SALE_RECORD) is True  # positive first

    with pytest.raises(PermissionRequired) as exc:
        require_permission(seller, STORE_MANAGE)

    assert exc.value.code == STORE_MANAGE
    assert isinstance(exc.value, PermissionDenied)  # 403 is right for rights


def test_a_permission_denial_is_audited_with_ids_only(shop, seller):
    with pytest.raises(PermissionRequired):
        require_permission(seller, STORE_MANAGE)

    row = AuditLog.objects.filter(action="permission.denied").get()

    assert row.org_id == shop.org.pk
    assert row.changes == {
        "user_id": seller.user_id,
        "org_id": shop.org.pk,
        "code": STORE_MANAGE,
    }


def test_an_unknown_permission_code_is_never_granted(shop):
    assert check_permission(shop.membership, "store.take_over_the_world") is False
    assert check_permission(shop.membership, "") is False
    assert PRESETS["Owner"] == frozenset(shop.membership.role.permissions)


def test_reach_is_checked_before_rights(shop, seller, second_store):
    """Order is a control, not a style. A 403 for a store the actor cannot see
    would confirm the store exists."""
    with pytest.raises(StoreNotPermitted):
        require_store_permission(seller, second_store.public_id, STORE_MANAGE)


def test_require_store_permission_needs_both_gates(shop, seller):
    assert require_store_permission(seller, shop.store.public_id, SALE_RECORD).pk == (
        shop.store.pk
    )

    with pytest.raises(PermissionRequired):
        require_store_permission(seller, shop.store.public_id, STORE_MANAGE)
