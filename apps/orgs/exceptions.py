"""What the orgs services raise, and what each one must render as.

**No HTTP in here.** The service layer stays renderer-agnostic so a DRF view can
sit on the same services later (ADR 0007); the exception-to-status translation
belongs in exactly one place, a `process_exception` hook in
`common/middleware.py`. This module only records, in the docstrings, which
status each type *must* produce - and `tests/test_tenancy_matrix.py` holds a
translator to the same table so the contract is executed rather than described.

The one rule that is a security control rather than a convention:

    StoreNotPermitted MUST NOT subclass PermissionDenied.

Django's default handler renders `PermissionDenied` as **403**, and a 403
confirms the row exists. With an owner override in play that turns the
override's complement into an existence oracle across sibling stores: a manager
probing public ids would learn which ones name a real store in the
organization. Store denials are **404**, byte-identical to the 404 for a row
that never existed (ADR 0011).

A *permission* denial is different and 403 is correct there: it is only ever
raised for a store the actor has already been shown it may reach, so it
confirms nothing the actor did not already know. `require_store_permission()`
therefore checks reach **before** rights - swap that order and the 403 becomes
the oracle the 404 exists to prevent.
"""

from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext_lazy as _


class OrgsError(Exception):
    """Base for every deliberate refusal in this app's service layer."""


# --------------------------------------------------------------------------
# Store reach — 404
# --------------------------------------------------------------------------


class StoreNotPermitted(OrgsError):
    """This actor may not reach that store. **Renders 404, never 403.**

    Deliberately not a `PermissionDenied` (see the module docstring). Carries
    no store id in its message: the message may reach a template, and the
    whole point is that the response is indistinguishable from "no such row".
    """

    def __init__(self, message=None):
        super().__init__(message or _("Not found."))


class NoPermittedStores(OrgsError):
    """The actor reaches zero stores and something tried to pin a query anyway.

    An empty permitted set is a legitimate answer - a member whose only store
    was retired, or whose access was revoked, gets a 200 with an empty state.
    What is refused is *pinning* it: `for_stores(())` raises inside the query
    layer naming a function the caller never called, and a pin of no stores
    reads as "unpinned" downstream, which is worse than an error.
    """


# --------------------------------------------------------------------------
# Rights — 403
# --------------------------------------------------------------------------


class PermissionRequired(PermissionDenied):
    """The actor's role does not hold `code`. Renders 403.

    A `PermissionDenied` on purpose, so Django's own handler is already
    correct. `code` is on the instance for the audit row and the log line;
    privacy ruling C5 means those carry `user_id`, `org_id` and `code` and
    never a username or an email.
    """

    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or _("You do not have permission to do that."))


class PrivilegeEscalation(PermissionDenied):
    """An actor tried to grant more power than it holds itself. Renders 403.

    Not in the original plan, and it closes a recorded hazard: `member.manage`
    without `role.manage` still let a Manager move a member - or themselves -
    into the Owner role, which now carries `store.access_all`. The rule is
    `set_membership_role()`'s: the role being assigned may grant nothing the
    actor's own role does not already grant.
    """


# --------------------------------------------------------------------------
# Actor resolution
# --------------------------------------------------------------------------


class MembershipNotActive(OrgsError):
    """A soft-deleted or unsaved membership was handed to a gate.

    A programming error, not a user error: the caller resolved the wrong row.
    The constraint deliberately ignores dead rows (that is what lets someone
    leave org A and join org B), so a dead membership must never be mistaken
    for authorization. Renders 500 - it means a read path reached for
    `all_objects`.
    """


class OrganizationRetired(PermissionDenied):
    """The actor's organization is soft-deleted, so it grants nothing. 403.

    "Org retired" is the state a Law 058/2021 erasure request lands in, and
    MEASURED before this existed it changed nothing about who could read the
    data: `permitted_stores()` filtered live *stores* and `select_related("org")`
    traversed the foreign key whatever `deleted_at` held, so a retired
    organization's owner kept full reach and `require_store()` still returned
    its stores. The two halves also disagreed - `create_store()` refused, but
    with a raw `Organization.DoesNotExist`, i.e. a 500.

    A `PermissionDenied` subclass so Django's own handler renders it correctly
    today, with no middleware entry to remember. It is not an existence oracle:
    it is only ever raised for the actor's *own* organization, which the actor
    already knows exists.
    """

    def __init__(self, message=None):
        super().__init__(message or _("This organization is closed."))


class NoMembership(OrgsError):
    """This user belongs to no organization. The anonymous-ish path.

    Not an error condition on its own: an invited user who has not accepted,
    or an account whose membership was retired, has no organization context.
    """


# --------------------------------------------------------------------------
# Store roster
# --------------------------------------------------------------------------


class StoreLimitReached(OrgsError):
    """The organization already runs `MAX_STORES_PER_ORG` live stores."""


class StoreNotEmpty(OrgsError):
    """Refusing to retire a store that live rows still point at.

    Hard delete is forbidden everywhere, so `on_delete=PROTECT` never fires:
    soft-deleting a parent would leave live children pointing at a dead store,
    and nothing in the database would object. The policy is therefore a service
    invariant - **a parent may not be retired while a live child points at
    it** - checked across every registered store-scoped model so a model added
    in slice 2 is covered the day it is written.
    """


class LastStore(OrgsError):
    """An organization runs between one and five stores; this is the one."""


class WouldLockOutTheOrganization(OrgsError):
    """The change would leave the organization with nobody who can manage it.

    Refused rather than allowed-and-repaired: repairing it needs a role edit,
    which needs `role.manage`, which is precisely what would no longer exist.
    """


__all__ = [
    "LastStore",
    "MembershipNotActive",
    "NoMembership",
    "NoPermittedStores",
    "OrganizationRetired",
    "OrgsError",
    "PermissionRequired",
    "PrivilegeEscalation",
    "StoreLimitReached",
    "StoreNotEmpty",
    "StoreNotPermitted",
    "WouldLockOutTheOrganization",
]
