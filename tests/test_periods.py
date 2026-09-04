"""The period engine's test matrix.

Seven properties, and a case table of the zone-days where "midnight in the
organization's timezone" is most likely to be quietly wrong.

* **P1 partition** - every instant falls in exactly *one* period of a family.
  Asserted as `sum(contains) == 1`, never `>= 1`: `>=` passes on the
  double-count that an inclusive end bound produces, and the double-count is
  the actual defect.
* **P2 contiguity** - one period's end is the next one's start, on dates and on
  instants, with no gap and no overlap.
* **P3 monotonicity** - `B` is non-decreasing in the date, even across a
  calendar day that does not exist.
* **P4 reconciliation** - totals agree across groupings: a month equals its two
  biweekly halves, equals its days; a `business_date` filter and the equivalent
  instant filter select the same rows.
* **P5 server-independence** - the answer does not depend on
  `settings.TIME_ZONE`, on the host's `/etc/localtime`, or on the Postgres
  session `TimeZone`. Every test in this module runs three times under three
  server zones, one of them +14 and one with DST.
* **P6 determinism** - a re-run returns the identical value, from a cold cache.
* **P7 no forbidden query form** - `tests/test_query_forms.py` scans the tree.

Every expected instant below was MEASURED in the container against Python
3.14.7 / tzdata as shipped, and the naive `datetime(y, m, d, tzinfo=tz)` value
is recorded next to it wherever the two differ.
"""

import time as time_module
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, available_timezones

import pytest
from django.db import connection

from common import periods
from common.periods import (
    Period,
    PeriodError,
    PeriodType,
    boundary,
    local_date,
)
from tests.testapp.models import Thing

KIGALI = "Africa/Kigali"

# --------------------------------------------------------------------------
# P5 - the three-server-timezone harness
# --------------------------------------------------------------------------
#
# The single most valuable test here, because it is the shape of the defect
# that was actually shipped: with `TIME_ZONE = "UTC"` Django compiles UTC into
# every `__date` lookup and `Trunc*` without an explicit `tzinfo`, and
# `filter(at__date=...)` was MEASURED returning 300 where the Kigali truth was
# 500. Nothing in this module may notice which of the three it is running under.
#
# `Pacific/Kiritimati` is +14 - the largest positive offset in the database, so
# a UTC-flavoured bug shifts a whole day. `America/Anchorage` is negative *and*
# observes DST, so it moves during the year.
#
# Django's own `setting_changed` receiver for `TIME_ZONE` sets `os.environ["TZ"]`
# and calls `time.tzset()`, so assigning the setting moves the **process** clock
# too - which is exactly the `/etc/localtime` dependency we need covered.
# `test_the_harness_actually_moves_the_host_clock` proves the harness has teeth
# rather than assuming it.
SERVER_TIMEZONES = ("UTC", "Pacific/Kiritimati", "America/Anchorage")


@pytest.fixture(autouse=True, params=SERVER_TIMEZONES)
def server_timezone(request, settings):
    settings.TIME_ZONE = request.param
    # Cleared per run, or runs two and three would read run one's answers out
    # of the cache and prove nothing at all.
    periods._boundary.cache_clear()
    periods._zone.cache_clear()
    return request.param


def test_the_harness_actually_moves_the_host_clock(server_timezone):
    """A harness that does not change anything cannot catch anything."""
    assert time_module.tzname[0] not in ("", None)
    probe = datetime(2026, 7, 1, 12, tzinfo=UTC).timestamp()
    host_hour = time_module.localtime(probe).tm_hour
    expected = datetime(2026, 7, 1, 12, tzinfo=UTC).astimezone(ZoneInfo(server_timezone)).hour
    assert host_hour == expected, (
        f"the host clock is not following TIME_ZONE={server_timezone}; "
        "the /etc/localtime half of P5 is not being tested"
    )


# --------------------------------------------------------------------------
# B - the boundary function
# --------------------------------------------------------------------------

#: `(zone, local date, expected boundary instant)`. MEASURED, one by one.
BOUNDARY_CASES = [
    # Kigali, the default. +02:00 with no DST, ever.
    ("Africa/Kigali", date(2026, 9, 1), "2026-08-31T22:00:00+00:00"),
    ("Africa/Kigali", date(2026, 9, 16), "2026-09-15T22:00:00+00:00"),
    ("Africa/Kigali", date(2026, 10, 1), "2026-09-30T22:00:00+00:00"),
    # Pre-1935 Kigali LMT: +02:00:16, a sixteen-second, non-whole-minute
    # offset. PRODUCT.md allows historical import at any age, and a boundary
    # rounded to the minute here would be 16 seconds wrong for every day of it.
    ("Africa/Kigali", date(1930, 6, 1), "1930-05-31T21:59:44+00:00"),
    ("Africa/Kigali", date(1935, 1, 1), "1934-12-31T21:59:44+00:00"),
    # Local midnight DOES NOT EXIST: the clocks go 23:59:59 -> 01:00:00. This
    # is where "the boundary is local midnight" is most likely subtly wrong,
    # and Cairo makes it an African-expansion case rather than a hypothetical.
    ("Africa/Cairo", date(2026, 4, 24), "2026-04-23T22:00:00+00:00"),
    ("Africa/Cairo", date(2026, 4, 25), "2026-04-24T21:00:00+00:00"),
    ("Asia/Beirut", date(2026, 3, 29), "2026-03-28T22:00:00+00:00"),
    ("America/Santiago", date(2026, 9, 6), "2026-09-06T04:00:00+00:00"),
    # A forward gap STRADDLING midnight: 23:29:59 -05:00 -> 00:30:00 -04:00 at
    # 04:30Z. The naive fold=0 form answers 05:00:00Z, so half an hour of the
    # 31st would be reported on the 30th. The only measured shape where the
    # naive form is not the infimum.
    ("America/Toronto", date(1919, 3, 31), "1919-03-31T04:30:00+00:00"),
    ("America/Toronto", date(1919, 4, 1), "1919-04-01T04:00:00+00:00"),
    # A SKIPPED CALENDAR DAY. 2011-12-30 never happened in Samoa.
    ("Pacific/Apia", date(2011, 12, 29), "2011-12-29T10:00:00+00:00"),
    ("Pacific/Apia", date(2011, 12, 30), "2011-12-30T10:00:00+00:00"),
    ("Pacific/Apia", date(2011, 12, 31), "2011-12-30T10:00:00+00:00"),
    ("Pacific/Apia", date(2012, 1, 1), "2011-12-31T10:00:00+00:00"),
    # AMBIGUOUS midnight: a fall-back landing on 00:00, so 00:00 happens
    # twice. The boundary must take the EARLIER occurrence (02:00Z, not the
    # fold=1 04:00Z) or P1 breaks - the hour between them would belong to two
    # days at once.
    ("America/Goose_Bay", date(1988, 10, 30), "1988-10-30T02:00:00+00:00"),
    ("America/Goose_Bay", date(1988, 10, 31), "1988-10-31T04:00:00+00:00"),
    ("America/St_Johns", date(1987, 10, 25), "1987-10-25T02:30:00+00:00"),
    # Non-hour offsets.
    ("Asia/Kathmandu", date(2026, 9, 1), "2026-08-31T18:15:00+00:00"),
    ("Pacific/Marquesas", date(2026, 9, 1), "2026-09-01T09:30:00+00:00"),
    # DST, both directions, in a zone with ordinary 02:00/03:00 transitions.
    ("Europe/Brussels", date(2026, 3, 29), "2026-03-28T23:00:00+00:00"),
    ("Europe/Brussels", date(2026, 3, 30), "2026-03-29T22:00:00+00:00"),
    ("Europe/Brussels", date(2026, 10, 25), "2026-10-24T22:00:00+00:00"),
    ("Europe/Brussels", date(2026, 10, 26), "2026-10-25T23:00:00+00:00"),
    # Offset changes inside our OWN allowlist: South Sudan and Sudan both
    # moved +03 -> +02. No allowlisted zone observes DST today, so these two
    # are the only in-allowlist boundary movements there are.
    ("Africa/Juba", date(2021, 2, 1), "2021-01-31T22:00:00+00:00"),
    ("Africa/Juba", date(2021, 2, 2), "2021-02-01T22:00:00+00:00"),
    ("Africa/Khartoum", date(2017, 11, 1), "2017-10-31T22:00:00+00:00"),
]


@pytest.mark.parametrize(("zone", "day_value", "expected"), BOUNDARY_CASES)
def test_boundary_case_table(zone, day_value, expected):
    assert boundary(day_value, zone).isoformat() == expected


@pytest.mark.parametrize(("zone", "day_value", "expected"), BOUNDARY_CASES)
def test_boundary_is_always_utc_and_aware(zone, day_value, expected):
    result = boundary(day_value, zone)
    assert result.tzinfo is UTC
    assert result.utcoffset() == timedelta(0)


@pytest.mark.parametrize(("zone", "day_value", "expected"), BOUNDARY_CASES)
def test_boundary_satisfies_its_own_definition(zone, day_value, expected):
    """`B(d)` is in the set, and the instant before it is not.

    The definition read straight off the docstring: the earliest instant whose
    wall-clock date in the zone is `>= d`.
    """
    tz = ZoneInfo(zone)
    result = boundary(day_value, zone)
    assert result.astimezone(tz).date() >= day_value
    assert (result - timedelta(seconds=1)).astimezone(tz).date() < day_value


@pytest.mark.parametrize("zone", ["Africa/Cairo", "Asia/Beirut", "America/Santiago"])
def test_local_midnight_really_is_missing_in_the_gap_zones(zone):
    """The premise of the gap branch, asserted rather than assumed.

    If a tzdata update ever gives these zones an ordinary 02:00 transition,
    this fails and the case table above stops being about anything.
    """
    day_value = {
        "Africa/Cairo": date(2026, 4, 24),
        "Asia/Beirut": date(2026, 3, 29),
        "America/Santiago": date(2026, 9, 6),
    }[zone]
    tz = ZoneInfo(zone)
    wall = datetime.combine(day_value, time.min)
    assert wall.replace(tzinfo=tz).astimezone(UTC).astimezone(tz).replace(tzinfo=None) != wall


def test_the_naive_form_is_wrong_where_a_gap_straddles_midnight():
    """The measurement that justifies the gap branch existing at all.

    Not a hypothetical: `America/Toronto` and its three aliases, 1919-03-31.
    Delete `_gap_end` and this is the test that fails.
    """
    tz = ZoneInfo("America/Toronto")
    day_value = date(1919, 3, 31)
    naive = datetime.combine(day_value, time.min).replace(tzinfo=tz).astimezone(UTC)
    assert naive.isoformat() == "1919-03-31T05:00:00+00:00"
    assert boundary(day_value, tz).isoformat() == "1919-03-31T04:30:00+00:00"
    # The half hour the naive form misfiles.
    misfiled = datetime(1919, 3, 31, 4, 45, tzinfo=UTC)
    assert misfiled.astimezone(tz).date() == day_value
    assert periods.day(day_value, tz).contains_instant(misfiled)
    assert not periods.day(day_value - timedelta(days=1), tz).contains_instant(misfiled)


@pytest.mark.parametrize(
    "zone", ["Africa/Kigali", "Europe/Brussels", "Pacific/Apia", "America/Toronto"]
)
def test_boundary_is_non_decreasing(zone):
    """P3. Over 1900-01-01…2040-12-31 in one pass, including the days that do
    not exist."""
    tz = ZoneInfo(zone)
    day_value = date(1900, 1, 1)
    previous = boundary(day_value, tz)
    while day_value < date(2041, 1, 1):
        day_value += timedelta(days=1)
        current = boundary(day_value, tz)
        assert current >= previous, f"{zone}: B({day_value}) went backwards"
        previous = current


def test_boundary_of_a_skipped_calendar_day_is_a_zero_length_period():
    """`B` stays total: the day is empty, not 24 hours and not an exception."""
    empty = periods.day(date(2011, 12, 30), "Pacific/Apia")
    assert empty.duration == timedelta(0)
    assert empty.days == 1  # one calendar day on the label, zero elapsed time
    assert not empty.contains_instant(datetime(2011, 12, 30, 12, tzinfo=UTC))
    # And the day before it is still a full 24 hours, not 48.
    assert periods.day(date(2011, 12, 29), "Pacific/Apia").duration == timedelta(hours=24)


def test_brussels_march_is_23_hours_and_october_is_25():
    """Which is why `B` is never `start + timedelta(days=n)`."""
    assert periods.day(date(2026, 3, 29), "Europe/Brussels").duration == timedelta(hours=23)
    assert periods.day(date(2026, 10, 25), "Europe/Brussels").duration == timedelta(hours=25)
    assert periods.day(date(2026, 6, 1), "Europe/Brussels").duration == timedelta(hours=24)
    # A whole month, where the two cancel out to something that is still not
    # 24 * days.
    march = periods.month(2026, 3, "Europe/Brussels")
    assert march.days == 31
    assert march.duration == timedelta(days=31) - timedelta(hours=1)


def test_pre_1935_kigali_boundary_carries_its_sixteen_seconds():
    """A non-whole-minute historical offset, kept exactly."""
    assert boundary(date(1930, 6, 1), KIGALI).second == 44
    day_1930 = periods.day(date(1930, 6, 1), KIGALI)
    assert day_1930.duration == timedelta(hours=24)
    # MEASURED: the switch from LMT +02:00:16 to +02:00 happened at
    # 1935-05-31T22:00:00Z, so 1935-05-31 is the one day in Rwandan history
    # that is 24 hours and 16 seconds long - the clock went *back*. Scanned
    # across the whole of 1935, it is also the only one.
    long_days = {
        d: periods.day(d, KIGALI).duration
        for d in (date(1935, 5, 30), date(1935, 5, 31), date(1935, 6, 1))
    }
    assert long_days[date(1935, 5, 31)] == timedelta(hours=24, seconds=16), long_days
    assert long_days[date(1935, 5, 30)] == timedelta(hours=24)
    assert long_days[date(1935, 6, 1)] == timedelta(hours=24)
    assert periods.month(1935, 5, KIGALI).duration == timedelta(days=31, seconds=16)


# --------------------------------------------------------------------------
# tz-database invariants the implementation leans on
# --------------------------------------------------------------------------


#: Zone-days where local midnight happens TWICE. Postgres and Python disagree
#: here; see the two tests below. MEASURED.
AMBIGUOUS_MIDNIGHTS = [
    ("America/Goose_Bay", date(1988, 10, 30), "1988-10-30T02:00:00+00:00",
     "1988-10-30T04:00:00+00:00"),
    ("America/St_Johns", date(1987, 10, 25), "1987-10-25T02:30:00+00:00",
     "1987-10-25T03:30:00+00:00"),
]

_AMBIGUOUS = {(zone, day_value) for zone, day_value, _e, _l in AMBIGUOUS_MIDNIGHTS}


def _postgres_midnight(cursor, zone, day_value):
    cursor.execute(
        "SELECT (%s::timestamp AT TIME ZONE %s) AT TIME ZONE 'UTC'",
        [day_value.isoformat(), zone],
    )
    (wall,) = cursor.fetchone()
    return wall.replace(tzinfo=UTC)


def test_postgres_agrees_with_python_wherever_midnight_is_unambiguous(db):
    """The agreement the fast path is built on, asserted rather than assumed.

    MEASURED on Postgres 18.6 across every case in the table: for an ordinary
    day *and* for a day whose local midnight is missing entirely -
    `Africa/Cairo 2026-04-24`, `Asia/Beirut 2026-03-29`,
    `America/Santiago 2026-09-06`, `America/Toronto 1919-03-31`,
    `Pacific/Apia 2011-12-30` - `AT TIME ZONE` returns exactly Python's
    `fold=0` instant. The naive implementation is only correct *because* of
    this, so it is an assertion.
    """
    with connection.cursor() as cursor:
        for zone, day_value, _expected in BOUNDARY_CASES:
            if (zone, day_value) in _AMBIGUOUS:
                continue
            python_naive = (
                datetime.combine(day_value, time.min)
                .replace(tzinfo=ZoneInfo(zone))
                .astimezone(UTC)
            )
            assert _postgres_midnight(cursor, zone, day_value) == python_naive, (
                f"{zone} {day_value}: Postgres and Python fold=0 have diverged"
            )


@pytest.mark.parametrize(
    ("zone", "day_value", "earlier", "later"), AMBIGUOUS_MIDNIGHTS
)
def test_postgres_disagrees_on_an_ambiguous_midnight(db, zone, day_value, earlier, later):
    """A finding, not a formality - and the reason no boundary is ever computed
    in SQL.

    Where a fall-back lands on 00:00, local midnight happens twice. MEASURED on
    Postgres 18.6: `AT TIME ZONE` returns the **later** occurrence, which is
    Python's `fold=1`. `America/Goose_Bay 1988-10-30` puts them two hours
    apart; `America/St_Johns 1987-10-25`, one hour.

    Postgres' answer is the wrong one against the definition of `B`: at
    1988-10-30T02:00:00Z it is already the 30th in Goose Bay, so 04:00Z is not
    the earliest such instant. Taking the later occurrence would file the first
    two hours of the 30th under the 29th - and if one query computed its bound
    in Python and another in SQL, the same two hours would be counted twice.

    So: `common.periods` computes every boundary, in Python, and hands Postgres
    a finished instant. `tests/test_query_forms.py` is what keeps SQL from
    computing one.
    """
    tz = ZoneInfo(zone)
    wall = datetime.combine(day_value, time.min)
    assert wall.replace(tzinfo=tz, fold=0).astimezone(UTC).isoformat() == earlier
    assert wall.replace(tzinfo=tz, fold=1).astimezone(UTC).isoformat() == later

    with connection.cursor() as cursor:
        assert _postgres_midnight(cursor, zone, day_value).isoformat() == later

    # Ours takes the earlier occurrence, because that is what the definition
    # says and what P1 needs.
    assert boundary(day_value, zone).isoformat() == earlier
    first_instant = datetime.fromisoformat(earlier)
    assert first_instant.astimezone(tz).date() == day_value
    assert periods.day(day_value, zone).contains_instant(first_instant)
    assert not periods.day(day_value - timedelta(days=1), zone).contains_instant(
        first_instant
    )

    # And a limitation of the whole idea, stated where it is visible: between
    # the two midnights the local date dips BACK to the previous day, so no
    # half-open partition can agree with the local date everywhere. MEASURED at
    # `America/St_Johns 1987-10-25T03:00:00Z`, whose wall clock reads
    # 23:30 on the 24th while the instant is filed under the 25th.
    midway = first_instant + (datetime.fromisoformat(later) - first_instant) // 2
    assert midway.astimezone(tz).date() == day_value - timedelta(days=1)
    # The partition is still exact, which is what the totals depend on: the
    # instant is in one day and one only.
    days = [periods.day(day_value + timedelta(days=offset), zone) for offset in (-1, 0, 1)]
    assert sum(1 for one_day in days if one_day.contains_instant(midway)) == 1
    assert periods.day(day_value, zone).contains_instant(midway)


def test_no_two_transitions_share_a_day():
    """`_gap_end` bisects on the assumption of at most one transition in the
    bracket, and the bracket is at most one gap wide.

    MEASURED across every zone in the container: the smallest interval between
    two consecutive transitions in the whole database is 344,400 seconds, and
    nothing is closer than an hour. Read from the private transition table on
    purpose - this is a fact about the data, not about our code.
    """
    from zoneinfo._zoneinfo import ZoneInfo as PureZoneInfo

    smallest = None
    for key in sorted(available_timezones()):
        transitions = getattr(PureZoneInfo(key), "_trans_utc", None) or []
        for earlier, later in zip(transitions, transitions[1:], strict=False):
            gap = later - earlier
            if smallest is None or gap < smallest:
                smallest = gap
    assert smallest is not None
    assert smallest == 344400, f"smallest transition gap is now {smallest}s, was 344400s"


def test_a_missing_local_midnight_is_rare_but_real():
    """The gap branch is not dead code. Counted, so a tzdata change that makes
    it dead shows up as a failure and not as silent coverage rot."""
    zones_with_a_gap_at_midnight = []
    for zone in ("Africa/Cairo", "Asia/Beirut", "America/Santiago", "Pacific/Apia"):
        tz = ZoneInfo(zone)
        day_value = {
            "Africa/Cairo": date(2026, 4, 24),
            "Asia/Beirut": date(2026, 3, 29),
            "America/Santiago": date(2026, 9, 6),
            "Pacific/Apia": date(2011, 12, 30),
        }[zone]
        wall = datetime.combine(day_value, time.min)
        if wall.replace(tzinfo=tz).astimezone(UTC).astimezone(tz).replace(tzinfo=None) != wall:
            zones_with_a_gap_at_midnight.append(zone)
    assert len(zones_with_a_gap_at_midnight) == 4, zones_with_a_gap_at_midnight


# --------------------------------------------------------------------------
# Period families - the case table
# --------------------------------------------------------------------------


def test_day_range():
    period = periods.day(date(2026, 9, 15), KIGALI)
    assert period.date_range == (date(2026, 9, 15), date(2026, 9, 16))
    assert period.instant_range == (
        datetime(2026, 9, 14, 22, tzinfo=UTC),
        datetime(2026, 9, 15, 22, tzinfo=UTC),
    )
    assert period.label() == "2026-09-15/2026-09-15"


@pytest.mark.parametrize(
    ("inside", "monday"),
    [
        (date(2026, 8, 31), date(2026, 8, 31)),  # a Monday
        (date(2026, 9, 6), date(2026, 8, 31)),  # the Sunday that closes it
        (date(2026, 9, 1), date(2026, 8, 31)),  # a Tuesday
    ],
)
def test_week_is_monday_start(inside, monday):
    period = periods.week(inside, KIGALI)
    assert period.start_date == monday
    assert period.start_date.weekday() == 0
    assert period.days == 7
    assert period.last_day == monday + timedelta(days=6)


def test_a_week_is_never_clipped_to_a_month():
    """A week that opens on 30 March closes on 5 April, and is one week."""
    period = periods.week(date(2026, 3, 31), KIGALI)
    assert period.date_range == (date(2026, 3, 30), date(2026, 4, 6))
    assert period.days == 7
    assert period.last_day.month == 4


def test_a_week_anchored_off_monday_is_refused():
    with pytest.raises(PeriodError, match="starts on a Monday"):
        Period(PeriodType.WEEK, date(2026, 9, 1), KIGALI)  # a Tuesday


#: `(year, month, H1 days, H2 days, month days)`. MEASURED.
BIWEEK_DAY_COUNTS = [
    (2026, 2, 15, 13, 28),  # February, not a leap year
    (2027, 2, 15, 13, 28),  # February, not a leap year
    (2028, 2, 15, 14, 29),  # February, leap year
    (2026, 1, 15, 16, 31),
    (2026, 3, 15, 16, 31),
    (2026, 4, 15, 15, 30),
    (2026, 12, 15, 16, 31),
]


@pytest.mark.parametrize(
    ("year", "month_number", "first_days", "second_days", "month_days"), BIWEEK_DAY_COUNTS
)
def test_biweekly_halves_are_deliberately_unequal(
    year, month_number, first_days, second_days, month_days
):
    """1st-15th and 16th-end. The 28th/29th/30th/31st all fall in the second
    half, so the second half is 13, 14, 15 or 16 days and never assumed to be
    15."""
    first = periods.biweek_first_half(year, month_number, KIGALI)
    second = periods.biweek_second_half(year, month_number, KIGALI)
    whole = periods.month(year, month_number, KIGALI)

    assert first.days == first_days
    assert second.days == second_days
    assert whole.days == month_days
    assert first.days + second.days == whole.days

    assert first.date_range == (date(year, month_number, 1), date(year, month_number, 16))
    assert second.start_date == date(year, month_number, 16)
    assert second.last_day == date(year, month_number, month_days)


def test_december_second_half_rolls_into_january():
    """Where `month + 1` crashes rather than lies - and it must keep crashing.

    MEASURED: `datetime(2026, 13, 1)` raises
    `ValueError: month must be in 1..12, not 13`.
    """
    with pytest.raises(ValueError, match="month must be in 1..12"):
        date(2026, 13, 1)

    december = periods.biweek_second_half(2026, 12, KIGALI)
    assert december.date_range == (date(2026, 12, 16), date(2027, 1, 1))
    assert december.days == 16
    assert december.end_instant == boundary(date(2027, 1, 1), KIGALI)

    january = december.next()
    assert january.type is PeriodType.BIWEEK_FIRST
    assert january.date_range == (date(2027, 1, 1), date(2027, 1, 16))
    # P2 across the year boundary.
    assert december.end_date_exclusive == january.start_date
    assert december.end_instant == january.start_instant

    assert periods.month(2026, 12, KIGALI).next().date_range == (
        date(2027, 1, 1),
        date(2027, 2, 1),
    )
    assert periods.next_month(2026, 12) == (2027, 1)


@pytest.mark.parametrize(
    ("inside", "expected_type", "expected_start"),
    [
        (date(2026, 2, 1), PeriodType.BIWEEK_FIRST, date(2026, 2, 1)),
        (date(2026, 2, 15), PeriodType.BIWEEK_FIRST, date(2026, 2, 1)),
        (date(2026, 2, 16), PeriodType.BIWEEK_SECOND, date(2026, 2, 16)),
        (date(2026, 2, 28), PeriodType.BIWEEK_SECOND, date(2026, 2, 16)),
    ],
)
def test_biweek_containing_a_date(inside, expected_type, expected_start):
    period = periods.biweek(inside, KIGALI)
    assert period.type is expected_type
    assert period.start_date == expected_start
    assert period.contains_date(inside)


def test_custom_range_is_inclusive_as_typed_and_half_open_as_stored():
    period = periods.custom(date(2026, 9, 3), date(2026, 9, 20), KIGALI)
    assert period.date_range == (date(2026, 9, 3), date(2026, 9, 21))
    assert period.last_day == date(2026, 9, 20)
    assert period.days == 18
    assert period.label() == "2026-09-03/2026-09-20"
    assert period.contains_date(date(2026, 9, 20))
    assert not period.contains_date(date(2026, 9, 21))


def test_a_single_day_custom_range_is_a_single_day():
    period = periods.custom(date(2026, 9, 3), date(2026, 9, 3), KIGALI)
    assert period.days == 1
    assert period.instant_range == periods.day(date(2026, 9, 3), KIGALI).instant_range


def test_a_backwards_custom_range_is_refused():
    with pytest.raises(PeriodError, match="runs forwards"):
        periods.custom(date(2026, 9, 20), date(2026, 9, 3), KIGALI)


def test_containing_refuses_to_invent_a_custom_range():
    with pytest.raises(PeriodError, match="no canonical period"):
        periods.containing(PeriodType.CUSTOM, date(2026, 9, 3), KIGALI)


# --------------------------------------------------------------------------
# P1 partition and P2 contiguity
# --------------------------------------------------------------------------


def _instants_across(period, count=200):
    """`count` instants spread over `period`, plus both edges and the instant
    before the start."""
    start, end = period.instant_range
    span = end - start
    yield start - timedelta(microseconds=1)
    yield start
    for step in range(1, count):
        yield start + (span * step) // count
    yield end - timedelta(microseconds=1)
    yield end


@pytest.mark.parametrize(
    "zone",
    ["Africa/Kigali", "Europe/Brussels", "Pacific/Kiritimati", "Asia/Kathmandu"],
)
def test_days_of_a_month_partition_the_month_exactly_once(zone):
    """P1. `sum(contains) == 1`, not `>= 1`: `>=` is satisfied by the
    double-count an inclusive end produces, so it would pass on the defect."""
    whole = periods.month(2026, 10, zone)
    days = [
        periods.day(whole.start_date + timedelta(days=offset), zone)
        for offset in range(whole.days)
    ]
    for instant in _instants_across(whole, count=400):
        hits = sum(1 for one_day in days if one_day.contains_instant(instant))
        if whole.contains_instant(instant):
            assert hits == 1, f"{zone}: {instant} landed in {hits} days of October"
        else:
            assert hits == 0, f"{zone}: {instant} is outside October but landed in {hits} days"


@pytest.mark.parametrize("zone", ["Africa/Kigali", "Europe/Brussels", "Pacific/Apia"])
def test_biweekly_halves_partition_the_year_exactly_once(zone):
    halves = [
        half
        for month_number in range(1, 13)
        for half in (
            periods.biweek_first_half(2011, month_number, zone),
            periods.biweek_second_half(2011, month_number, zone),
        )
    ]
    year = periods.custom(date(2011, 1, 1), date(2011, 12, 31), zone)
    for instant in _instants_across(year, count=1000):
        hits = sum(1 for half in halves if half.contains_instant(instant))
        expected = 1 if year.contains_instant(instant) else 0
        assert hits == expected, f"{zone}: {instant} landed in {hits} halves of 2011"


@pytest.mark.parametrize("zone", ["Africa/Kigali", "Europe/Brussels", "Pacific/Apia"])
def test_business_dates_of_a_month_partition_it_exactly_once(zone):
    """The same partition on the column reports actually filter."""
    whole = periods.month(2026, 10, zone)
    days = [
        periods.day(whole.start_date + timedelta(days=offset), zone)
        for offset in range(whole.days)
    ]
    value = date(2026, 9, 28)
    while value <= date(2026, 11, 3):
        hits = sum(1 for one_day in days if one_day.contains_date(value))
        assert hits == (1 if whole.contains_date(value) else 0)
        value += timedelta(days=1)


@pytest.mark.parametrize(
    "period_type",
    [PeriodType.DAY, PeriodType.WEEK, PeriodType.MONTH, PeriodType.BIWEEK_FIRST],
)
@pytest.mark.parametrize("zone", ["Africa/Kigali", "Europe/Brussels", "Pacific/Apia"])
def test_a_period_and_its_neighbours_are_contiguous(period_type, zone):
    """P2, on dates and on instants, in both directions."""
    period = periods.containing(period_type, date(2011, 12, 28), zone)
    for _ in range(40):
        following = period.next()
        assert period.end_date_exclusive == following.start_date
        assert period.end_instant == following.start_instant
        assert following.previous() == period
        period = following


def test_previous_and_next_round_trip_for_a_custom_range():
    period = periods.custom(date(2026, 9, 3), date(2026, 9, 9), KIGALI)
    assert period.next().date_range == (date(2026, 9, 10), date(2026, 9, 17))
    assert period.previous().date_range == (date(2026, 8, 27), date(2026, 9, 3))
    assert period.previous().next() == period
    assert period.next().previous() == period


# --------------------------------------------------------------------------
# P4 reconciliation
# --------------------------------------------------------------------------

#: The Kigali biweekly boundary, to the microsecond. `at` instants, with the
#: `business_date` each one defaults to.
BOUNDARY_ROWS = [
    ("2026-09-01T09:00:00+02:00", date(2026, 9, 1), Decimal("20.00")),
    ("2026-09-10T13:30:00+02:00", date(2026, 9, 10), Decimal("20.00")),
    ("2026-09-15T23:59:59.999999+02:00", date(2026, 9, 15), Decimal("20.00")),
    ("2026-09-16T00:00:00.000000+02:00", date(2026, 9, 16), Decimal("20.00")),
    ("2026-09-16T00:00:00.000001+02:00", date(2026, 9, 16), Decimal("20.00")),
    ("2026-09-30T18:00:00+02:00", date(2026, 9, 30), Decimal("20.00")),
]


def test_the_business_date_a_row_defaults_to_is_the_local_date_of_its_instant():
    for raw, expected_business_date, _value in BOUNDARY_ROWS:
        instant = datetime.fromisoformat(raw)
        assert local_date(instant, KIGALI) == expected_business_date


def test_business_date_and_instant_filters_select_the_same_rows():
    """The hinge the whole design hangs from: `local_date(at) == d` if and only
    if `at` is in `DAY(d)`. If these two ever disagree, the sales list and the
    report disagree, and both look right."""
    first = periods.biweek_first_half(2026, 9, KIGALI)
    second = periods.biweek_second_half(2026, 9, KIGALI)
    for raw, expected_business_date, _value in BOUNDARY_ROWS:
        instant = datetime.fromisoformat(raw)
        for period in (first, second):
            assert period.contains_instant(instant) == period.contains_date(
                expected_business_date
            ), f"{raw} is filed differently by its instant and by its business date in {period}"


def test_half_open_reconciles_at_the_biweekly_boundary():
    """MEASURED: the half-open range totals 60.00 for the first half; the
    inclusive-end form totals 80.00, having counted the 00:00:00.000000 row in
    both halves."""
    first = periods.biweek_first_half(2026, 9, KIGALI)
    second = periods.biweek_second_half(2026, 9, KIGALI)
    rows = [(datetime.fromisoformat(raw), value) for raw, _d, value in BOUNDARY_ROWS]
    grand_total = sum(value for _instant, value in rows)

    first_total = sum(v for i, v in rows if first.contains_instant(i))
    second_total = sum(v for i, v in rows if second.contains_instant(i))
    assert first_total == Decimal("60.00")
    assert second_total == Decimal("60.00")
    assert first_total + second_total == grand_total == Decimal("120.00")

    # The defect, reproduced: an inclusive end on the same bounds.
    inclusive_first = sum(
        v for i, v in rows if first.start_instant <= i <= first.end_instant
    )
    assert inclusive_first == Decimal("80.00")
    assert inclusive_first + second_total > grand_total


def test_month_reconciles_across_every_grouping():
    """P4. The same month, added up four ways, in a zone that changes offset
    mid-month so the groupings cannot agree by accident."""
    zone = "Europe/Brussels"
    whole = periods.month(2026, 10, zone)
    halves = [
        periods.biweek_first_half(2026, 10, zone),
        periods.biweek_second_half(2026, 10, zone),
    ]
    days = [
        periods.day(whole.start_date + timedelta(days=offset), zone)
        for offset in range(whole.days)
    ]

    assert sum(half.days for half in halves) == whole.days
    assert sum(one_day.days for one_day in days) == whole.days
    assert sum((half.duration for half in halves), timedelta()) == whole.duration
    assert sum((one_day.duration for one_day in days), timedelta()) == whole.duration
    # And the month is genuinely not 31 * 24h, so the equalities above are not
    # trivially true.
    assert whole.duration == timedelta(days=31) + timedelta(hours=1)


def test_instant_lookups_select_exactly_the_period_rows_through_the_orm(db, actor):
    """The same reconciliation through Django and Postgres rather than in
    Python. `Thing.created_at` is a `timestamptz` standing in for `at`."""
    first = periods.biweek_first_half(2026, 9, KIGALI)
    second = periods.biweek_second_half(2026, 9, KIGALI)
    for index, (raw, _business_date, _value) in enumerate(BOUNDARY_ROWS):
        row = Thing.objects.create(name=f"row-{index}", created_by=actor)
        Thing.objects.filter(pk=row.pk).update(created_at=datetime.fromisoformat(raw))

    in_first = Thing.objects.filter(**first.instant_lookups("created_at")).count()
    in_second = Thing.objects.filter(**second.instant_lookups("created_at")).count()
    assert in_first == 3
    assert in_second == 3
    assert in_first + in_second == Thing.objects.count() == len(BOUNDARY_ROWS)


def test_the_only_lookups_a_period_compiles_to_are_gte_and_lt(db):
    """Deliverable 2, item 1, at the point of use."""
    period = periods.biweek_first_half(2026, 9, KIGALI)
    assert set(period.instant_lookups("created_at")) == {
        "created_at__gte",
        "created_at__lt",
    }
    assert set(period.date_lookups("business_date")) == {
        "business_date__gte",
        "business_date__lt",
    }
    sql = str(Thing.objects.filter(**period.instant_lookups("created_at")).query)
    # periods: allow asserting the absence of the refused keyword
    assert " BETWEEN " not in sql.upper()
    assert '"created_at" >= ' in sql
    assert '"created_at" < ' in sql
    assert '"created_at" <= ' not in sql


def test_between_double_counts_the_boundary_row_in_postgres(db):
    """The defect in SQL, on a real `business_date` column, watched happening.

    No model carries `business_date` yet, so this is a temporary table - which
    also means the assertion is about Postgres' semantics rather than about the
    ORM's rendering of them.
    """
    first = periods.biweek_first_half(2026, 9, KIGALI)
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMPORARY TABLE period_probe ("
            "  at timestamptz NOT NULL,"
            "  business_date date NOT NULL,"
            "  value numeric(12, 2) NOT NULL)"
        )
        cursor.executemany(
            "INSERT INTO period_probe (at, business_date, value) VALUES (%s, %s, %s)",
            [(raw, business_date, value) for raw, business_date, value in BOUNDARY_ROWS],
        )

        start_date, end_date = first.date_range
        cursor.execute(
            "SELECT COALESCE(SUM(value), 0) FROM period_probe "
            "WHERE business_date >= %s AND business_date < %s",
            [start_date, end_date],
        )
        (half_open,) = cursor.fetchone()

        # The defect: `BETWEEN` is inclusive at both ends, so the half-open
        # end bound sweeps in the whole first day of the next period.
        cursor.execute(
            # periods: allow reproducing the double-count is this test's subject
            "SELECT COALESCE(SUM(value), 0) FROM period_probe "
            "WHERE business_date BETWEEN %s AND %s",
            [start_date, end_date],
        )
        (between_on_the_exclusive_end,) = cursor.fetchone()

        start_instant, end_instant = first.instant_range
        cursor.execute(
            "SELECT COALESCE(SUM(value), 0) FROM period_probe WHERE at >= %s AND at < %s",
            [start_instant, end_instant],
        )
        (half_open_instants,) = cursor.fetchone()

        # And on instants, where the overcount is exactly the boundary row.
        cursor.execute(
            # periods: allow reproducing the double-count is this test's subject
            "SELECT COALESCE(SUM(value), 0) FROM period_probe WHERE at BETWEEN %s AND %s",
            [start_instant, end_instant],
        )
        (between_on_instants,) = cursor.fetchone()

        second = periods.biweek_second_half(2026, 9, KIGALI)
        cursor.execute(
            "SELECT COALESCE(SUM(value), 0) FROM period_probe "
            "WHERE business_date >= %s AND business_date < %s",
            list(second.date_range),
        )
        (second_half,) = cursor.fetchone()
        cursor.execute("SELECT COALESCE(SUM(value), 0) FROM period_probe")
        (grand_total,) = cursor.fetchone()

    # MEASURED. The half-open form reconciles: the two halves add up to the
    # whole and nothing is counted twice.
    assert half_open == Decimal("60.00")
    assert half_open_instants == Decimal("60.00")
    assert second_half == Decimal("60.00")
    assert half_open + second_half == grand_total == Decimal("120.00")

    # The inclusive form does not. On instants it overcounts by exactly the
    # 00:00:00.000000 row - 20.00. On dates the end bound is a whole day, so it
    # overcounts by both rows dated the 16th - 40.00.
    assert between_on_instants == Decimal("80.00")
    assert between_on_instants - half_open_instants == Decimal("20.00")
    assert between_on_the_exclusive_end == Decimal("100.00")
    assert between_on_the_exclusive_end - half_open == Decimal("40.00")
    assert between_on_the_exclusive_end + second_half > grand_total


# --------------------------------------------------------------------------
# P5 server-independence, at the database
# --------------------------------------------------------------------------


def test_the_result_does_not_follow_the_postgres_session_timezone(db):
    """The bounds are `date` and `timestamptz` values computed in Python, so
    the session GUC has nothing to change. Watched under three settings, one of
    them +14, with the `__date`-shaped query alongside it losing rows - which
    is the defect this contract exists to prevent."""
    first = periods.biweek_first_half(2026, 9, KIGALI)
    start_date, end_date = first.date_range
    start_instant, end_instant = first.instant_range

    half_open_totals = {}
    at_time_zone_totals = {}
    with connection.cursor() as cursor:
        cursor.execute("SHOW TimeZone")
        (original_guc,) = cursor.fetchone()
        cursor.execute(
            "CREATE TEMPORARY TABLE period_probe ("
            "  at timestamptz NOT NULL, business_date date NOT NULL,"
            "  value numeric(12, 2) NOT NULL)"
        )
        cursor.executemany(
            "INSERT INTO period_probe (at, business_date, value) VALUES (%s, %s, %s)",
            [(raw, business_date, value) for raw, business_date, value in BOUNDARY_ROWS],
        )
        try:
            for guc in ("UTC", "Pacific/Kiritimati", "America/Anchorage", KIGALI):
                cursor.execute(f"SET TimeZone TO '{guc}'")
                cursor.execute(
                    "SELECT COALESCE(SUM(value), 0) FROM period_probe "
                    "WHERE business_date >= %s AND business_date < %s",
                    [start_date, end_date],
                )
                (total,) = cursor.fetchone()
                cursor.execute(
                    "SELECT COALESCE(SUM(value), 0) FROM period_probe "
                    "WHERE at >= %s AND at < %s",
                    [start_instant, end_instant],
                )
                (instant_total,) = cursor.fetchone()
                assert total == instant_total
                half_open_totals[guc] = total

                # The forbidden shape, for contrast: a date truncation that
                # takes its zone from the session instead of from the
                # organization.
                cursor.execute(
                    # periods: allow the session-dependent form is the contrast
                    "SELECT COALESCE(SUM(value), 0) FROM period_probe "
                    "WHERE at::date >= %s AND at::date < %s",
                    [start_date, end_date],
                )
                (session_dated,) = cursor.fetchone()
                at_time_zone_totals[guc] = session_dated
        finally:
            cursor.execute(f"SET TimeZone TO '{original_guc}'")

    assert set(half_open_totals.values()) == {Decimal("60.00")}, half_open_totals
    # And the session-dependent form really does move, so the test above is
    # not passing because nothing in the environment changed.
    assert len(set(at_time_zone_totals.values())) > 1, at_time_zone_totals


def test_boundaries_do_not_follow_settings_time_zone(server_timezone):
    """Recomputed from a cold cache under each of the three server zones."""
    assert boundary(date(2026, 9, 16), KIGALI) == datetime(2026, 9, 15, 22, tzinfo=UTC)
    assert periods.month(2026, 2, KIGALI).date_range == (
        date(2026, 2, 1),
        date(2026, 3, 1),
    )
    assert periods.biweek_second_half(2026, 2, KIGALI).days == 13


# --------------------------------------------------------------------------
# P6 determinism
# --------------------------------------------------------------------------


def test_the_same_period_computed_twice_is_identical():
    first = periods.biweek_second_half(2026, 2, KIGALI)
    periods._boundary.cache_clear()
    again = periods.biweek_second_half(2026, 2, KIGALI)
    assert first == again
    assert first.key == again.key
    assert first.instant_range == again.instant_range
    assert hash(first) == hash(again)


def test_a_period_is_immutable():
    """A boundary that can be reassigned after the fact is a boundary that can
    disagree with the label printed on the report."""
    period = periods.month(2026, 9, KIGALI)
    with pytest.raises((AttributeError, TypeError)):
        period.start_date = date(2026, 9, 2)


# --------------------------------------------------------------------------
# The canonical key and the derived label
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period", "expected_key", "expected_label"),
    [
        (
            periods.day(date(2026, 9, 15), KIGALI),
            "1|day|2026-09-15||Africa/Kigali",
            "2026-09-15/2026-09-15",
        ),
        (
            periods.week(date(2026, 9, 3), KIGALI),
            "1|week|2026-08-31||Africa/Kigali",
            "2026-08-31/2026-09-06",
        ),
        (
            periods.biweek_first_half(2026, 9, KIGALI),
            "1|biweek-first|2026-09-01||Africa/Kigali",
            "2026-09-01/2026-09-15",
        ),
        (
            periods.biweek_second_half(2026, 2, KIGALI),
            "1|biweek-second|2026-02-16||Africa/Kigali",
            "2026-02-16/2026-02-28",
        ),
        (
            periods.month(2026, 9, KIGALI),
            "1|month|2026-09-01||Africa/Kigali",
            "2026-09-01/2026-09-30",
        ),
        (
            periods.custom(date(2026, 9, 3), date(2026, 9, 20), KIGALI),
            "1|custom|2026-09-03|2026-09-20|Africa/Kigali",
            "2026-09-03/2026-09-20",
        ),
    ],
)
def test_key_round_trips_and_label_is_derived(period, expected_key, expected_label):
    assert period.key == expected_key
    assert Period.parse(expected_key) == period
    assert Period.parse(period.key).instant_range == period.instant_range
    assert period.label() == expected_label


def test_the_key_carries_its_zone():
    """A period without its zone is not a period. The same named month in two
    zones is two different windows, and the key has to say which."""
    kigali = periods.month(2026, 9, KIGALI)
    utc = periods.month(2026, 9, "UTC")
    assert kigali.key != utc.key
    assert kigali != utc
    assert kigali.date_range == utc.date_range  # same calendar days...
    assert kigali.instant_range != utc.instant_range  # ...different instants
    assert kigali.label() == utc.label()  # and the same human label


def test_a_label_is_not_a_key():
    """`tests/test_audit.py:453` already stores `2026-09-01/2026-09-15` in an
    audit payload. That inclusive-end string is a fine label and must never be
    read back as a query bound: read as half-open it would run to the 15th
    exclusive and lose a day; read as inclusive it double-counts the 16th."""
    label = periods.biweek_first_half(2026, 9, KIGALI).label()
    assert label == "2026-09-01/2026-09-15"
    with pytest.raises(PeriodError, match="is not a period key"):
        Period.parse(label)


@pytest.mark.parametrize(
    "bad_key",
    [
        "2|month|2026-09-01||Africa/Kigali",  # unknown version
        "1|fortnight|2026-09-01||Africa/Kigali",  # unknown type
        "1|month|2026-09-01|Africa/Kigali",  # too few fields
        "1|month|September||Africa/Kigali",  # not ISO-8601
        "1|month|2026-09-01||localtime",  # unsafe zone
        "1|month|2026-09-01||Etc/GMT+5",  # unsafe zone
        "1|month|2026-09-02||Africa/Kigali",  # a month that starts on the 2nd
        "1|biweek-second|2026-09-15||Africa/Kigali",  # H2 starts on the 16th
    ],
)
def test_a_malformed_key_is_refused_not_guessed_at(bad_key):
    with pytest.raises(PeriodError):
        Period.parse(bad_key)


def test_parse_refuses_a_non_string():
    with pytest.raises(PeriodError, match="is a string"):
        Period.parse(20260901)


# --------------------------------------------------------------------------
# The timezone is a parameter
# --------------------------------------------------------------------------


class _OrgLike:
    """Stands in for `orgs.Organization`, which the engine deliberately does
    not import: taking the zone as a parameter is what makes all of the above
    runnable without a database."""

    def __init__(self, timezone_value):
        self.timezone = timezone_value


def test_the_engine_reads_a_timezone_off_anything_that_exposes_one():
    expected = boundary(date(2026, 9, 1), KIGALI)
    for source in (
        KIGALI,
        ZoneInfo(KIGALI),
        _OrgLike(KIGALI),
        _OrgLike(ZoneInfo(KIGALI)),
    ):
        assert boundary(date(2026, 9, 1), source) == expected
        assert periods.month(2026, 9, source).timezone_key == KIGALI


def test_a_real_organization_is_an_acceptable_timezone_source(db, org):
    """Read-only against `orgs.Organization`, whose wiring belongs in the org
    services layer and not here."""
    assert org.timezone == KIGALI
    assert periods.month(2026, 9, org).instant_range == periods.month(
        2026, 9, KIGALI
    ).instant_range


@pytest.mark.parametrize(
    "source", [None, 0, object(), _OrgLike(None), _OrgLike(""), _OrgLike(42)]
)
def test_a_missing_timezone_is_refused_rather_than_defaulted(source):
    """There is no fallback to `settings.TIME_ZONE`, deliberately. A default
    here is how a report silently becomes UTC-bounded."""
    with pytest.raises(PeriodError, match="Pass a timezone"):
        boundary(date(2026, 9, 1), source)


@pytest.mark.parametrize("unsafe", ["localtime", "Factory", "Etc/GMT+5", "posixrules"])
def test_the_engine_refuses_an_unsafe_zone_even_from_a_stored_row(unsafe):
    """The field validator guards writes; this guards a row written before the
    validator existed. Both, because a report is computed from the row."""
    with pytest.raises(PeriodError, match="cannot bound a reporting period"):
        boundary(date(2026, 9, 1), unsafe)
    with pytest.raises(PeriodError, match="cannot bound a reporting period"):
        periods.month(2026, 9, _OrgLike(unsafe))


def test_an_unknown_zone_is_refused():
    with pytest.raises(PeriodError, match="not a known time zone"):
        boundary(date(2026, 9, 1), "Africa/Kigaliii")


# --------------------------------------------------------------------------
# business_date - the user-settable half
# --------------------------------------------------------------------------


def test_local_date_refuses_a_naive_datetime():
    """A naive instant is one whose zone somebody forgot. Reading it as UTC
    moves a Kigali sale recorded at 01:00 back onto the previous day."""
    with pytest.raises(PeriodError, match="aware datetime"):
        local_date(datetime(2026, 9, 16, 1, 0), KIGALI)


def test_a_backdated_entry_lands_in_the_period_its_business_date_names():
    """Recorded at 00:20 on the 16th, business date the 15th: the sale belongs
    to the first half, and the report has to say so."""
    recorded_at = datetime.fromisoformat("2026-09-16T00:20:00+02:00")
    first = periods.biweek_first_half(2026, 9, KIGALI)
    second = periods.biweek_second_half(2026, 9, KIGALI)

    # By its recording instant it is in the second half...
    assert second.contains_instant(recorded_at)
    assert not first.contains_instant(recorded_at)
    # ...and by the business date the user set, it is in the first.
    business_date = date(2026, 9, 15)
    assert first.contains_date(business_date)
    assert not second.contains_date(business_date)
    # Which is the whole reason every period query filters on business_date.
    assert first.date_lookups() == {
        "business_date__gte": date(2026, 9, 1),
        "business_date__lt": date(2026, 9, 16),
    }


@pytest.mark.parametrize(
    ("business_date", "expected"),
    [
        (date(2026, 9, 15), False),
        (date(2026, 9, 16), False),  # today in Kigali
        (date(2026, 9, 17), True),
    ],
)
def test_a_future_business_date_is_refused_always(business_date, expected):
    """No permission unlocks this: a total for a period that has not finished
    is not late data, it is fiction. `now` is 00:20 on the 16th in Kigali,
    which is still the 15th in UTC - so a UTC-flavoured implementation would
    accept the 16th as future and refuse it."""
    now = datetime.fromisoformat("2026-09-16T00:20:00+02:00")
    assert local_date(now, KIGALI) == date(2026, 9, 16)
    assert local_date(now, "UTC") == date(2026, 9, 15)
    assert periods.is_future_business_date(business_date, now, KIGALI) is expected


# --------------------------------------------------------------------------
# Calendar arithmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "month_number", "expected"),
    [(2026, 1, (2026, 2)), (2026, 11, (2026, 12)), (2026, 12, (2027, 1))],
)
def test_next_month_carries_the_year(year, month_number, expected):
    assert periods.next_month(year, month_number) == expected


@pytest.mark.parametrize(
    ("year", "month_number", "expected"),
    [
        (2026, 2, date(2026, 2, 28)),
        (2028, 2, date(2028, 2, 29)),
        (2100, 2, date(2100, 2, 28)),  # not a leap year: divisible by 100
        (2000, 2, date(2000, 2, 29)),  # a leap year: divisible by 400
        (2026, 12, date(2026, 12, 31)),
    ],
)
def test_last_day_of_month(year, month_number, expected):
    assert periods.last_day_of_month(year, month_number) == expected


def test_leap_day_is_an_ordinary_day():
    leap_day = periods.day(date(2028, 2, 29), KIGALI)
    assert leap_day.duration == timedelta(hours=24)
    assert periods.month(2028, 2, KIGALI).contains_date(date(2028, 2, 29))
    assert periods.biweek_second_half(2028, 2, KIGALI).contains_date(date(2028, 2, 29))
    assert periods.biweek_second_half(2028, 2, KIGALI).last_day == date(2028, 2, 29)
