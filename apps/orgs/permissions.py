"""The permission catalog.

Codes live in code, not in the database: a role stores a list of codes and the
catalog is the single source of truth for what a code may be. Adding a
permission is a code change that ships with the feature that needs it.
"""

from django.utils.translation import gettext_lazy as _

MEMBER_MANAGE = "member.manage"
ROLE_MANAGE = "role.manage"
INVITE_CREATE = "invite.create"
STORE_MANAGE = "store.manage"
#: Widens *reach*, grants no *rights* (ADR 0011). Which stores an actor may
#: touch is `services.access.permitted_stores()`; which actions they may take
#: is `Role.has(code)`. The two axes stay orthogonal, so a custom role may
#: hold this code with only `report.generate` - an accountant who reads every
#: branch and writes nowhere.
STORE_ACCESS_ALL = "store.access_all"
SALE_RECORD = "sale.record"
SALE_BELOW_FLOOR_OVERRIDE = "sale.below_floor_override"
STOCK_RESTOCK = "stock.restock"
STOCK_WRITE_OFF = "stock.write_off"
EXPENSE_RECORD = "expense.record"
CYCLE_MANAGE = "cycle.manage"
REPORT_GENERATE = "report.generate"
AUDIT_VIEW = "audit.view"

#: Human labels for the role editor. Wrapped for translation from day one.
PERMISSION_LABELS: dict[str, str] = {
    MEMBER_MANAGE: _("Manage members"),
    ROLE_MANAGE: _("Manage roles"),
    INVITE_CREATE: _("Invite people"),
    STORE_MANAGE: _("Manage stores"),
    STORE_ACCESS_ALL: _("Access every store in the organization"),
    SALE_RECORD: _("Record sales"),
    SALE_BELOW_FLOOR_OVERRIDE: _("Sell below the floor price"),
    STOCK_RESTOCK: _("Restock"),
    STOCK_WRITE_OFF: _("Write off stock"),
    EXPENSE_RECORD: _("Record expenses"),
    CYCLE_MANAGE: _("Manage cycles"),
    REPORT_GENERATE: _("Generate reports"),
    AUDIT_VIEW: _("View the audit trail"),
}

PERMISSIONS: frozenset[str] = frozenset(PERMISSION_LABELS)

#: Ordered pairs for forms and templates.
PERMISSION_CHOICES: tuple[tuple[str, str], ...] = tuple(PERMISSION_LABELS.items())

#: Preset roles created for a new organization.
#:
#: **Every preset is written out in full. No preset may be defined by
#: subtraction from `PERMISSIONS`.** Manager used to be
#: `PERMISSIONS - {ROLE_MANAGE, STORE_MANAGE}`, which meant every code added to
#: the catalog was granted to Manager by whoever added it - silently, with no
#: decision. `audit.view` and `sale.below_floor_override` reached Manager that
#: way, and `store.access_all` would have broken Elvis's owner-only store rule
#: on the day it landed (ADR 0011 rule 3).
#:
#: A code added to the catalog must therefore be added to a preset here or to
#: `UNASSIGNED` below, or `tests/test_orgs_permissions.py` goes red. That test
#: is ADR 0011's `common.E010` and should become a real startup check.
#:
#: Owner is spelled out rather than aliased to `PERMISSIONS` for exactly that
#: reason: aliased, the exhaustiveness rule can never fire, because a new code
#: would land in Owner automatically and count as "assigned".
PRESETS: dict[str, frozenset[str]] = {
    # Everything. `store.access_all` is here and nowhere else.
    "Owner": frozenset(
        {
            MEMBER_MANAGE,
            ROLE_MANAGE,
            INVITE_CREATE,
            STORE_MANAGE,
            STORE_ACCESS_ALL,
            SALE_RECORD,
            SALE_BELOW_FLOOR_OVERRIDE,
            STOCK_RESTOCK,
            STOCK_WRITE_OFF,
            EXPENSE_RECORD,
            CYCLE_MANAGE,
            REPORT_GENERATE,
            AUDIT_VIEW,
        }
    ),
    # Runs the shop floor of the stores it was granted. It cannot reshape the
    # organization (no `role.manage`, no `store.manage`) and it cannot reach a
    # store it was not granted (no `store.access_all`) - a Manager holds
    # `member.manage`, so a Manager with the override would be one role edit
    # away from the whole organization.
    #
    # `audit.view` and `sale.below_floor_override` are here because the
    # subtractive definition granted them and this change is not the place to
    # silently take them away. Both are flagged for ratification: a floor-price
    # override is the discount-fraud control, and the audit trail is where a
    # Manager's own actions are recorded.
    "Manager": frozenset(
        {
            MEMBER_MANAGE,
            INVITE_CREATE,
            SALE_RECORD,
            SALE_BELOW_FLOOR_OVERRIDE,
            STOCK_RESTOCK,
            STOCK_WRITE_OFF,
            EXPENSE_RECORD,
            CYCLE_MANAGE,
            REPORT_GENERATE,
            AUDIT_VIEW,
        }
    ),
    # Serves customers. Nothing else.
    "Seller": frozenset({SALE_RECORD}),
}

#: Codes that are deliberately in no preset. Declaring one here is how the
#: exhaustiveness rule above is satisfied *without* granting it: a code that is
#: only ever attached to a hand-built custom role belongs here, and the empty
#: set means every code in the catalog is currently assigned to someone.
UNASSIGNED: frozenset[str] = frozenset()


def unknown_codes(codes) -> list[str]:
    """The codes in `codes` that are not in the catalog, in input order."""
    return [code for code in codes if code not in PERMISSIONS]


__all__ = [
    "PERMISSIONS",
    "PERMISSION_CHOICES",
    "PERMISSION_LABELS",
    "PRESETS",
    "STORE_ACCESS_ALL",
    "UNASSIGNED",
    "unknown_codes",
]
