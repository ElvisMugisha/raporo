"""Who is in the organization, what they may do, and which stores they reach.

Two rules in here are security controls rather than bookkeeping.

**You cannot grant what you do not hold.** `set_membership_role()` refuses a
role granting any code the actor's own role does not already grant. Without it,
`member.manage` without `role.manage` - which is exactly what the Manager
preset holds - lets a Manager move a member, or themselves, into the Owner
role. ADR 0011 raised that hazard's payoff from "reshape roles within my own
store set" to "read and write every store in the organization", and explicitly
says its rule 3 does not close it. This is the close.

**You cannot grant reach you do not have.** `grant_store_access()` refuses a
store the actor cannot reach itself, so a manager confined to A1 cannot hand
A2 to a colleague and then borrow their account.
"""

import logging

from django.db import transaction

from apps.audit import services as audit
from apps.orgs.exceptions import (
    OrgsError,
    PrivilegeEscalation,
    StoreNotPermitted,
    WouldLockOutTheOrganization,
)
from apps.orgs.models import Membership, Organization, Role, StoreAccess
from apps.orgs.permissions import MEMBER_MANAGE, ROLE_MANAGE, STORE_ACCESS_ALL
from apps.orgs.services.access import (
    permitted_stores,
    require_permission,
    require_store,
)

logger = logging.getLogger("raporo.orgs")


class AccessAllHoldsNoRows(OrgsError):
    """This membership's role reaches every store; a row here would be a decoy.

    ADR 0011: for an `store.access_all` role the grant *is* the role. A
    `StoreAccess` row that does not control access is worse than its absence,
    because someone auditing "who can reach store A2" would read it as an
    answer.
    """


def grant_store_access(
    actor_membership: Membership, membership: Membership, store=None, *, public_id=None
) -> StoreAccess:
    """Let `membership` work in `store`. Idempotent.

    Pass **either** a `Store` the caller already resolved **or** `public_id`,
    the identifier straight off a form - which is resolved *through the actor's
    own permitted set*, so an id the actor cannot reach is a 404 before
    anything is written. Both together is a caller bug, not a preference: it
    hides which of the two decided.
    """
    require_permission(actor_membership, MEMBER_MANAGE)
    if (store is None) == (public_id is None):
        raise TypeError("grant_store_access() needs exactly one of `store` or `public_id`.")
    if public_id is not None:
        store = require_store(actor_membership, public_id)

    if membership.org_id != actor_membership.org_id:
        # Another organization's employee. Refused as not-found, never as
        # forbidden: a 403 here confirms the membership exists.
        raise StoreNotPermitted()
    if store.org_id != actor_membership.org_id:
        raise StoreNotPermitted()
    if store not in permitted_stores(actor_membership):
        raise StoreNotPermitted()
    if membership.role.has(STORE_ACCESS_ALL):
        raise AccessAllHoldsNoRows(
            f"Membership {membership.pk} holds {STORE_ACCESS_ALL} and already "
            f"reaches every store; refusing to write a row that grants nothing."
        )

    with transaction.atomic():
        existing = StoreAccess.objects.filter(membership=membership, store=store).first()
        if existing is not None:
            # At-least-once delivery: a retry must not raise and must not
            # write a second row.
            return existing
        access = StoreAccess(membership=membership, store=store, created_by=actor_membership.user)
        access.full_clean()
        access.save()
        audit.record(
            "store_access.granted",
            actor=actor_membership.user,
            org=actor_membership.org,
            store=store,
            target=access,
            changes={
                "member_user_id": membership.user_id,
                "membership_id": membership.pk,
                "store_id": store.pk,
                "store_name": store.name,
            },
        )
    return access


def revoke_store_access(actor_membership: Membership, membership: Membership, store) -> bool:
    """Take a store away. Returns False if it was already revoked.

    Takes effect at the next gate call, including one later in the same
    request: nothing caches the permitted set.
    """
    require_permission(actor_membership, MEMBER_MANAGE)
    if membership.org_id != actor_membership.org_id or store.org_id != actor_membership.org_id:
        raise StoreNotPermitted()

    with transaction.atomic():
        access = StoreAccess.objects.filter(membership=membership, store=store).first()
        if access is None:
            return False
        access.soft_delete(by=actor_membership.user)
        audit.record(
            "store_access.revoked",
            actor=actor_membership.user,
            org=actor_membership.org,
            store=store,
            target=access,
            changes={
                "member_user_id": membership.user_id,
                "membership_id": membership.pk,
                "store_id": store.pk,
            },
        )
    return True


def set_membership_role(
    actor_membership: Membership, membership: Membership, role: Role, *, stores=None
) -> Membership:
    """Move a member to another role, keeping their store set stated.

    Three refusals, each closing a hole the database cannot see:

    * **Privilege escalation.** The new role may grant nothing the actor's own
      role does not already grant.
    * **The demotion hazard.** Moving *away* from a role holding
      `store.access_all` requires `stores` and refuses `None`. A membership
      promoted to Owner may still carry `StoreAccess` rows from before; they
      are inert while the role holds the override, and they would become that
      membership's entire store set the instant it is demoted - silently, to
      whatever it happened to hold months earlier. Moving *to* such a role
      retracts those rows in the same transaction, so the inert-row state
      cannot arise going forward either.
    * **Lock-out.** The change may not leave the organization with nobody
      holding `role.manage`, because repairing that needs `role.manage`.

    The organization row is locked first, so two simultaneous demotions cannot
    both pass the lock-out check.
    """
    require_permission(actor_membership, MEMBER_MANAGE)
    require_permission(actor_membership, ROLE_MANAGE)

    if membership.org_id != actor_membership.org_id or role.org_id != actor_membership.org_id:
        raise StoreNotPermitted()

    granted = frozenset(role.permissions or [])
    held = frozenset(actor_membership.role.permissions or [])
    if not granted <= held:
        raise PrivilegeEscalation(
            f"That role grants {sorted(granted - held)}, which you do not hold."
        )

    was_access_all = membership.role.has(STORE_ACCESS_ALL)
    will_be_access_all = role.has(STORE_ACCESS_ALL)
    if was_access_all and not will_be_access_all and stores is None:
        raise ValueError(
            "Moving a membership off a store.access_all role needs an explicit "
            "`stores` list: it currently reaches every store, and inheriting "
            "whatever StoreAccess rows it happens to hold is not a decision."
        )

    with transaction.atomic():
        org = Organization.objects.select_for_update().get(pk=actor_membership.org_id)
        locked = Membership.objects.select_for_update().get(pk=membership.pk, org=org)
        previous = locked.role

        if previous.has(ROLE_MANAGE) and not role.has(ROLE_MANAGE):
            remaining = [
                other
                for other in Membership.objects.select_related("role").filter(org=org)
                if other.pk != locked.pk and other.role.has(ROLE_MANAGE)
            ]
            if not remaining:
                raise WouldLockOutTheOrganization(
                    "This is the last membership that can manage roles."
                )

        locked.role = role
        locked.updated_by = actor_membership.user
        locked.full_clean()
        locked.save(update_fields=["role", "updated_by", "updated_at"])

        retracted = 0
        granted_rows = 0
        if will_be_access_all:
            for access in StoreAccess.objects.filter(membership=locked):
                if access.soft_delete(by=actor_membership.user):
                    retracted += 1
        elif was_access_all:
            for store in stores:
                if store.org_id != org.pk:
                    raise StoreNotPermitted()
                if store not in permitted_stores(actor_membership):
                    raise StoreNotPermitted()
                access = StoreAccess(
                    membership=locked, store=store, created_by=actor_membership.user
                )
                access.full_clean()
                access.save()
                granted_rows += 1

        audit.record(
            "membership.role_changed",
            actor=actor_membership.user,
            org=org,
            target=locked,
            changes={
                "member_user_id": locked.user_id,
                "membership_id": locked.pk,
                "role_id_before": previous.pk,
                "role_id_after": role.pk,
                "permissions_before": sorted(previous.permissions or []),
                "permissions_after": sorted(role.permissions or []),
                "store_access_rows_retracted": retracted,
                "store_access_rows_granted": granted_rows,
            },
        )

    logger.info(
        "orgs.membership_role_changed",
        extra={
            "user_id": actor_membership.user_id,
            "org_id": org.pk,
            "membership_id": locked.pk,
        },
    )
    return locked


__all__ = [
    "AccessAllHoldsNoRows",
    "grant_store_access",
    "revoke_store_access",
    "set_membership_role",
]
