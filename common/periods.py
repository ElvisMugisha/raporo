"""The period engine: what "the 1st to the 15th, in Kigali" means as a query.

Nothing else in this codebase computes a period boundary. Every report, every
export and every "today's total" resolves its window here, and every period
query is the same two comparisons — `>= start` and `< end`. That is not style;
the alternatives are measurably wrong, and the measurements are in the
docstrings below and in `tests/test_periods.py`.

Read this much before using it
------------------------------

**Two clocks, and only one of them is user-settable.** A row carries `at` — the
instant it was recorded, `timestamptz`, never user-settable, kept for the audit
trail and the sales list — and `business_date`, a plain `DATE` in the
organization's timezone that says *which day the business counts it on*. Every
period query filters on `business_date`. A sale entered at 00:20 on the 16th
for the previous evening's trading belongs to the 15th, and only a user-settable
business date can say so.

So a period has **two** ranges and they are not interchangeable:

* `date_range` — `[start_date, end_date_exclusive)`, two `date`s. This is the
  form that filters `business_date`. It is what reports use.
* `instant_range` — `[B(start_date), B(end_date_exclusive))`, two aware UTC
  datetimes. This is the form that filters an *instant* column such as `at`.

They agree by construction: `business_date` defaults to `local_date(at, tz)`,
and `local_date(t, tz) == d` if and only if `t` lies in `DAY(d).instant_range`.
`tests/test_periods.py` asserts that equivalence rather than trusting it,
because it is the hinge the whole design hangs from.

**Half-open, always.** `[start, end)` on instants and on dates. The inclusive
end a human reads ("1–15 September") is *derived* by `label()` and never
stored, so the two cannot drift. MEASURED on a Kigali fixture straddling the
15th/16th boundary at `23:59:59.999999`, `00:00:00.000000` and
`00:00:00.000001`: the half-open range reconciled at 60.00 while the
`BETWEEN`-with-an-inclusive-end form returned 80.00 — the 20.00 counted twice.
`tests/test_periods.py::test_between_double_counts_the_boundary_row` reproduces
it.

**The timezone is a parameter.** Never `settings.TIME_ZONE`, never the host
clock, never the Postgres session `TimeZone`. Pass the organization (or its
zone) in; `resolve_timezone()` accepts a `ZoneInfo`, an IANA key, or any object
exposing a `timezone` attribute — which `orgs.Organization` does. The whole
period suite runs three times, under `TIME_ZONE` of `UTC`,
`Pacific/Kiritimati` (+14) and `America/Anchorage` (DST, negative), and asserts
identical results. Under `TIME_ZONE = "UTC"` Django compiles UTC into every
`__date` lookup, and `filter(at__date=...)` was MEASURED returning 300 where the
Kigali truth was 500.
"""

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from common.validators import unsafe_timezone_reason

#: One second, the granularity every IANA transition lands on. Used by the
#: bisection in `_gap_end()`.
_ONE_SECOND = timedelta(seconds=1)

#: Sanity bound on that bisection. A 24-hour gap (Pacific/Apia 2011-12-30, a
#: whole calendar day skipped) needs 17 halvings to reach one second; 64 is
#: room to spare and a guarantee the loop terminates.
_MAX_BISECTIONS = 64


class PeriodError(ValueError):
    """A period that cannot exist: a week anchored on a Thursday, a custom
    range that runs backwards, a biweekly half anchored on the 7th."""


# --------------------------------------------------------------------------
# Timezones
# --------------------------------------------------------------------------


@lru_cache(maxsize=512)
def _zone(key: str) -> ZoneInfo:
    return ZoneInfo(key)


def resolve_timezone(source) -> ZoneInfo:
    """The organization's reporting zone, from whatever holds it.

    Accepts a `ZoneInfo`, an IANA key, or an object with a `timezone`
    attribute — `orgs.Organization` is the intended caller, and taking it as a
    parameter rather than importing the model is what lets the engine be
    tested without a database.

    Structurally unsafe keys are refused *here as well as* at the field
    validator, because a row written before the allowlist existed would
    otherwise compute real month-ends off the host clock. `localtime` resolves
    to `/etc/localtime`, so the same database served from two machines would
    produce two different month-ends for the same organization; `Etc/GMT+5`
    was MEASURED with a `utcoffset` of −05:00, a ten-hour error in a form that
    reads as if it were right.
    """
    if isinstance(source, ZoneInfo):
        key = source.key
    elif isinstance(source, str):
        key = source
    else:
        key = getattr(source, "timezone", None)
        if isinstance(key, ZoneInfo):
            key = key.key
        if not isinstance(key, str) or not key:
            raise PeriodError(
                "Pass a timezone: an IANA key, a ZoneInfo, or an object with a "
                f"`timezone` attribute (got {type(source).__name__})."
            )

    reason = unsafe_timezone_reason(key)
    if reason is not None:
        raise PeriodError(f"{key!r} cannot bound a reporting period: {reason}")
    try:
        return _zone(key)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PeriodError(f"{key!r} is not a known time zone name.") from exc


# --------------------------------------------------------------------------
# B — the boundary function
# --------------------------------------------------------------------------


def boundary(d: date, tz) -> datetime:
    """`B(d, tz)`: the earliest instant whose wall-clock date in `tz` is `>= d`.

    Returned as an aware UTC datetime, because a boundary is an instant and an
    instant has no zone of its own.

    **Why `>=` and not `==`.** It makes `B` total. Local midnight does not
    exist on `Africa/Cairo 2026-04-24`, `Asia/Beirut 2026-03-29` or
    `America/Santiago 2026-09-06` (all MEASURED: the clocks go 23:59:59 →
    01:00:00), and on `Pacific/Apia` the calendar date 2011-12-30 does not
    exist at all. With `==` those are a crash or a guess. With `>=`, `B` is
    defined everywhere and non-decreasing, so the day-periods still tile the
    timeline and a skipped calendar day comes out as a **zero-length period** —
    MEASURED: `B(2011-12-30) == B(2011-12-31) == 2011-12-30T10:00:00Z`, so
    `DAY(2011-12-30)` is empty rather than 24 hours of someone else's takings.

    **`B` is not `start + timedelta(days=n)`.** MEASURED in `Europe/Brussels`:
    2026-03-29 is 23 hours long and 2026-10-25 is 25. Every boundary is
    computed from its own calendar date.

    **Why the fast path is not the whole function.** `datetime(y, m, d,
    tzinfo=tz)` with `fold=0` is right almost everywhere: when local midnight
    exists once it *is* the infimum, and when it exists twice (a fall-back
    landing on 00:00, e.g. `America/Goose_Bay` every October from 1987 to 2010,
    where DST ended at 00:01 and the local date went *backwards* into the
    previous day) `fold=0` picks the earlier occurrence, which is again the
    infimum. It is wrong in exactly one shape: a forward gap that *straddles*
    midnight, so midnight is missing and the gap opened before it. MEASURED on
    `America/Toronto` (and its aliases Montreal, Nipigon, Thunder_Bay)
    1919-03-31, where the clocks went 23:29:59 −05:00 → 00:30:00 −04:00 at
    1919-03-31T04:30:00Z: the naive form answers 05:00:00Z, but 04:30:00Z is
    already the 31st locally, so half an hour of the 31st gets reported on the
    30th. Historical import is allowed at any age, so the gap branch below
    finds the real infimum — the instant the gap ends.

    A full scan of all 486 zones in the container across 1900-01-01…2040-12-31
    (25,029,000 zone-days) found the naive form non-monotone **0** times, and
    not-the-infimum **4** times: the four Ontario aliases above, all on
    1919-03-31. The branch exists for those four and for whatever tzdata adds
    next.
    """
    return _boundary(d, resolve_timezone(tz).key)


@lru_cache(maxsize=8192)
def _boundary(d: date, key: str) -> datetime:
    tz = _zone(key)
    wall_midnight = datetime.combine(d, time.min)
    # fold=0 reads a missing local time with the offset in force *before* the
    # gap, and an ambiguous one as its earlier occurrence. Both are what we
    # want; see the docstring.
    candidate = wall_midnight.replace(tzinfo=tz, fold=0).astimezone(UTC)
    if candidate.astimezone(tz).replace(tzinfo=None) == wall_midnight:
        return candidate

    # Local midnight is missing: it lies inside a forward gap. fold=1 reads it
    # with the post-gap offset, which lands *before* the transition, so the
    # transition is bracketed and the infimum is where the gap ends.
    other = wall_midnight.replace(tzinfo=tz, fold=1).astimezone(UTC)
    gap_end = _gap_end(tz, min(candidate, other), max(candidate, other))
    if gap_end is not None and gap_end.astimezone(tz).date() >= d:
        return min(candidate, gap_end)
    return candidate


def _gap_end(tz: ZoneInfo, lo: datetime, hi: datetime) -> datetime | None:
    """The least instant in `(lo, hi]` where `tz`'s offset changes, or `None`.

    Sound because it assumes at most one transition in the bracket, and the
    bracket is at most one gap wide. MEASURED across every zone in the
    container: the smallest interval between two consecutive transitions in the
    whole IANA database is 344,400 seconds (about four days, `Africa/Freetown`
    1939), and **zero** pairs are closer than an hour.
    `tests/test_periods.py::test_no_two_transitions_share_a_day` keeps that an
    assertion rather than an assumption.
    """
    off_lo = lo.astimezone(tz).utcoffset()
    if hi.astimezone(tz).utcoffset() == off_lo:
        return None
    for _ in range(_MAX_BISECTIONS):
        span = (hi - lo) // _ONE_SECOND
        if span <= 1:
            return hi
        mid = lo + timedelta(seconds=span // 2)
        if mid.astimezone(tz).utcoffset() == off_lo:
            lo = mid
        else:
            hi = mid
    raise AssertionError(  # pragma: no cover - _MAX_BISECTIONS is generous
        f"offset bisection did not converge for {tz.key} in ({lo}, {hi}]"
    )


def local_date(instant: datetime, tz) -> date:
    """The wall-clock date of `instant` in `tz` — the default `business_date`.

    Refuses a naive datetime. A naive value here is an instant whose zone
    somebody forgot, and reading it as UTC is the guess that shifts a Kigali
    sale recorded at 01:00 back onto the previous day.
    """
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise PeriodError("local_date() needs an aware datetime; a naive one has no instant.")
    return instant.astimezone(resolve_timezone(tz)).date()


def is_future_business_date(business_date: date, now: datetime, tz) -> bool:
    """A business date in the organization's future. Always refused, no
    permission unlocks it: a report for a period that has not finished is not
    late data, it is fiction. The bounded backdating window and the
    `sale.backdate` permission need models that do not exist yet; this
    predicate is the half that does not."""
    return business_date > local_date(now, tz)


# --------------------------------------------------------------------------
# Calendar arithmetic
# --------------------------------------------------------------------------


def next_month(year: int, month: int) -> tuple[int, int]:
    """The month after `(year, month)`, carrying the year.

    Never `month + 1`. MEASURED: `datetime(2026, 13, 1)` raises
    `ValueError: month must be in 1..12, not 13`, which makes December the one
    month where the naive form crashes instead of lying — the good failure, and
    `tests/test_periods.py::test_december_second_half_rolls_into_january` keeps
    it impossible to reintroduce quietly.
    """
    return (year + 1, 1) if month == 12 else (year, month + 1)


def first_of_next_month(day: date) -> date:
    year, month = next_month(day.year, day.month)
    return date(year, month, 1)


def last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def iso_week_start(day: date) -> date:
    """The Monday of `day`'s ISO week. Never clipped to a month: a week that
    starts on 30 March runs to 5 April and is reported as one week."""
    return day - timedelta(days=day.weekday())


#: The day of the month the second biweekly half opens on. The first half is
#: the 1st–15th and the second the 16th–end of month, so 28-, 29-, 30- and
#: 31-day months all put the tail in the second half and the halves are
#: deliberately unequal.
BIWEEK_SECOND_HALF_DAY = 16


# --------------------------------------------------------------------------
# The canonical period
# --------------------------------------------------------------------------


class PeriodType(StrEnum):
    DAY = "day"
    WEEK = "week"
    BIWEEK_FIRST = "biweek-first"
    BIWEEK_SECOND = "biweek-second"
    MONTH = "month"
    CUSTOM = "custom"


#: Bumped when the stored key's *meaning* changes, never for a new period type.
#: A stored key with an unknown version is refused, not guessed at: a report
#: artifact that cannot say which rules produced it cannot be reconciled.
KEY_VERSION = "1"

_KEY_SEPARATOR = "|"
_KEY_FIELDS = 5

#: The sanctioned lookups, and the only two a period may compile to. `__lte`,
#: `__gt`, `__range`, `__between`, `__date`, `__year`, `__month`, `__week` and
#: `Trunc*`-without-`tzinfo` are all refused by
#: `tests/test_query_forms.py`, which scans the tree.
START_LOOKUP = "gte"
END_LOOKUP = "lt"


@dataclass(frozen=True, slots=True)
class Period:
    """A named window, stored as *structure* and never as two timestamps.

    The state is the type, the first calendar day, the zone key and — for a
    custom range only — the last calendar day. The half-open date range, the
    half-open instant range and the inclusive human label are all **derived**
    from that, so a report cannot end up labelled "1–15 September" while
    querying something else. `tests/test_audit.py:453` already stores
    `"2026-09-01/2026-09-15"` in an audit payload: that inclusive-end string is
    a fine human label, it is what `label()` produces, and it must never be
    parsed back into a query. `Period.parse()` refuses it.
    """

    type: PeriodType
    start_date: date
    timezone_key: str
    custom_last_day: date | None = None

    # -- construction ----------------------------------------------------

    def __post_init__(self):
        # Validated here rather than in each constructor so a hand-built or
        # parsed Period cannot skip it.
        if self.type is PeriodType.WEEK and self.start_date.weekday() != 0:
            raise PeriodError(
                f"A week starts on a Monday; {self.start_date} is a "
                f"{calendar.day_name[self.start_date.weekday()]}."
            )
        if self.type in (PeriodType.MONTH, PeriodType.BIWEEK_FIRST) and self.start_date.day != 1:
            raise PeriodError(f"{self.type} starts on the 1st; got {self.start_date}.")
        if (
            self.type is PeriodType.BIWEEK_SECOND
            and self.start_date.day != BIWEEK_SECOND_HALF_DAY
        ):
            raise PeriodError(
                f"The second biweekly half starts on the {BIWEEK_SECOND_HALF_DAY}th; "
                f"got {self.start_date}."
            )
        if self.type is PeriodType.CUSTOM:
            if self.custom_last_day is None:
                raise PeriodError("A custom period needs its last day.")
            if self.custom_last_day < self.start_date:
                raise PeriodError(
                    f"A custom period runs forwards: {self.start_date} to "
                    f"{self.custom_last_day} does not."
                )
        elif self.custom_last_day is not None:
            raise PeriodError(f"custom_last_day belongs to a custom period, not to {self.type}.")
        # Resolves the zone, so an unsafe or unknown key is refused at
        # construction and not three screens later inside a report.
        resolve_timezone(self.timezone_key)

    # -- derived shape ---------------------------------------------------

    @property
    def tzinfo(self) -> ZoneInfo:
        return resolve_timezone(self.timezone_key)

    @property
    def last_day(self) -> date:
        """The inclusive last calendar day. Derived, never stored — except for
        a custom range, whose last day *is* part of its identity."""
        match self.type:
            case PeriodType.DAY:
                return self.start_date
            case PeriodType.WEEK:
                return self.start_date + timedelta(days=6)
            case PeriodType.BIWEEK_FIRST:
                return self.start_date.replace(day=BIWEEK_SECOND_HALF_DAY - 1)
            case PeriodType.BIWEEK_SECOND | PeriodType.MONTH:
                return last_day_of_month(self.start_date.year, self.start_date.month)
            case PeriodType.CUSTOM:
                return self.custom_last_day
        raise AssertionError(f"unhandled period type {self.type}")  # pragma: no cover

    @property
    def end_date_exclusive(self) -> date:
        """The first day *after* the period. The bound `business_date < end`
        compares against, and the reason a 31-day month and a 28-day February
        need no special case anywhere else."""
        return self.last_day + timedelta(days=1)

    @property
    def date_range(self) -> tuple[date, date]:
        """`[start, end)` for the `business_date` column."""
        return self.start_date, self.end_date_exclusive

    @property
    def start_instant(self) -> datetime:
        return boundary(self.start_date, self.timezone_key)

    @property
    def end_instant(self) -> datetime:
        return boundary(self.end_date_exclusive, self.timezone_key)

    @property
    def instant_range(self) -> tuple[datetime, datetime]:
        """`[B(start), B(end))` for an instant column such as `at`."""
        return self.start_instant, self.end_instant

    @property
    def days(self) -> int:
        """Calendar days in the period: 13 for February 2026's second half, 14
        for February 2028's, 16 for January's."""
        return (self.end_date_exclusive - self.start_date).days

    @property
    def duration(self) -> timedelta:
        """Elapsed time, which is *not* `days * 24h`. MEASURED in
        `Europe/Brussels`: 23 hours on 2026-03-29 and 25 on 2026-10-25."""
        return self.end_instant - self.start_instant

    # -- membership ------------------------------------------------------

    def contains_date(self, value: date) -> bool:
        return self.start_date <= value < self.end_date_exclusive

    def contains_instant(self, value: datetime) -> bool:
        if value.tzinfo is None or value.utcoffset() is None:
            raise PeriodError("contains_instant() needs an aware datetime.")
        start, end = self.instant_range
        return start <= value < end

    # -- queries ---------------------------------------------------------

    def date_lookups(self, field: str = "business_date") -> dict:
        """`field >= start AND field < end`, as ORM keyword arguments.

        The only way a period reaches a queryset. Two comparisons, both
        computed here, so no caller ever writes a boundary and no caller can
        reach for `__range` or an inclusive `__lte`.
        """
        start, end = self.date_range
        return {f"{field}__{START_LOOKUP}": start, f"{field}__{END_LOOKUP}": end}

    def instant_lookups(self, field: str = "at") -> dict:
        """The same two comparisons against an instant column."""
        start, end = self.instant_range
        return {f"{field}__{START_LOOKUP}": start, f"{field}__{END_LOOKUP}": end}

    # -- identity --------------------------------------------------------

    @property
    def key(self) -> str:
        """The canonical stored form: version, type, first day, custom last
        day (blank unless custom), zone. Fixed five fields, so a parser never
        has to guess, and the zone travels with it — a period without its zone
        is not a period."""
        last = self.custom_last_day.isoformat() if self.custom_last_day else ""
        return _KEY_SEPARATOR.join(
            (KEY_VERSION, str(self.type), self.start_date.isoformat(), last, self.timezone_key)
        )

    @classmethod
    def parse(cls, key: str) -> Period:
        """Rebuild a period from `key`. Refuses anything else, including a
        `label()` string: `2026-09-01/2026-09-15` has an inclusive end, and an
        inclusive end read as a query bound double-counts the last day."""
        if not isinstance(key, str):
            raise PeriodError(f"A period key is a string, not {type(key).__name__}.")
        fields = key.split(_KEY_SEPARATOR)
        if len(fields) != _KEY_FIELDS:
            raise PeriodError(
                f"{key!r} is not a period key. Expected {_KEY_FIELDS} "
                f"{_KEY_SEPARATOR!r}-separated fields."
            )
        version, type_value, start_value, last_value, zone = fields
        if version != KEY_VERSION:
            raise PeriodError(
                f"Period key version {version!r} is not {KEY_VERSION!r}; refusing to "
                "guess which rules produced it."
            )
        try:
            period_type = PeriodType(type_value)
        except ValueError as exc:
            raise PeriodError(f"{type_value!r} is not a period type.") from exc
        try:
            start = date.fromisoformat(start_value)
            last = date.fromisoformat(last_value) if last_value else None
        except ValueError as exc:
            raise PeriodError(f"{key!r} carries a date that is not ISO-8601.") from exc
        return cls(
            type=period_type, start_date=start, timezone_key=zone, custom_last_day=last
        )

    def label(self) -> str:
        """The inclusive human label, derived. `2026-09-01/2026-09-15`.

        Derived on every read and never stored, which is what makes it
        impossible for the label and the range to disagree. Formatting it for a
        Kinyarwanda or French reader is `localization-engineer`'s; this is the
        stable machine-readable form that goes in an audit payload.
        """
        return f"{self.start_date.isoformat()}/{self.last_day.isoformat()}"

    def __str__(self):
        return f"{self.type} {self.label()} ({self.timezone_key})"

    # -- navigation ------------------------------------------------------

    def previous(self) -> Period:
        """The period of the same type immediately before this one. Contiguous
        by construction: `previous().end_date_exclusive == start_date`, and the
        same on instants. This is how "the current period plus the previous
        one" — the backdating window — is computed."""
        return _of_type(self.type, self.start_date - timedelta(days=1), self.timezone_key, self)

    def next(self) -> Period:
        return _of_type(self.type, self.end_date_exclusive, self.timezone_key, self)


# --------------------------------------------------------------------------
# Constructors — one per period family
# --------------------------------------------------------------------------


def day(value: date, tz) -> Period:
    """`DAY(d) = [B(d), B(d+1))`."""
    return Period(PeriodType.DAY, value, resolve_timezone(tz).key)


def week(value: date, tz) -> Period:
    """`WEEK(monday) = [B(monday), B(monday+7d))`, for the ISO week containing
    `value`. Monday-start, and never clipped to a month."""
    return Period(PeriodType.WEEK, iso_week_start(value), resolve_timezone(tz).key)


def biweek(value: date, tz) -> Period:
    """The biweekly half containing `value`: the 1st–15th or the 16th–end."""
    if value.day < BIWEEK_SECOND_HALF_DAY:
        return biweek_first_half(value.year, value.month, tz)
    return biweek_second_half(value.year, value.month, tz)


def biweek_first_half(year: int, month: int, tz) -> Period:
    """`H1(y,m) = [B(y-m-01), B(y-m-16))` — always 15 days."""
    return Period(PeriodType.BIWEEK_FIRST, date(year, month, 1), resolve_timezone(tz).key)


def biweek_second_half(year: int, month: int, tz) -> Period:
    """`H2(y,m) = [B(y-m-16), B(next_month-01))` — 13, 14, 15 or 16 days."""
    return Period(
        PeriodType.BIWEEK_SECOND,
        date(year, month, BIWEEK_SECOND_HALF_DAY),
        resolve_timezone(tz).key,
    )


def month(year: int, month_number: int, tz) -> Period:
    """`MONTH(y,m) = [B(y-m-01), B(next_month-01))`."""
    return Period(PeriodType.MONTH, date(year, month_number, 1), resolve_timezone(tz).key)


def custom(first_day: date, last_day: date, tz) -> Period:
    """`CUSTOM(d1..d2)`, inclusive as the user typed it, stored as
    `[B(d1), B(d2+1))`."""
    return Period(PeriodType.CUSTOM, first_day, resolve_timezone(tz).key, last_day)


def containing(period_type: PeriodType, value: date, tz) -> Period:
    """The period of `period_type` containing the calendar date `value`.

    `CUSTOM` has no canonical container — a custom range is whatever the user
    typed — so it is refused rather than invented.
    """
    return _of_type(period_type, value, resolve_timezone(tz).key, None)


def _of_type(period_type: PeriodType, value: date, tz_key: str, like: Period | None) -> Period:
    match period_type:
        case PeriodType.DAY:
            return day(value, tz_key)
        case PeriodType.WEEK:
            return week(value, tz_key)
        case PeriodType.BIWEEK_FIRST | PeriodType.BIWEEK_SECOND:
            return biweek(value, tz_key)
        case PeriodType.MONTH:
            return month(value.year, value.month, tz_key)
        case PeriodType.CUSTOM:
            if like is None:
                raise PeriodError(
                    "A custom range has no canonical period containing a date: "
                    "pass the two days the user chose to custom()."
                )
            # Neighbouring custom range: same width, butted up against this one.
            span = timedelta(days=like.days - 1)
            first = value if value >= like.start_date else value - span
            return custom(first, first + span, tz_key)
    raise AssertionError(f"unhandled period type {period_type}")  # pragma: no cover
