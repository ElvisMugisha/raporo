"""The orgs service layer: all business logic, no HTTP (ADR 0007).

A service takes resolved domain objects - never a request, a form or a
`QueryDict` - owns its transaction, its permission check and its audit write,
and returns a domain result. Views parse input, call one service and render.
That is what makes the future DRF API weeks rather than a rewrite, and it is
why every gate in here takes a `Membership`: the membership *is* the actor in
this domain (user + org + role), it is resolved once per request, and after
one-org-per-user it is a total function of the user.

Where things live:

* `access` - `membership_for` / `org_for`, `permitted_stores`, `require_store`,
  `require_store_permission`, `check_permission`, `require_permission`.
* `provisioning` - `register_owner`, `create_store`, `soft_delete_store`.
* `members` - `grant_store_access`, `revoke_store_access`,
  `set_membership_role`.

Import the names from here; the split into modules is an implementation detail.
"""

from apps.orgs.services.access import (
    StoreSet,
    Via,
    check_permission,
    membership_for,
    org_for,
    permitted_stores,
    require_permission,
    require_store,
    require_store_permission,
)
from apps.orgs.services.members import (
    AccessAllHoldsNoRows,
    grant_store_access,
    revoke_store_access,
    set_membership_role,
)
from apps.orgs.services.provisioning import (
    DEFAULT_STORE_NAME,
    FOUNDER_ROLE,
    Registration,
    create_store,
    live_store_scoped_rows,
    register_owner,
    soft_delete_store,
)

__all__ = [
    "DEFAULT_STORE_NAME",
    "FOUNDER_ROLE",
    "AccessAllHoldsNoRows",
    "Registration",
    "StoreSet",
    "Via",
    "check_permission",
    "create_store",
    "grant_store_access",
    "live_store_scoped_rows",
    "membership_for",
    "org_for",
    "permitted_stores",
    "register_owner",
    "require_permission",
    "require_store",
    "require_store_permission",
    "revoke_store_access",
    "set_membership_role",
    "soft_delete_store",
]
