import pytest
from django.contrib.auth import get_user_model

from apps.orgs.models import Membership, Role, Store, StoreAccess
from apps.orgs.permissions import PRESETS, REPORT_GENERATE, ROLE_MANAGE
from apps.orgs.services import (
    check_permission,
    create_store,
    permitted_stores,
    register_owner,
    require_store,
    set_membership_role,
)

pytestmark = pytest.mark.django_db

FOUNDER = dict(
    username="eva", email="eva@example.rw", phone="250788000010",
    password="S3cure!passphrase", org_name="Eva Shop",
)


def make_user(n):
    return get_user_model().objects.create_user(
        username=f"u{n}", email=f"u{n}@example.rw", phone=f"2507880001{n:02d}",
        password="S3cure!passphrase",
    )


@pytest.fixture
def shop(db):
    return register_owner(**FOUNDER)


def test_f4_stores_is_silently_dropped(shop):
    second = create_store(shop.membership, "Kimironko")
    colleague = Membership.objects.create(
        user=make_user(41), org=shop.org, role=shop.roles["Seller"]
    )
    StoreAccess.objects.create(membership=colleague, store=shop.store)
    print("F4 reach before:", permitted_stores(colleague).store_pks)
    updated = set_membership_role(
        shop.membership, colleague, shop.roles["Manager"], stores=[second]
    )
    print("F4 asked for:", (second.pk,), "reach after:", permitted_stores(updated).store_pks)
    assert permitted_stores(updated).store_pks == (second.pk,), "stores= silently dropped"


def test_f5_a_retired_role_counts_as_a_role_manager(shop):
    confederate_role = Role.objects.create(
        org=shop.org, name="Deputy", permissions=sorted(PRESETS["Owner"])
    )
    confederate = Membership.objects.create(
        user=make_user(42), org=shop.org, role=confederate_role
    )
    confederate_role.soft_delete(by=shop.user)
    confederate.refresh_from_db()

    print("P10 confederate role grants role.manage?",
          confederate.role.has(ROLE_MANAGE),
          "  live-role check says:", check_permission(confederate, ROLE_MANAGE))

    plain = Role.objects.create(org=shop.org, name="Plain", permissions=[REPORT_GENERATE])
    set_membership_role(shop.membership, shop.membership, plain, stores=[])

    live = [
        m for m in Membership.objects.select_related("role", "org").filter(org=shop.org)
        if check_permission(m, ROLE_MANAGE)
    ]
    print("P10 remaining live role.manage holders:", [m.pk for m in live])
    assert live, "the organization is now permanently unmanageable"


def test_f6_a_retired_org_still_grants_reach(shop):
    second = create_store(shop.membership, "Kimironko")
    shop.org.soft_delete(by=shop.user)
    membership = Membership.objects.select_related("role", "org").get(pk=shop.membership.pk)

    reach = permitted_stores(membership)
    print("P27 org deleted_at=", membership.org.deleted_at, " reach=", reach.store_pks,
          " via=", reach.via)
    allowed = None
    try:
        allowed = require_store(membership, second.public_id)
    except Exception as exc:
        print("P27 require_store refused:", type(exc).__name__)
    else:
        print("P27 require_store on a retired org's store: ALLOWED", allowed.pk)
    assert reach.store_pks == (), "a retired organization still grants reach"


def test_f6b_create_store_on_a_retired_org_500s(shop):
    shop.org.soft_delete(by=shop.user)
    membership = Membership.objects.select_related("role", "org").get(pk=shop.membership.pk)
    try:
        create_store(membership, "Kimironko")
    except Exception as exc:
        print("P27b create_store raised:", type(exc).__module__ + "." + type(exc).__name__)
        raise
