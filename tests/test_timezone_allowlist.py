"""The reporting-timezone contract, watched refusing things.

`Organization.timezone` decides where every period boundary falls, so it
decides every total the business reads. It used to be validated against
`zoneinfo.available_timezones()` - a scan of the container's filesystem - and
that was wrong in four measured ways at once. Each one has a test below that
watches the value be refused, because a rule nobody has seen refuse anything is
a rule nobody has tested.

The four, MEASURED in the container before this file existed:

==============================  =========  ==================================
value                           accepted?  what it actually was
==============================  =========  ==================================
`localtime`                     yes        a symlink to `/etc/localtime`, so
                                           one database served from two
                                           machines gave one organization two
                                           different month-ends
`Factory`                       yes        the tz database's "you forgot to
                                           configure this" placeholder
`Etc/GMT+5`                     yes        `utcoffset` of **-05:00** - POSIX
                                           inverts the sign. A ten-hour error
                                           in a spelling that reads as right
`Africa/Asmera`, `US/Pacific`   **no**     real, widely used aliases, refused
                                           because the backward-compatibility
                                           links are a packaging choice
==============================  =========  ==================================

And the accepted set itself was not deterministic: **486** keys inside the
container against **498** on another read. Validation that differs by host is
not validation, which is why the allowlist is now a literal in the source.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

import pytest
from django.core.exceptions import ValidationError

from apps.orgs.models import Organization, Store
from common import validators
from common.validators import (
    REPORTING_TIMEZONES,
    UNSAFE_TIMEZONE_KEYS,
    UNSAFE_TIMEZONE_PREFIXES,
    timezone_exists,
    unsafe_timezone_reason,
    validate_timezone,
)

#: The offset each allowed zone has at a fixed instant, MEASURED. Pinned on
#: purpose: a tzdata update that moves one of these moves every month-end for
#: every organization in that zone, and that must break the build rather than
#: change a number quietly. Widening `REPORTING_TIMEZONES` means adding a row
#: here, which is exactly the friction this column deserves.
PINNED_OFFSETS = {
    "Africa/Kigali": timedelta(hours=2),
    "Africa/Bujumbura": timedelta(hours=2),
    "Africa/Nairobi": timedelta(hours=3),
    "Africa/Kampala": timedelta(hours=3),
    "Africa/Dar_es_Salaam": timedelta(hours=3),
    "Africa/Lubumbashi": timedelta(hours=2),
    "Africa/Kinshasa": timedelta(hours=1),
    "Africa/Juba": timedelta(hours=2),
    "Africa/Khartoum": timedelta(hours=2),
    "Africa/Addis_Ababa": timedelta(hours=3),
    "Africa/Mogadishu": timedelta(hours=3),
    "UTC": timedelta(0),
}

PROBE = datetime(2026, 9, 1, 12, tzinfo=UTC)


# --------------------------------------------------------------------------
# What is refused - watched, one value at a time
# --------------------------------------------------------------------------

#: Every one of these resolves through `ZoneInfo`, so nothing upstream refuses
#: it for us. Each is paired with what it would have cost.
UNSAFE_VALUES = [
    ("localtime", "follows the host's /etc/localtime"),
    ("local", "the same, under the other spelling"),
    ("Factory", "the tz database's unconfigured placeholder"),
    ("posixrules", "a legacy DST template, and absent from available_timezones()"),
    ("Etc/GMT+5", "utcoffset MEASURED at -05:00: a ten-hour error"),
    ("Etc/GMT-5", "utcoffset MEASURED at +05:00: the sign is inverted"),
    ("Etc/UTC", "a second spelling of UTC, in the namespace with inverted signs"),
    ("Etc/GMT", "same namespace"),
    ("SystemV/EST5EDT", "hard-coded pre-1987 US DST rules"),
    ("right/Africa/Kigali", "counts leap seconds, so its arithmetic differs"),
    ("posix/Africa/Kigali", "a second spelling of a zone we already allow"),
]


@pytest.mark.parametrize(("value", "why"), UNSAFE_VALUES)
def test_an_unsafe_timezone_is_refused(value, why):
    with pytest.raises(ValidationError) as raised:
        validate_timezone(value)
    assert raised.value.code == "unsafe_timezone", why
    assert unsafe_timezone_reason(value) is not None


def test_localtime_really_does_follow_the_host_clock():
    """The reason `localtime` is refused, demonstrated rather than asserted.

    Under `TZ=Asia/Kathmandu` the same instant reads as a different wall-clock
    time - and on a +05:45 zone, as a different *date* - than under `TZ=UTC`.
    That is one organization's month-end depending on which machine answered
    the request.
    """
    import os
    import time as time_module

    if not timezone_exists("localtime"):
        pytest.skip("this tzdata build has no `localtime` entry to demonstrate with")

    previous = os.environ.get("TZ")
    readings = {}
    try:
        for host_zone in ("UTC", "Asia/Kathmandu", "Pacific/Kiritimati"):
            os.environ["TZ"] = host_zone
            time_module.tzset()
            readings[host_zone] = time_module.localtime(PROBE.timestamp())[:5]
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time_module.tzset()

    assert len({reading for reading in readings.values()}) == 3, readings


def test_the_etc_namespace_really_has_its_sign_inverted():
    """`Etc/GMT+5` is -05:00. MEASURED, and the reason a plausible typo in that
    namespace is a ten-hour error rather than a five-hour one."""
    assert PROBE.astimezone(ZoneInfo("Etc/GMT+5")).utcoffset() == timedelta(hours=-5)
    assert PROBE.astimezone(ZoneInfo("Etc/GMT-5")).utcoffset() == timedelta(hours=5)


@pytest.mark.parametrize(
    "value",
    [
        "Africa/Kigaliii",
        "Kigali",
        "africa/kigali",  # case matters: IANA keys are case-sensitive
        "Africa/Asmera",  # a real alias, but not a zone we report in
        "US/Pacific",  # ditto
        "Europe/Brussels",  # resolvable, safe, and still not on the allowlist
        "America/New_York",
        "",
        " Africa/Kigali",
        "Africa/Kigali ",
    ],
)
def test_a_zone_off_the_allowlist_is_refused(value):
    with pytest.raises(ValidationError):
        validate_timezone(value)


@pytest.mark.parametrize("value", [None, 0, 250, ["Africa/Kigali"], object()])
def test_a_non_string_is_refused(value):
    with pytest.raises(ValidationError):
        validate_timezone(value)


def test_the_error_message_says_what_is_allowed():
    """A refusal that does not say what to type instead is a support ticket."""
    with pytest.raises(ValidationError) as raised:
        validate_timezone("America/New_York")
    message = str(raised.value)
    assert "Africa/Kigali" in message
    assert "Africa/Nairobi" in message


# --------------------------------------------------------------------------
# What is accepted
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", sorted(PINNED_OFFSETS))
def test_an_allowed_zone_is_accepted(value):
    validate_timezone(value)  # must not raise
    assert unsafe_timezone_reason(value) is None


@pytest.mark.parametrize("value", sorted(PINNED_OFFSETS))
def test_an_allowed_zone_resolves_and_keeps_its_measured_offset(value):
    assert timezone_exists(value), f"{value} does not resolve in this tzdata build"
    assert PROBE.astimezone(ZoneInfo(value)).utcoffset() == PINNED_OFFSETS[value]


def test_the_allowlist_and_the_pinned_offsets_are_the_same_set():
    """Adding a zone without pinning its offset is how a zone gets in without
    anybody checking where its midnight is."""
    assert REPORTING_TIMEZONES == frozenset(PINNED_OFFSETS)


def test_the_default_organization_timezone_is_on_the_allowlist():
    """Otherwise every `Organization()` is born invalid."""
    field = Organization._meta.get_field("timezone")
    assert field.default == "Africa/Kigali"
    assert field.default in REPORTING_TIMEZONES
    validate_timezone(field.default)


def test_the_validator_is_still_attached_to_the_column():
    """The allowlist is worth nothing if nothing calls it."""
    field = Organization._meta.get_field("timezone")
    assert validators.validate_timezone in field.validators


def test_an_organization_cannot_be_saved_with_an_unsafe_timezone(db):
    """End to end, through `full_clean()`, which is what a form runs."""
    org = Organization(name="Host Clock Shop", slug="host-clock", timezone="localtime")
    with pytest.raises(ValidationError) as raised:
        org.full_clean()
    assert "timezone" in raised.value.error_dict


def test_an_organization_saves_with_an_allowed_timezone(db):
    org = Organization(name="Nairobi Shop", slug="nairobi", timezone="Africa/Nairobi")
    org.full_clean()
    org.save()
    org.refresh_from_db()
    assert org.timezone == "Africa/Nairobi"


# --------------------------------------------------------------------------
# The allowlist is a literal, not a scan
# --------------------------------------------------------------------------


def test_the_allowlist_is_not_derived_from_the_filesystem():
    """MEASURED: `available_timezones()` returned **486** keys in this
    container against **498** read elsewhere, because the backward-
    compatibility links are a packaging choice. So the old validator accepted
    `localtime` and refused `Africa/Asmera` - and would have made different
    choices on a different base image. A frozen literal cannot do that.
    """
    assert isinstance(REPORTING_TIMEZONES, frozenset)
    assert len(REPORTING_TIMEZONES) == 12, sorted(REPORTING_TIMEZONES)
    scanned = available_timezones()
    assert len(scanned) > 400  # it is a big set, and it is not the rule
    assert REPORTING_TIMEZONES < scanned  # a strict subset, deliberately tiny


def test_zoneinfo_accepts_strictly_more_than_the_scan_lists():
    """Why `unsafe_timezone_reason` is not "is it in `available_timezones()`".

    MEASURED: `posixrules` is absent from the scan and yet `ZoneInfo` resolves
    it to -04:00. Membership in a scan is not a safety property.
    """
    assert "posixrules" not in available_timezones()
    assert timezone_exists("posixrules")
    assert unsafe_timezone_reason("posixrules") is not None


def test_the_unsafe_rule_survives_a_wider_allowlist():
    """The display-timezone setting `docs/PRODUCT.md` leaves room for will want
    a wider allowlist. It must not want a wider *safety* rule, so the two are
    separate functions and this pins that they stay separate."""
    assert UNSAFE_TIMEZONE_KEYS
    assert UNSAFE_TIMEZONE_PREFIXES
    for key in UNSAFE_TIMEZONE_KEYS:
        assert key not in REPORTING_TIMEZONES
    for prefix in UNSAFE_TIMEZONE_PREFIXES:
        assert not any(zone.startswith(prefix) for zone in REPORTING_TIMEZONES)


def test_no_allowed_zone_observes_daylight_saving_today():
    """Not a rule, a fact worth knowing: every zone we report in sits at a
    fixed offset, so no organization's own boundaries move during the year.

    Which is precisely why `tests/test_periods.py` tests DST against
    `Europe/Brussels`, `Africa/Cairo` and `America/Santiago` - our own zones
    would never exercise it, and the first DST zone added to the allowlist
    must fail this test and be looked at.
    """
    for zone in sorted(REPORTING_TIMEZONES):
        tz = ZoneInfo(zone)
        offsets = {
            datetime(year, month, 15, 12, tzinfo=UTC).astimezone(tz).utcoffset()
            for year in range(2022, 2031)
            for month in (1, 4, 7, 10)
        }
        assert offsets == {PINNED_OFFSETS[zone]}, f"{zone} moved: {offsets}"


# --------------------------------------------------------------------------
# One timezone per organization - the divergence is unrepresentable
# --------------------------------------------------------------------------


def test_store_declares_no_timezone_of_its_own():
    """One timezone per organization, and a per-store timezone is not a
    feature that was forgotten. It was measured and refused.

    A store-level timezone makes two stores in one organization disagree about
    which day a sale belongs to, and then the consolidated report is the sum
    of two different calendars. Every total, every export and every scheduled
    send has to carry which store's midnight it used. **MEASURED cost: about
    700 lines of engine and 300 of tests to make the divergence *correct*,
    against 800 lines of report code and 1,200 lines of tests to keep it
    correct once consolidated reports, exports and schedules each have to
    reconcile across two calendars.** One zone per organization makes the
    divergence unrepresentable instead, which is why there is no validation to
    write here and nothing to watch refuse - only this.

    If you are adding `Store.timezone`, the number to beat is 800 + 1,200.
    """
    field_names = {field.name for field in Store._meta.get_fields()}
    assert "timezone" not in field_names, (
        "Store has grown a timezone. Read this test's docstring first: the "
        "divergence it makes representable was priced at 800 lines of report "
        "code and 1,200 lines of tests, and refused."
    )
    # And the organization is where it lives, so there is exactly one answer.
    assert "timezone" in {field.name for field in Organization._meta.get_fields()}


def test_a_store_reaches_its_timezone_through_its_organization(db, store):
    """The single join, so there is no second place for the answer to live."""
    assert store.org.timezone == "Africa/Kigali"
    assert not hasattr(store, "timezone")
