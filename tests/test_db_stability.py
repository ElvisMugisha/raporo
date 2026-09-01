"""The stability contract of `common/db.py`, enforced instead of described.

Django replays a migration by *name*, never by content. `audit/0002` imports its
SQL from `common/db.py`, so an edit there changes what a **fresh** database
installs while every already-migrated database keeps the definition it applied
originally - a silent fork nobody notices until the two behave differently.

Two mechanisms, deliberately complementary:

* **structural** - nothing a migration may import is unversioned
  (`CREATE_APPEND_ONLY_FUNCTION_V1`, `append_only_triggers_v1`). The name states
  which shipped migrations depend on that exact text, so an in-place edit is
  obviously the wrong move rather than a judgement call. `test_every_sql_helper_
  in_common_db_is_versioned` keeps that true for helpers added later.
* **tripwire** - a SHA-256 of every one of those strings, and of the SQL the
  migration actually carries, is pinned below. Structure only discourages; this
  fires in CI.

No test here touches the database: the point is the *text*, not its effect.
"""

import ast
import hashlib
import importlib
import inspect
from pathlib import Path

import pytest
from django.db import migrations

from common import db

AUDIT_TRIGGER_MIGRATION = "apps.audit.migrations.0002_append_only_trigger"
GUARDED_TABLE = "audit_auditlog"

REPO_ROOT = Path(__file__).resolve().parent.parent

#: sha256 of each frozen string. Keyed by how a reader finds the string again.
PINNED_SQL = {
    "CREATE_APPEND_ONLY_FUNCTION_V1": (
        "444a870f6f4e75d6a610f0352ff9c350c24701d666d210797e9c8aca6d5c89a0"
    ),
    "DROP_APPEND_ONLY_FUNCTION_V1": (
        "cd3cdae091948fcac3ebba3d2e5c877ada7ab3c86711f7b73c44da44d4be1d8a"
    ),
    f"append_only_triggers_v1({GUARDED_TABLE!r})[forward]": (
        "9cead7d2ade220a1a387e05721447f896990a0d8c3d9a1c13f188c5d522dfc3b"
    ),
    f"append_only_triggers_v1({GUARDED_TABLE!r})[reverse]": (
        "f29d95adfba85c6bc230c80b70ddb4d147a145a0dfda1dc683798a0ccba5e019"
    ),
}

#: sha256 -> what it is, for every OTHER statement a shipped migration replays.
#: `orgs/0001_initial` writes its composite-key SQL inline rather than importing
#: it, and inline SQL is just as frozen: Django replays the migration by name, so
#: editing the text here forks a fresh install from an already-migrated database
#: exactly the same way. Together with `PINNED_SQL` this is the complete set of
#: SQL text the migration graph carries.
PINNED_MIGRATION_SQL = {
    "ef9a20e2ba938e4113a10bb11c8bf5b2e800dbad68dc96199b7197658603e37b": (
        "orgs/0001_initial: ADD CONSTRAINT orgs_membership_role_same_org_fk"
    ),
    "f49eb66b59858353729e4bbbf1220cb09726ea951efde185910b2ce3158ef4d7": (
        "orgs/0001_initial: DROP CONSTRAINT orgs_membership_role_same_org_fk"
    ),
    "32e45943fa8199b8c41a0d3b8f8cc794922ce062f1b0af8cee362b45f81a2d8b": (
        "orgs/0001_initial: ADD CONSTRAINT orgs_storeaccess_membership_same_org_fk"
    ),
    "5663fc7af041b4de05e9ecfd79887fc58230effc45be4a47c3f3a76e62fad7bb": (
        "orgs/0001_initial: DROP CONSTRAINT orgs_storeaccess_membership_same_org_fk"
    ),
    "925b9e7ee793b6d99a62f0cf31d7e036a44e894b6fc5482091aeb37c37e48189": (
        "orgs/0001_initial: ADD CONSTRAINT orgs_storeaccess_store_same_org_fk"
    ),
    "6027187b97ef36ac834b91703c94e1806e2193e2b9a615928a0472f4b89e119b": (
        "orgs/0001_initial: DROP CONSTRAINT orgs_storeaccess_store_same_org_fk"
    ),
    "5ce0ad1793819116b7b232b932fd2c976b96cf1011792b1e758cee2c2f726860": (
        "audit/0001_initial: ADD CONSTRAINT audit_auditlog_store_same_org_fk"
    ),
    "b12a529500e9ad059770bc58b7b3a4853a9c9b1f1ef52805bb5e9e4f36684e82": (
        "audit/0001_initial: DROP CONSTRAINT audit_auditlog_store_same_org_fk"
    ),
}

#: Names `common/db.py` is allowed to export without a version suffix: they name
#: a database object, they are not SQL that a migration replays.
UNVERSIONED_IDENTIFIERS = {"APPEND_ONLY_FUNCTION"}

REMEDY = """
`{name}` is frozen: apps/audit/migrations/0002_append_only_trigger.py replays it
verbatim. Django replays migrations by name, so this edit changes what a FRESH
database installs while every already-migrated database keeps the old
definition. The two then diverge silently.

To change the guard's behaviour, revert this edit and instead:
  1. add CREATE_APPEND_ONLY_FUNCTION_V2 (and append_only_triggers_v2, if the
     trigger wiring changes) NEXT TO the V1 names in common/db.py - never edit a
     _V1 name;
  2. add a NEW migration that runs the V2 SQL (V2 re-issues CREATE OR REPLACE
     for the same function, so a fresh install and a migrated database converge);
  3. add the V2 hashes to PINNED_SQL in this file.

Only if no database anywhere has ever run the migration carrying this string may
you update the hash below instead - in the same commit, called out in the commit
message so a reviewer sees it.

  expected sha256: {expected}
  actual   sha256: {actual}
  actual text:
{text}
"""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assert_pinned(name: str, text: str) -> None:
    expected = PINNED_SQL[name]
    actual = sha256(text)
    assert actual == expected, REMEDY.format(
        name=name, expected=expected, actual=actual, text=text
    )


@pytest.fixture(scope="module")
def migration():
    return importlib.import_module(AUDIT_TRIGGER_MIGRATION)


# --------------------------------------------------------------------------
# The tripwire: the frozen strings themselves
# --------------------------------------------------------------------------


def test_the_frozen_function_sql_has_not_changed():
    assert_pinned("CREATE_APPEND_ONLY_FUNCTION_V1", db.CREATE_APPEND_ONLY_FUNCTION_V1)
    assert_pinned("DROP_APPEND_ONLY_FUNCTION_V1", db.DROP_APPEND_ONLY_FUNCTION_V1)


def test_the_frozen_trigger_sql_has_not_changed():
    forward, reverse = db.append_only_triggers_v1(GUARDED_TABLE)

    assert_pinned(f"append_only_triggers_v1({GUARDED_TABLE!r})[forward]", forward)
    assert_pinned(f"append_only_triggers_v1({GUARDED_TABLE!r})[reverse]", reverse)


def test_the_shipped_migration_still_carries_exactly_the_pinned_sql(migration):
    """Pinning `common/db.py` is not enough on its own.

    The migration could be edited to build its SQL some other way and the module
    hashes would still match, so hash what the migration actually applies.
    """
    forward, reverse = db.append_only_triggers_v1(GUARDED_TABLE)
    applied = [(op.sql, op.reverse_sql) for op in migration.Migration.operations]

    assert applied == [
        (db.CREATE_APPEND_ONLY_FUNCTION_V1, db.DROP_APPEND_ONLY_FUNCTION_V1),
        (forward, reverse),
    ]
    for sql, reverse_sql in applied:
        assert sha256(sql) in PINNED_SQL.values()
        assert sha256(reverse_sql) in PINNED_SQL.values()


# --------------------------------------------------------------------------
# The catch-all: hash what the migrations actually replay
# --------------------------------------------------------------------------


def _run_sql_statements(value) -> list[str]:
    """The statements one `RunSQL.sql` / `.reverse_sql` will send.

    `RunSQL` accepts a string, a list of strings, or a list of
    `(sql, params)` pairs - and `None` / `RunSQL.noop` for "nothing to do".
    """
    if value is None or value is migrations.RunSQL.noop:
        return []
    if isinstance(value, str):
        return [value]
    statements = []
    for item in value:
        statements.append(item[0] if isinstance(item, (list, tuple)) else item)
    return statements


def _migration_modules() -> dict[str, object]:
    """Every migration module in the project, keyed by repo-relative path."""
    modules = {}
    for path in sorted(REPO_ROOT.glob("apps/*/migrations/*.py")):
        if path.name.startswith("__"):
            continue
        dotted = ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
        modules[str(path.relative_to(REPO_ROOT))] = importlib.import_module(dotted)
    return modules


def test_every_run_sql_statement_in_every_migration_is_pinned():
    """The escape hatches the two structural tests below cannot close.

    They read *source*: one scans `vars(common.db)`, the other scans migrations
    for `from common.db import ...`. Both are blind to SQL that arrives another
    way - inlined in the migration, held in a dict or a class attribute, reached
    through `import common.db as cdb`, or produced by a pinned *helper* called
    with a new table name (`append_only_triggers_v1("ledger_stockmovement")`
    hashes to something no pin covers, while the import scan happily counts the
    helper's name as pinned).

    This one reads the operations Django will run, so it does not care how the
    text got there. Slice 2's ledger migrations are the next thing it will meet.
    """
    known = set(PINNED_SQL.values()) | set(PINNED_MIGRATION_SQL)
    seen: dict[str, list[str]] = {}
    unpinned = {}

    for relative_path, module in _migration_modules().items():
        for index, operation in enumerate(getattr(module.Migration, "operations", [])):
            if not isinstance(operation, migrations.RunSQL):
                continue
            for attribute in ("sql", "reverse_sql"):
                for statement in _run_sql_statements(getattr(operation, attribute)):
                    where = f"{relative_path}[{index}].{attribute}"
                    seen.setdefault(relative_path, []).append(where)
                    if sha256(statement) not in known:
                        unpinned[where] = (sha256(statement), statement)

    # Premise: the scan really did reach both migrations that carry raw SQL. A
    # glob or an import that quietly matches nothing would make this vacuous.
    assert "apps/audit/migrations/0002_append_only_trigger.py" in seen
    assert "apps/orgs/migrations/0001_initial.py" in seen

    assert unpinned == {}, (
        f"These migration statements are not pinned: {sorted(unpinned)}. A "
        f"migration is replayed by name, so its SQL is frozen the moment it "
        f"ships: a fresh database would install this text while every migrated "
        f"database keeps the old one. If the statement is genuinely new, add its "
        f"sha256 to PINNED_MIGRATION_SQL with a one-line description; if you "
        f"edited a shipped statement, revert and add a new migration instead.\n"
        + "\n".join(f"  {where}: {digest}\n{text}" for where, (digest, text) in unpinned.items())
    )


# --------------------------------------------------------------------------
# The structure: no unversioned SQL exists to be imported in the first place
# --------------------------------------------------------------------------


def _holds_text(value, depth: int = 0) -> bool:
    """True when `value` is, or contains, a string a migration could replay.

    Deliberately not a "does this look like SQL?" sniff. That version knew only
    CREATE/DROP/ALTER/SET/COMMENT, so an unversioned `REVOKE`, `GRANT`,
    `TRUNCATE`, `INSERT`, `DO $$ ... $$` or `WITH ... UPDATE` constant walked
    straight past it - and the ledger's own routed follow-up is a `REVOKE`. The
    burden is inverted: a string here is replayable text until it is allowlisted.
    """
    if depth > 3:  # pragma: no cover - nothing in common/db.py nests this deep
        return False
    if isinstance(value, str):
        return True
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_holds_text(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return any(_holds_text(item, depth + 1) for item in value.values())
    if isinstance(value, type):
        return any(
            _holds_text(item, depth + 1)
            for name, item in vars(value).items()
            if not name.startswith("_")
        )
    return False


def test_every_sql_helper_in_common_db_is_versioned():
    """A V2 added later must not be reachable through an unversioned alias.

    An alias re-opens exactly the trap this module closes: the next author
    imports the convenient short name into a new migration, someone points that
    name at V3, and the shipped migration quietly changes meaning.
    """
    offenders = []
    for name, value in vars(db).items():
        if name.startswith("_") or name in UNVERSIONED_IDENTIFIERS:
            continue
        is_sql = _holds_text(value)
        is_helper = inspect.isfunction(value) and value.__module__ == db.__name__
        if (is_sql or is_helper) and not name.lower().rstrip("0123456789").endswith("_v"):
            offenders.append(name)

    assert offenders == [], (
        f"common/db.py exports unversioned SQL: {offenders}. Migrations replay "
        f"imported text verbatim, so every SQL string or SQL-building helper must "
        f"carry the version it is frozen at (e.g. `_V1`, `append_only_triggers_v1`) "
        f"and be pinned in PINNED_SQL above. If the name is only an identifier and "
        f"never replayed, add it to UNVERSIONED_IDENTIFIERS with a reason."
    )


def test_no_migration_imports_an_unpinned_name_from_common_db():
    """Slice 2's ledger migrations are the next caller - catch them here.

    A new migration importing a helper this file does not pin would re-create the
    unenforced contract for its own tables.
    """
    pinned_names = {key.split("(")[0] for key in PINNED_SQL}
    imported: dict[str, set[str]] = {}

    for path in sorted(REPO_ROOT.glob("apps/*/migrations/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "common.db":
                names = {alias.name for alias in node.names}
                imported.setdefault(str(path.relative_to(REPO_ROOT)), set()).update(names)

    # Premise: this scan actually sees the one migration we know imports the SQL.
    assert "apps/audit/migrations/0002_append_only_trigger.py" in imported

    unpinned = {
        path: sorted(names - pinned_names - UNVERSIONED_IDENTIFIERS)
        for path, names in imported.items()
        if names - pinned_names - UNVERSIONED_IDENTIFIERS
    }

    assert unpinned == {}, (
        f"These migrations import SQL from common/db.py that no hash in "
        f"PINNED_SQL covers: {unpinned}. Add the hash of each imported string to "
        f"PINNED_SQL, so a later edit to it breaks loudly instead of silently "
        f"changing what an already-shipped migration installs."
    )
