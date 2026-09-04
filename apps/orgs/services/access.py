"""Who acts, which stores they reach, and which actions they may take.

Three questions, kept apart on purpose:

* **Who is acting** - `membership_for(user)` / `org_for(user)`. One live
  membership per user (schema plan §J), so this is a total function and it uses
  `.get()`: `MultipleObjectsReturned` is then a free runtime assertion that the
  database constraint held.
* **Which stores** - `permitted_stores(membership)`. The only place the org-wide
  override exists. Nothing else in the codebase reads `StoreAccess` or decides
  which stores an actor may reach (ADR 0011).
* **Which actions** - `check_permission()` / `require_permission()`, over
  `Role.has(code)`.

`store.access_all` widens **reach** and grants no **rights**, so the two axes
compose rather than substitute: a custom role may hold `store.access_all` with
only `report.generate` - an accountant who reads every branch and writes
nowhere. `require_store_permission()` exists so the pair is hard to half-use.

**Never check `role.name == "Owner"`.** `Role.name` is a user-editable,
translatable `CharField`, unique only among live rows of one organization. An
org can rename its owner role - and should be able to; Raporo ships EN/RW/FR -
which would silently *remove* the override, and an org can create a powerless
second role called "Owner", which a name check would silently *grant* it to.
`tests/test_tenancy_matrix.py` carries the decoy row that proves this check is
not name-based, and the mutation evidence for it.

**Nothing here is cached, not even per request.** An owner's store set changes
whenever a store is created or retired, and `create_store()` runs inside a
request: memoised, an owner would create a store and be unable to see it for
the rest of that request. Two indexed queries returning at most five rows do
not buy that. "Revocation takes effect immediately" therefore means *at the
next check*, and every gate is a check.

One seam left open deliberately: §I.1 also has the resolver refuse a membership
whose `org_id` disagrees with the active tenant context. `common/tenancy.py`
does not exist yet, so there is no context to disagree with; when it lands,
that check goes in `permitted_stores()` beside the liveness check below and
raises that module's `TenantContextMismatch`.

Blast radius, so it is never a surprise: **if `permitted_stores()` is wrong,
every store in that one organization becomes readable and writable by every
member of it, and nothing below Python stops it.** RLS checks the organization
and the organization is correct; the composite foreign key checks the
organization and the organization is correct; the store predicate the query
carries is the one this function produced. It is not cross-tenant - the org
predicate comes from elsewhere - but it is total within the org, and the only
detection is a test.
"""

import dataclasses
import enum
import logging
import uuid

from django.core.exceptions import MultipleObjectsReturned

from apps.audit import services as audit
from apps.orgs.exceptions import (
    MembershipNotActive,
    NoMembership,
    NoPermittedStores,
    PermissionRequired,
    StoreNotPermitted,
)
from apps.orgs.models import Membership, Organization, Store
from apps.orgs.permissions import STORE_ACCESS_ALL

logger = logging.getLogger("raporo.orgs")


class Via(enum.StrEnum):
    """How a membership got its store set. For messages and audit rows only -
    **never** for control flow, or the two branches stop being one answer."""

    #: The role holds `store.access_all`: every live store in the org.
    ACCESS_ALL = "access_all"
    #: The live `StoreAccess` rows name the set.
    STORE_ACCESS = "store_access"
    #: The membership's role is soft-deleted, so it grants nothing. Fail
    #: closed: a retired role must not keep handing out reach. Third value
    #: added to §I.1's two because the state is reachable - `Role` is
    #: `PROTECT`ed and hard delete is forbidden, so a retired role leaves live
    #: memberships pointing at it.
    NO_ROLE = "no_role"


@dataclasses.dataclass(frozen=True, slots=True)
class StoreSet:
    """Every live store one membership may reach, and how it got them.

    Frozen because it is an answer, not a working set: something that can be
    appended to is something a later line of a view can widen.
    """

    org_pk: int
    stores: tuple[Store, ...]
    via: Via

    def __bool__(self) -> bool:
        return bool(self.stores)

    def __len__(self) -> int:
        return len(self.stores)

    def __iter__(self):
        return iter(self.stores)

    def __contains__(self, store) -> bool:
        pk = store.pk if isinstance(store, Store) else store
        if not isinstance(pk, int) or isinstance(pk, bool):
            return False
        return pk in self.store_pks

    @property
    def store_pks(self) -> tuple[int, ...]:
        return tuple(store.pk for store in self.stores)

    def by_public_id(self, public_id) -> Store | None:
        """The store in this set with that public identifier, or None.

        Takes whatever a URL produced. A malformed identifier is *absent*, not
        an error: it must land on the same 404 as a well-formed identifier the
        actor may not reach, or the difference is an oracle.
        """
        wanted = _as_uuid(public_id)
        if wanted is None:
            return None
        for store in self.stores:
            if store.public_id == wanted:
                return store
        return None

    def pin(self, manager):
        """Pin a store-scoped queryset to this set.

        Materialised ids, not a subquery: the merge algebra in
        `common/managers.py` is set algebra over integers, the cap is five, and
        a list is a snapshot with a known age while a subquery re-evaluates per
        statement (so a report's totals could disagree with its own rows).
        """
        if not self.stores:
            raise NoPermittedStores(
                "This membership reaches no stores; handle the empty state "
                "before building a query."
            )
        return manager.for_stores(self.stores)


def _as_uuid(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str) and len(value) <= 36:
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Who is acting
# --------------------------------------------------------------------------


def membership_for(user) -> Membership:
    """The user's one live membership, with its role and org loaded.

    `.get()` over the **live** manager, and both halves are load-bearing.

    Live, because the constraint deliberately ignores dead rows: a user may
    hold a live membership in B and a dead one in A, which is what lets someone
    leave one organization and join another. A resolver over `all_objects`
    taking `.first()` would let the dead row in A grant access to A. That is
    the one hazard in this area the database cannot close, and this function is
    how it is closed - `Membership.all_objects` is for audit, export and
    erasure code only.

    `.get()` rather than `.filter().first()`, because
    `MultipleObjectsReturned` here means the one-org constraint was violated.
    It must surface, never be quietly resolved by picking a row. It is not
    audited: writing an audit row needs an organization, and which of the two
    to write it under is exactly the question that cannot be answered.
    """
    try:
        return Membership.objects.select_related("role", "org").get(user=user)
    except Membership.DoesNotExist as exc:
        raise NoMembership("This account belongs to no organization.") from exc
    except MultipleObjectsReturned:
        logger.error(
            "orgs.multiple_live_memberships",
            extra={"user_id": getattr(user, "pk", None)},
        )
        raise


def org_for(user) -> Organization:
    """The one organization this user belongs to."""
    return membership_for(user).org


# --------------------------------------------------------------------------
# Which stores
# --------------------------------------------------------------------------


def permitted_stores(membership: Membership) -> StoreSet:
    """Every live store this membership may reach, and how it got them.

    The only place the org-wide override exists. Nothing else in the codebase
    reads `StoreAccess` or decides which stores an actor may reach.

    One query, either branch. It returns `Store` **instances** rather than ids
    so the scope pin costs nothing extra, and it assumes the caller resolved
    the membership with `select_related("role")` - `membership_for()` does.
    """
    _require_active(membership)

    if _live_role(membership) is None:
        return StoreSet(org_pk=membership.org_id, stores=(), via=Via.NO_ROLE)

    if membership.role.has(STORE_ACCESS_ALL):
        stores = Store.objects.filter(org=membership.org_id)
        via = Via.ACCESS_ALL
    else:
        stores = Store.objects.filter(
            # Redundant in this branch - the composite foreign key already
            # forbids a `StoreAccess` row that mixes organizations - and kept
            # anyway so both branches emit the same predicate shape and both
            # use the org-leading index.
            org=membership.org_id,
            access__membership=membership,
            # The revocation path. Without it a soft-deleted `StoreAccess` row
            # still resolves and revoking access silently does nothing.
            access__deleted_at__isnull=True,
        )
        via = Via.STORE_ACCESS

    return StoreSet(org_pk=membership.org_id, stores=tuple(stores), via=via)


def require_store(membership: Membership, public_id) -> Store:
    """The store behind a URL identifier, or `StoreNotPermitted` (404).

    Store ids from request data never reach `for_stores()`; they reach this
    function, which resolves them *against the permitted set*. So an id that
    got here has been proven live, in-org and reachable, in that order.
    """
    store = permitted_stores(membership).by_public_id(public_id)
    if store is None:
        _audit_store_denial(membership, public_id)
        raise StoreNotPermitted()
    return store


def require_store_permission(membership: Membership, public_id, code: str) -> Store:
    """`require_store()` then `require_permission()`. Both gates, one call.

    Reach is checked first and that order is a control, not a style: a 403 for
    a store the actor cannot see would confirm the store exists.
    """
    store = require_store(membership, public_id)
    require_permission(membership, code)
    return store


def _audit_store_denial(membership: Membership, public_id) -> None:
    """Record the refusal under the **actor's own** organization.

    Never the target's: writing into another tenant's trail is a cross-tenant
    write, which RLS will refuse outright at the worst possible moment.

    Deliberately *not* attempted here: classifying the attempt as within-org or
    cross-org. That needs a lookup outside the permitted set - a cross-tenant
    read RLS will return nothing for - so the row says only what this
    organization can honestly know: this actor asked for a store id it may not
    reach. The requested identifier is recorded because the actor already had
    it; the key ends in `_id`, so the audit redactor treats it as a reference
    rather than content.
    """
    requested = _as_uuid(public_id)
    audit.record(
        "store.access_denied",
        actor=membership.user,
        org=membership.org,
        changes={
            "user_id": membership.user_id,
            "org_id": membership.org_id,
            "membership_id": membership.pk,
            "requested_store_public_id": str(requested) if requested else None,
            "malformed_identifier": requested is None,
        },
    )
    logger.warning(
        "orgs.store_access_denied",
        extra={
            "user_id": membership.user_id,
            "org_id": membership.org_id,
            "requested_store_public_id": str(requested) if requested else None,
        },
    )


# --------------------------------------------------------------------------
# Which actions
# --------------------------------------------------------------------------


def check_permission(membership: Membership, code: str) -> bool:
    """True when this membership's live role grants `code`.

    `Role.has()` already tests catalog membership, so an unknown or withdrawn
    code is False - the correct failure direction for an override.
    """
    _require_active(membership)
    role = _live_role(membership)
    return role is not None and role.has(code)


def require_permission(membership: Membership, code: str) -> None:
    """`PermissionRequired` (403) when the role does not grant `code`."""
    if not check_permission(membership, code):
        audit.record(
            "permission.denied",
            actor=membership.user,
            org=membership.org,
            changes={
                "user_id": membership.user_id,
                "org_id": membership.org_id,
                "code": code,
            },
        )
        logger.warning(
            "orgs.permission_denied",
            extra={
                "user_id": membership.user_id,
                "org_id": membership.org_id,
                "code": code,
            },
        )
        raise PermissionRequired(code)


# --------------------------------------------------------------------------
# Shared preconditions
# --------------------------------------------------------------------------


def _require_active(membership) -> None:
    if not isinstance(membership, Membership):
        raise TypeError(
            f"This gate takes a Membership - the actor in this domain is the "
            f"membership, not the user - got {type(membership).__name__}."
        )
    if membership.pk is None:
        raise MembershipNotActive("This membership was never saved.")
    if membership.deleted_at is not None:
        raise MembershipNotActive(
            f"Membership {membership.pk} is soft-deleted and grants nothing."
        )


def _live_role(membership: Membership):
    """The membership's role, or None if it was retired.

    Fail closed. A retired role that kept granting reach would be a permission
    change nobody made, and `PROTECT` plus the hard-delete ban means the state
    is reachable rather than theoretical.
    """
    role = membership.role
    return None if role is None or role.deleted_at is not None else role


__all__ = [
    "StoreSet",
    "Via",
    "check_permission",
    "membership_for",
    "org_for",
    "permitted_stores",
    "require_permission",
    "require_store",
    "require_store_permission",
]
