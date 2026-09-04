"""Bringing an organization into existence, and its store roster.

Transaction and lock discipline for everything in this package, stated once:

* **The organization row is the only lock, and it is always taken first.**
  `create_store()`, `soft_delete_store()` and `set_membership_role()` all take
  `Organization.objects.select_for_update()` on the actor's own org before they
  read anything they then act on. One lock, one order, so no deadlock can form;
  when a second lockable row appears, it is taken *after* the organization.
* **Locks are held for two indexed statements**, never across an audit write to
  a third table's index or across anything that could block.
* **`register_owner()` is all-or-nothing.** Account, organization, first store,
  the three preset roles and the founder's membership commit together or none
  of them do.
"""

import dataclasses
import logging
import secrets

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from apps.audit import services as audit
from apps.orgs.exceptions import (
    LastStore,
    StoreLimitReached,
    StoreNotEmpty,
    StoreNotPermitted,
)
from apps.orgs.models import (
    MAX_STORES_PER_ORG,
    Membership,
    Organization,
    Role,
    Store,
    StoreAccess,
)
from apps.orgs.permissions import PRESETS, STORE_MANAGE
from apps.orgs.services.access import require_permission

logger = logging.getLogger("raporo.orgs")

#: The store every organization starts with. A plain string, not a translated
#: one: it is *data*, it is the target of a live-unique constraint, and the
#: founder renames it in a text field five seconds later. A `gettext` here
#: would make the row depend on the request's language.
DEFAULT_STORE_NAME = "Main"

#: Which preset role the founder gets. The *name* is arbitrary and renameable
#: by the organization - the power comes from the codes, never from this string.
FOUNDER_ROLE = "Owner"

_SLUG_ATTEMPTS = 6


@dataclasses.dataclass(frozen=True, slots=True)
class Registration:
    """What a signup produced.

    A value object rather than the `(user, org, store)` tuple the slice-1 plan
    sketched: the caller needs the **membership** too - it is what logs the
    founder in and what every gate takes - and a tuple that grows breaks every
    unpacking site. `roles` is included because the role editor screen is the
    next thing the founder sees.
    """

    user: object
    org: Organization
    store: Store
    membership: Membership
    roles: dict


def register_owner(
    *,
    username,
    email,
    phone,
    password,
    org_name,
    language="en",
    store_name=DEFAULT_STORE_NAME,
) -> Registration:
    """Create an account, its organization, and everything that makes it usable.

    One transaction. Account + organization + first store + the three preset
    roles + the founder's Owner membership, or none of them.

    **The founder gets no `StoreAccess` row** (ADR 0011): the Owner preset holds
    `store.access_all`, so the grant is the role and a row here would be a decoy
    for anyone auditing who can reach a store.

    Idempotency, since a signup POST can be delivered twice: the account's three
    unique identifiers are the idempotency key. A replay fails on
    `username`/`email`/`phone` and rolls the whole transaction back, so a second
    delivery cannot produce a second organization. It surfaces as a
    `ValidationError` (from `create_user`'s `full_clean`) or an `IntegrityError`
    under a true race - both are refusals, neither is a partial write.

    Audit rows carry **no identifiers of the new user** (privacy ruling C3) and
    **no ip** (C4): the actor pointer and the target pointer are how the trail
    names people, and they keep pointing at rows that erasure can anonymise.
    """
    org_name = _required_text("org_name", org_name, max_length=120)
    store_name = _required_text("store_name", store_name, max_length=120)

    with transaction.atomic():
        user = get_user_model().objects.create_user(
            username=username,
            email=email,
            phone=phone,
            password=password,
            language=language,
        )

        org = _create_org(org_name, actor=user)

        roles = {}
        for name, codes in PRESETS.items():
            role = Role(
                org=org, name=name, permissions=sorted(codes), is_preset=True, created_by=user
            )
            role.full_clean()
            role.save()
            roles[name] = role
            audit.record(
                "role.created",
                actor=user,
                org=org,
                target=role,
                changes={"role_name": name, "preset": True, "permissions": sorted(codes)},
            )

        store = Store(org=org, name=store_name, created_by=user)
        store.full_clean()
        store.save()
        audit.record(
            "store.created",
            actor=user,
            org=org,
            store=store,
            target=store,
            changes={"store_name": store.name, "first": True},
        )

        membership = Membership(user=user, org=org, role=roles[FOUNDER_ROLE], created_by=user)
        membership.full_clean()
        membership.save()
        audit.record(
            "membership.created",
            actor=user,
            org=org,
            target=membership,
            changes={
                "user_id": user.pk,
                "role_id": membership.role_id,
                "role_name": FOUNDER_ROLE,
                "store_access_rows": 0,
            },
        )

        # Last, so the row that says "this account exists" is only ever
        # committed alongside a usable organization.
        audit.record(
            "user.registered",
            actor=user,
            org=org,
            target=user,
            changes={"user_id": user.pk, "language": user.language, "via": "owner_signup"},
        )

    logger.info(
        "orgs.registered",
        extra={"user_id": user.pk, "org_id": org.pk, "store_id": store.pk},
    )
    return Registration(user=user, org=org, store=store, membership=membership, roles=roles)


def _create_org(name: str, *, actor) -> Organization:
    """Create the organization, resolving a slug collision by retrying.

    No check-then-act: each attempt is a savepoint and the database decides.
    Two simultaneous signups of "Eva Shop" therefore produce `eva-shop` and
    `eva-shop-2` rather than one 500.
    """
    base = slugify(name)[:120] or "org"
    for attempt in range(1, _SLUG_ATTEMPTS + 1):
        if attempt == 1:
            slug = base
        elif attempt < _SLUG_ATTEMPTS:
            slug = f"{base}-{attempt}"
        else:
            slug = f"{base}-{secrets.token_hex(3)}"
        try:
            with transaction.atomic():
                org = Organization(name=name, slug=slug, created_by=actor)
                # `validate_constraints=False` on purpose: the slug's
                # live-unique constraint is the thing being raced for, and
                # asking about it first is the check-then-act this loop exists
                # to avoid. Field validation (currency, timezone, brand) still
                # runs, and the partial unique index decides the slug.
                org.full_clean(validate_constraints=False)
                org.save()
        except IntegrityError:
            continue
        audit.record(
            "org.created",
            actor=actor,
            org=org,
            target=org,
            changes={
                "slug": org.slug,
                "base_currency": org.base_currency,
                "timezone": org.timezone,
            },
        )
        return org
    raise IntegrityError(
        f"Could not find a free slug for {name!r} in {_SLUG_ATTEMPTS} attempts."
    )


def create_store(membership: Membership, name: str) -> Store:
    """Add a store, refusing the sixth.

    Takes the **membership**, not `(org, actor)`: the membership carries the
    user, the organization and the role, so there is no pair to disagree and no
    way to pass an actor from one org with an org from another.

    The cap holds under concurrency because the organization row is locked
    before the count is taken, which serialises every create in that
    organization. Measured with two real connections against an org holding
    four stores: one commits, one raises, the org holds exactly five.
    """
    name = _required_text("name", name, max_length=120)
    require_permission(membership, STORE_MANAGE)

    with transaction.atomic():
        # Live manager: a retired organization is not there to add a store to.
        org = Organization.objects.select_for_update().get(pk=membership.org_id)

        if Store.objects.filter(org=org).count() >= MAX_STORES_PER_ORG:
            raise StoreLimitReached(
                f"An organization can run at most {MAX_STORES_PER_ORG} stores."
            )

        store = Store(org=org, name=name, created_by=membership.user)
        # Under the lock, so the live-unique name check cannot race either.
        store.full_clean()
        store.save()

        audit.record(
            "store.created",
            actor=membership.user,
            org=org,
            store=store,
            target=store,
            changes={"store_name": store.name, "first": False},
        )

    logger.info(
        "orgs.store_created",
        extra={"user_id": membership.user_id, "org_id": org.pk, "store_id": store.pk},
    )
    return store


def soft_delete_store(membership: Membership, store: Store) -> Store:
    """Retire a store, and retract the access rows that named it.

    **The parent/child policy, because the database cannot hold it here.** Hard
    delete is forbidden, so `on_delete=PROTECT` never fires: soft-deleting a
    store would otherwise leave live rows pointing at a dead parent, and
    nothing below Python would object. So:

    1. a store with any live store-scoped row is refused (`StoreNotEmpty`),
       checked over **every registered store-scoped model**, so a model added
       in slice 2 is covered the day it is written rather than the day someone
       remembers this function;
    2. an organization's last live store is refused (`LastStore`) - the rule is
       one to five stores, and an org with none is unusable;
    3. the store's live `StoreAccess` rows are retracted in the same
       transaction, so no membership is left holding a grant to a dead store.

    Owners need no step 3: they hold no rows, and `permitted_stores()` reads
    live stores only, so a retired store leaves an owner's set immediately.
    """
    require_permission(membership, STORE_MANAGE)
    if store.org_id != membership.org_id:
        # Not reachable through the gates - `require_store()` would already
        # have refused - but this service must not trust its caller either.
        raise StoreNotPermitted()

    with transaction.atomic():
        org = Organization.objects.select_for_update().get(pk=membership.org_id)
        try:
            locked = Store.objects.select_for_update().get(pk=store.pk, org=org)
        except Store.DoesNotExist:
            # Already retired. A retry of an at-least-once caller, not an
            # error: `soft_delete()` is idempotent and so is this.
            return Store.all_objects.get(pk=store.pk, org=org)

        if Store.objects.filter(org=org).count() <= 1:
            raise LastStore("An organization must keep at least one store.")

        blocking = live_store_scoped_rows(locked)
        if blocking:
            raise StoreNotEmpty(
                "This store still holds live rows: "
                + ", ".join(f"{label} ({count})" for label, count in blocking)
            )

        retracted = 0
        for access in StoreAccess.objects.filter(store=locked):
            if access.soft_delete(by=membership.user):
                retracted += 1

        locked.soft_delete(by=membership.user)

        audit.record(
            "store.retired",
            actor=membership.user,
            org=org,
            target=locked,
            changes={
                "store_id": locked.pk,
                "store_name": locked.name,
                "store_access_rows_retracted": retracted,
            },
        )

    logger.info(
        "orgs.store_retired",
        extra={"user_id": membership.user_id, "org_id": org.pk, "store_id": locked.pk},
    )
    return locked


def live_store_scoped_rows(store: Store) -> list[tuple[str, int]]:
    """`(label, count)` for every registered store-scoped model with live rows
    in this store. Empty means the store can be retired.

    Generated from the model registry rather than a hand-kept list: the whole
    point is that a model added later is covered without anyone remembering.
    `all_objects` is the right manager here - this is exactly the audit case it
    exists for, and the guarded manager would refuse an unpinned query.
    """
    from django.apps import apps as django_apps

    from common.models import StoreScopedModel

    blocking = []
    for model in django_apps.get_models():
        if not issubclass(model, StoreScopedModel) or model._meta.abstract:
            continue
        count = model.all_objects.filter(store=store, deleted_at__isnull=True).count()
        if count:
            blocking.append((model._meta.label, count))
    return blocking


def _required_text(field: str, value, *, max_length: int) -> str:
    """Validate at the boundary: type, presence, length. Fail fast, by name."""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string, got {type(value).__name__}.")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} is required.")
    if len(value) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters.")
    return value


__all__ = [
    "DEFAULT_STORE_NAME",
    "Registration",
    "FOUNDER_ROLE",
    "create_store",
    "live_store_scoped_rows",
    "register_owner",
    "soft_delete_store",
]
