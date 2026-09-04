"""P7 - the forbidden-query-form guard.

Deliverable 2 of the period engine is a *contract*, and a contract nobody
enforces is a paragraph. Every period query is `field >= start AND field < end`
with both bounds computed by `common.periods`; anything else either takes its
timezone from the server instead of from the organization, or counts a boundary
row twice. This module scans the source tree and refuses the alternatives.

What it can see
---------------

* an ORM keyword argument whose lookup is one of the refused ones - either on a
  known period column (`business_date`, `at`, `created_at`, …) or, for the
  timezone-dependent extraction lookups, on any column at all;
* the same lookups spelled as string keys in a `**{...}` literal;
* a `Trunc*` or `Extract*` call with no explicit `tzinfo=`;
* `BETWEEN` or a bare `::date` cast inside a SQL string literal;
* a wall-clock reading with no zone: `datetime.now()`, `datetime.utcnow()`,
  `date.today()`, and `timezone.localdate()` / `timezone.localtime()` called
  without an explicit zone - all of which resolve against
  `settings.TIME_ZONE`, which is the measured 300-versus-500 defect.

What it cannot see, stated plainly
----------------------------------

* a lookup assembled at run time - `**{f"{column}__{op}": value}`. The keys are
  not literals, so there is nothing to read.
* SQL concatenated at run time, or arriving from outside the repository.
* anything outside `.py` files: a template filter, a management SQL file, a
  migration's `RunSQL` body assembled from a variable, a report definition in
  a database row.
* whether the bounds a *sanctioned* `__gte`/`__lt` pair uses actually came from
  `common.periods`. It can see the shape, not the provenance.

So this is a floor, not a proof. It exists because it protects code that has
not been written yet, which is the only time that protection is free.

The escape hatch is deliberate and greppable: `# periods: allow <reason>` on
the offending line (or the line above it). Every use of it in this repository
is a test demonstrating the thing being refused.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

#: Directories that hold code we own and that could query a period.
SCANNED_ROOTS = ("common", "apps", "config", "tests")

#: `# periods: allow <reason>`. Case-insensitive, reason required, because an
#: unexplained waiver is a waiver nobody can review.
ALLOW_MARKER = re.compile(r"#\s*periods:\s*allow\s+\S+", re.IGNORECASE)

#: Columns that carry a period. A comparison against one of these has to be
#: the sanctioned pair.
PERIOD_COLUMNS = frozenset(
    {
        "business_date",
        "at",
        "created_at",
        "updated_at",
        "deleted_at",
        "occurred_at",
        "recorded_at",
        "period_start",
        "period_end",
    }
)

#: Refused on a period column. `lte` and `gt` are inclusive-end bounds, which
#: double-count; `range` compiles to SQL `BETWEEN`, inclusive at both ends.
REFUSED_ON_PERIOD_COLUMNS = frozenset({"lte", "gt", "range"})

#: Refused on ANY column: every one of these truncates or extracts using
#: `settings.TIME_ZONE` unless a `tzinfo` is threaded through, and none of them
#: has a legitimate use here - the period engine computes bounds in Python.
REFUSED_EVERYWHERE = frozenset(
    {
        "date",
        "year",
        "iso_year",
        "month",
        "week",
        "week_day",
        "iso_week_day",
        "quarter",
        "day",
        "time",
        "hour",
        "minute",
        "second",
    }
)

#: `Trunc*` and `Extract*` need an explicit `tzinfo=`; without one they use
#: `settings.TIME_ZONE`.
TIMEZONE_DEPENDENT_FUNCTIONS = frozenset(
    {
        "Trunc",
        "TruncDate",
        "TruncTime",
        "TruncDay",
        "TruncWeek",
        "TruncMonth",
        "TruncQuarter",
        "TruncYear",
        "TruncHour",
        "TruncMinute",
        "TruncSecond",
        "Extract",
        "ExtractDay",
        "ExtractWeek",
        "ExtractWeekDay",
        "ExtractIsoWeekDay",
        "ExtractMonth",
        "ExtractQuarter",
        "ExtractYear",
        "ExtractIsoYear",
        "ExtractHour",
        "ExtractMinute",
        "ExtractSecond",
    }
)

#: Wall-clock readings with no zone. Matched on the dotted name, so
#: `time.localtime()` (a different function, in the standard library) is not
#: swept up with `django.utils.timezone.localtime()`.
ZONELESS_CLOCK_CALLS = frozenset(
    {
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "date.today",
        "datetime.date.today",
    }
)

#: `timezone.localdate()` and `timezone.localtime()` default to
#: `settings.TIME_ZONE`. Legitimate with an explicit zone, never without.
SERVER_ZONE_CALLS = frozenset(
    {
        "timezone.localdate",
        "timezone.localtime",
        "django.utils.timezone.localdate",
        "django.utils.timezone.localtime",
    }
)

#: SQL shapes refused inside a string literal. `BETWEEN` is inclusive at both
#: ends; a bare `::date` cast on a `timestamptz` takes the Postgres session's
#: `TimeZone`, which is the server's and not the organization's.
REFUSED_SQL_PATTERNS = (
    # Case-SENSITIVE, unlike every other pattern here, and for the same reason
    # `now()` was dropped below: case-insensitive `\bbetween\b` matches the
    # English word. It fired on `common/models.py`'s "moving a row between
    # organizations is not an operation." - prose in an error message, not SQL.
    # Every raw statement in this codebase spells keywords in upper case (the
    # pinned `_V1` constants in `common/db.py` all do), so the upper-case form
    # still catches real SQL and spares English. A lower-case `between` in a
    # genuine query is the stated blind spot; `test_the_scanner_still_catches_a
    # _real_uppercase_between` is what keeps the narrowing honest.
    # periods: allow naming the refused keyword is this module's job
    (re.compile(r"\bBETWEEN\b"), "BETWEEN is inclusive at both ends"),
    (
        re.compile(r"::\s*date\b", re.IGNORECASE),
        # periods: allow naming the refused cast is this module's job
        "a date cast reads the Postgres session TimeZone, not the organization's",
    ),
    (
        # `now()` is deliberately NOT in this list: it matched Python's own
        # `timezone.now()` in every string that mentioned it, and a guard with
        # false positives gets switched off. The named SQL constants below are
        # unambiguous; a bare `now()` in raw SQL is a blind spot, stated.
        re.compile(
            r"\bcurrent_date\b|\bcurrent_timestamp\b|\blocaltimestamp\b"
            r"|\bstatement_timestamp\b|\bclock_timestamp\b",
            re.IGNORECASE,
        ),
        "the database clock is the server's clock, not the organization's",
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    form: str
    why: str

    def __str__(self):
        return f"{self.path}:{self.line}  {self.form}  -- {self.why}"


def _dotted_name(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    else:
        return ""
    return ".".join(reversed(parts))


def _lookup_suffixes(keyword_name: str) -> tuple[str, str]:
    """`("business_date", "lte")` from `"business_date__lte"`. The column is
    everything before the last `__`, which is how Django reads it too."""
    column, _, lookup = keyword_name.rpartition("__")
    return column, lookup


def _check_lookup_name(name: str) -> str | None:
    if "__" not in name:
        return None
    column, lookup = _lookup_suffixes(name)
    if lookup in REFUSED_EVERYWHERE:
        return (
            f"`__{lookup}` truncates or extracts in settings.TIME_ZONE, not in "
            "the organization's zone"
        )
    if lookup in REFUSED_ON_PERIOD_COLUMNS and column.rpartition("__")[2] in PERIOD_COLUMNS:
        return (
            f"`__{lookup}` on a period column: the sanctioned pair is "
            "`__gte` and `__lt`, both from common.periods"
        )
    return None


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Docstrings are prose about the rules, not SQL. Identified by position -
    a string that is a statement on its own - rather than by content."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                ids.add(id(node.value))
    return ids


def scan_source(source: str, path: str) -> list[Finding]:
    """Every refused form in `source`. Pure: no filesystem, no imports."""
    findings: list[Finding] = []
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:  # pragma: no cover - a broken file fails elsewhere
        return [Finding(path, exc.lineno or 0, "<syntax error>", str(exc))]

    lines = source.splitlines()
    prose = _docstring_nodes(tree)

    def waived(node) -> bool:
        first = getattr(node, "lineno", 0)
        last = getattr(node, "end_lineno", first) or first
        for number in range(max(1, first - 1), min(len(lines), last) + 1):
            if ALLOW_MARKER.search(lines[number - 1]):
                return True
        return False

    def report(node, form, why):
        if not waived(node):
            findings.append(Finding(path, getattr(node, "lineno", 0), form, why))

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg:
            why = _check_lookup_name(node.arg)
            if why:
                report(node, f"{node.arg}=", why)

        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    why = _check_lookup_name(key.value)
                    if why:
                        report(key, f'"{key.value}"', why)

        elif isinstance(node, ast.Call):
            dotted = _dotted_name(node.func)
            simple = dotted.rpartition(".")[2]
            if simple in TIMEZONE_DEPENDENT_FUNCTIONS:
                if not any(word.arg == "tzinfo" for word in node.keywords):
                    report(
                        node,
                        f"{simple}(...)",
                        "no explicit `tzinfo=`, so it truncates in settings.TIME_ZONE",
                    )
            elif dotted in ZONELESS_CLOCK_CALLS and not node.args and not node.keywords:
                report(node, f"{dotted}()", "a wall-clock reading with no zone")
            elif dotted in SERVER_ZONE_CALLS and len(node.args) < 2 and not node.keywords:
                report(
                    node,
                    f"{dotted}()",
                    "no explicit zone, so it resolves against settings.TIME_ZONE",
                )

        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in prose:
                continue
            for pattern, why in REFUSED_SQL_PATTERNS:
                if pattern.search(node.value):
                    report(node, pattern.pattern, why)
                    break

    return findings


def scan_tree(root: Path, subdirectories=SCANNED_ROOTS) -> list[Finding]:
    findings = []
    for name in subdirectories:
        directory = root / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            findings.extend(scan_source(path.read_text(encoding="utf-8"), relative))
    return findings


# --------------------------------------------------------------------------
# The guard, pointed at this repository
# --------------------------------------------------------------------------


def test_the_scanner_still_catches_a_real_uppercase_between():
    """Narrowing `BETWEEN` to case-sensitive must not have disarmed it.

    The pattern was `re.IGNORECASE` and fired on `common/models.py`'s error
    message "moving a row between organizations is not an operation." - the
    English word, in prose, in an f-string. It was narrowed rather than waived,
    on the same reasoning that dropped `now()`: a guard with false positives
    gets switched off. This test is the price of the narrowing. Both halves are
    asserted, because a pattern that catches nothing would pass the first half
    alone.
    """
    caught = scan_source(
        # periods: allow this module has to spell the keyword it refuses
        'cur.execute("SELECT 1 FROM t WHERE business_date BETWEEN %s AND %s", [a, b])',
        "probe_sql.py",
    )
    # periods: allow this module has to spell the keyword it refuses
    assert [f.why for f in caught] == ["BETWEEN is inclusive at both ends"]

    spared = scan_source(
        'raise ValueError("moving a row between organizations is not an operation.")',
        "probe_prose.py",
    )
    assert spared == []


def test_the_tree_contains_no_forbidden_period_query_form():
    findings = scan_tree(REPOSITORY_ROOT)
    assert not findings, "forbidden query forms:\n" + "\n".join(str(f) for f in findings)


def test_the_scanner_actually_reads_this_repository():
    """A scanner pointed at nothing passes everything. This pins that it found
    real files, so `test_the_tree_...` above cannot pass vacuously."""
    scanned = [
        path
        for name in SCANNED_ROOTS
        for path in (REPOSITORY_ROOT / name).rglob("*.py")
        if (REPOSITORY_ROOT / name).is_dir()
    ]
    assert len(scanned) > 30, f"only {len(scanned)} files scanned"
    assert any(path.name == "periods.py" for path in scanned)


# --------------------------------------------------------------------------
# The scanner, watched refusing each form
# --------------------------------------------------------------------------

#: `(source, the form the finding must name)`. Each one is a shape that has
#: produced a wrong total somewhere.
REFUSED_SNIPPETS = [
    (
        "Sale.objects.filter(business_date__gte=start, business_date__lte=end)",
        "business_date__lte=",
    ),
    ("Sale.objects.filter(business_date__range=(start, end))", "business_date__range="),
    ("Sale.objects.filter(at__lte=end)", "at__lte="),
    ("Sale.objects.filter(created_at__gt=start)", "created_at__gt="),
    ("Sale.objects.filter(at__date=today)", "at__date="),
    ("Sale.objects.filter(at__month=9, at__year=2026)", "at__year="),
    ("Sale.objects.filter(at__week=38)", "at__week="),
    ("Sale.objects.filter(store__sale__at__date=today)", "store__sale__at__date="),
    ('Sale.objects.filter(**{"business_date__lte": end})', '"business_date__lte"'),
    ("Sale.objects.annotate(d=TruncDate('at'))", "TruncDate(...)"),
    ("Sale.objects.annotate(m=TruncMonth('at')).values('m')", "TruncMonth(...)"),
    ("Sale.objects.annotate(y=ExtractYear('at'))", "ExtractYear(...)"),
    # periods: allow quoted forbidden form
    ("total = 0\nsql = 'SELECT 1 WHERE d BETWEEN %s AND %s'", "BETWEEN"),
    # periods: allow quoted forbidden form
    ("sql = 'SELECT sum(v) FROM s WHERE at::date >= %s'", "::"),
    # periods: allow quoted forbidden form
    ("sql = 'SELECT sum(v) FROM s WHERE at >= current_date'", "current_date"),
    ("start = datetime.now()", "datetime.now()"),
    ("start = datetime.utcnow()", "datetime.utcnow()"),
    ("start = date.today()", "date.today()"),
    ("start = timezone.localdate()", "timezone.localdate()"),
    ("start = timezone.localtime()", "timezone.localtime()"),
]


@pytest.mark.parametrize(("source", "expected_form"), REFUSED_SNIPPETS)
def test_the_scanner_refuses_a_forbidden_form(source, expected_form):
    findings = scan_source(source, "probe.py")
    assert findings, f"the scanner accepted {source!r}"
    assert any(expected_form in finding.form for finding in findings), [
        str(f) for f in findings
    ]


#: Shapes that must NOT be flagged. A guard that cries wolf gets switched off,
#: and then it guards nothing.
ACCEPTED_SNIPPETS = [
    # The sanctioned pair.
    "Sale.objects.filter(business_date__gte=start, business_date__lt=end)",
    "Sale.objects.filter(**period.date_lookups())",
    "Sale.objects.filter(**period.instant_lookups('at'))",
    # `__lte` on something that is not a period.
    "Product.objects.filter(price__lte=1500)",
    "Product.objects.filter(quantity__gt=0)",
    "Sale.objects.filter(id__range=(1, 50))",
    # A field genuinely called `year`, compared normally.
    "Report.objects.filter(year__gte=2026)",
    # Truncation with the zone threaded through.
    "Sale.objects.annotate(d=TruncDate('at', tzinfo=period.tzinfo))",
    "Sale.objects.annotate(m=TruncMonth('at', tzinfo=tz))",
    # Clock readings that carry a zone.
    "start = datetime.now(UTC)",
    "start = datetime.now(tz=period.tzinfo)",
    "start = timezone.now()",
    "start = timezone.localdate(instant, period.tzinfo)",
    "start = timezone.localdate(timezone=period.tzinfo)",
    "start = time.localtime(stamp)",
    "start = periods.local_date(instant, org)",
    # Soft-delete, which is not a period at all.
    "Sale.objects.filter(deleted_at__isnull=True)",
    # A docstring that talks about the rules.
    # periods: allow quoted prose, which is the point of the case
    '"""Never use BETWEEN here, and never at::date."""\nx = 1',
]


@pytest.mark.parametrize("source", ACCEPTED_SNIPPETS)
def test_the_scanner_accepts_a_sanctioned_form(source):
    findings = scan_source(source, "probe.py")
    assert not findings, [str(f) for f in findings]


def test_the_waiver_needs_a_reason():
    """`# periods: allow` with nothing after it is not a waiver, it is a
    silencer."""
    # periods: allow quoted forbidden form
    silenced = "sql = 'WHERE d BETWEEN %s AND %s'  # periods: allow"
    explained = (
        # periods: allow quoted forbidden form
        "sql = 'WHERE d BETWEEN %s AND %s'  "
        "# periods: allow demonstrating the double-count"
    )
    assert scan_source(silenced, "probe.py")
    assert not scan_source(explained, "probe.py")


def test_a_waiver_on_the_line_above_also_counts():
    source = (
        # periods: allow quoted forbidden form
        "sql = (\n"
        "    # periods: allow the contrast is the point\n"
        "    'SELECT 1 '\n"
        "    'WHERE d BETWEEN %s AND %s'\n"
        ")\n"
    )
    assert not scan_source(source, "probe.py")


def test_every_waiver_in_the_tree_is_in_a_test():
    """The escape hatch exists to demonstrate what is refused. If it ever
    appears in `common/` or `apps/`, somebody has waived a real query."""
    offenders = []
    for name in ("common", "apps", "config"):
        directory = REPOSITORY_ROOT / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if ALLOW_MARKER.search(line):
                    offenders.append(f"{path.relative_to(REPOSITORY_ROOT)}:{number}")
    assert not offenders, "period-guard waivers outside the test suite:\n" + "\n".join(
        offenders
    )
