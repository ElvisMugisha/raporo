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

#: Preset roles created for a new organization. A Manager runs the shop floor
#: but cannot reshape the organization itself (no role or store management).
PRESETS: dict[str, frozenset[str]] = {
    "Owner": PERMISSIONS,
    "Manager": PERMISSIONS - {ROLE_MANAGE, STORE_MANAGE},
    "Seller": frozenset({SALE_RECORD}),
}


def unknown_codes(codes) -> list[str]:
    """The codes in `codes` that are not in the catalog, in input order."""
    return [code for code in codes if code not in PERMISSIONS]


__all__ = [
    "PERMISSIONS",
    "PERMISSION_CHOICES",
    "PERMISSION_LABELS",
    "PRESETS",
    "unknown_codes",
]
