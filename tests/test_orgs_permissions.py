"""The permission catalog and the presets that hand it out.

Why this file exists at all: `PRESETS["Manager"]` was defined *subtractively*
(`PERMISSIONS - {ROLE_MANAGE, STORE_MANAGE}`), so every code added to the
catalog was granted to Manager by default, silently, by whoever added it. That
is how `audit.view` and `sale.below_floor_override` reached Manager without a
decision, and it is how `store.access_all` would have broken Elvis's
owner-only rule on the day the code landed (ADR 0011 rule 3).

The assertions below are the exhaustiveness rule ADR 0011 numbers
`common.E010`. They live here rather than in `common/checks.py` because that
file belongs to another track in this round; **moving them into a real startup
check is a follow-up commit**, and the criterion says a check earns its cost
only when it enforces a rule over a *class* of things new code can join
unnoticed - which the catalog is, so this one does qualify.
"""

import ast
import inspect
from pathlib import Path

import pytest

from apps.orgs import permissions as perms
from apps.orgs.permissions import (
    PERMISSION_LABELS,
    PERMISSIONS,
    PRESETS,
    STORE_ACCESS_ALL,
    UNASSIGNED,
)

# --------------------------------------------------------------------------
# The catalog
# --------------------------------------------------------------------------


def test_store_access_all_is_in_the_catalog_with_a_label():
    assert STORE_ACCESS_ALL == "store.access_all"
    assert STORE_ACCESS_ALL in PERMISSIONS
    assert str(PERMISSION_LABELS[STORE_ACCESS_ALL]) == "Access every store in the organization"


def test_every_code_has_a_label():
    assert set(PERMISSION_LABELS) == set(PERMISSIONS)


# --------------------------------------------------------------------------
# Exhaustiveness — ADR 0011's `common.E010`, asserted as a test for now
# --------------------------------------------------------------------------


def test_every_catalog_code_is_assigned_or_declared_unassigned():
    """A new code cannot be committed green without a decision per preset."""
    assigned = frozenset().union(*PRESETS.values())
    undecided = PERMISSIONS - assigned - UNASSIGNED

    assert not undecided, (
        f"these codes are in the catalog but in no preset and not declared "
        f"UNASSIGNED: {sorted(undecided)}. Decide, in writing, which presets "
        f"receive them."
    )


def test_no_preset_grants_a_code_outside_the_catalog():
    for name, codes in PRESETS.items():
        assert codes <= PERMISSIONS, f"{name} grants unknown codes: {sorted(codes - PERMISSIONS)}"


def test_unassigned_codes_are_in_the_catalog_and_in_no_preset():
    assert UNASSIGNED <= PERMISSIONS
    for name, codes in PRESETS.items():
        assert not (codes & UNASSIGNED), f"{name} grants a code declared UNASSIGNED"


def test_owner_holds_everything_that_is_assigned_to_anyone():
    assert PRESETS["Owner"] == PERMISSIONS - UNASSIGNED


# --------------------------------------------------------------------------
# The owner-only rule, and the shape that protects it
# --------------------------------------------------------------------------


def test_store_access_all_reaches_no_preset_but_owner():
    """Elvis's rule: only the organization's owner reaches every store.

    Manager especially: it holds `member.manage`, so a Manager who also held
    `store.access_all` would be one role edit away from the whole
    organization.
    """
    for name, codes in PRESETS.items():
        if name == "Owner":
            assert STORE_ACCESS_ALL in codes
        else:
            assert STORE_ACCESS_ALL not in codes, f"{name} must not reach every store"


def test_manager_runs_a_store_but_does_not_own_the_org():
    manager = PRESETS["Manager"]

    assert {"sale.record", "stock.restock", "expense.record", "report.generate"} <= manager
    assert not {"role.manage", "store.manage", STORE_ACCESS_ALL} & manager


def test_seller_records_sales_and_nothing_else():
    assert PRESETS["Seller"] == frozenset({"sale.record"})


def test_no_preset_is_defined_by_subtraction():
    """The cause, not the instance.

    A subtractive preset grants every future code by default. This reads the
    source: each preset value must be a literal `frozenset({...})` of names and
    strings - no `-`, no `|`, no comprehension, nothing that can absorb a code
    nobody assigned.
    """
    source = Path(inspect.getsourcefile(perms)).read_text()
    tree = ast.parse(source)
    presets = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign | ast.Assign)
        and any(
            getattr(t, "id", None) == "PRESETS"
            for t in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        )
    )

    assert isinstance(presets, ast.Dict)
    for key, value in zip(presets.keys, presets.values, strict=True):
        label = getattr(key, "value", "?")
        assert isinstance(value, ast.Call), f"{label} is not a literal frozenset(...)"
        assert getattr(value.func, "id", None) == "frozenset", f"{label} is not a frozenset"
        (arg,) = value.args
        assert isinstance(arg, ast.Set | ast.List | ast.Tuple), f"{label} is not a literal set"
        for element in arg.elts:
            assert isinstance(element, ast.Name | ast.Constant), (
                f"{label} contains a computed element: presets must be written out"
            )


@pytest.mark.parametrize("name", ["Owner", "Manager", "Seller"])
def test_presets_are_frozensets(name):
    assert isinstance(PRESETS[name], frozenset)
