"""Reusable field validators.

User-facing messages are wrapped in `gettext_lazy`: they surface in forms.
"""

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, RegexValidator
from django.utils.translation import gettext_lazy as _
from PIL import Image, UnidentifiedImageError

# --------------------------------------------------------------------------
# Phone numbers
# --------------------------------------------------------------------------
#
# A phone number is an *identifier* here: unique per account and one of the
# three things a person can log in with. So the only thing that matters is that
# one human phone maps to exactly one stored string. `PHONE_REGEX` alone never
# gave that - it checks shape, not identity, and `788123456` and
# `250788123456` are the same SIM under two strings it both accepted, in two
# rows `unique=True` had no grounds to refuse. Anything that reaches the
# column, or looks the column up, goes through `normalize_phone()` first.
#
# Rwanda's numbering plan, checked rather than assumed. Primary source: the
# National Numbering Plan RURA communicated to ITU-T on 14.X.2009, published as
# "Rwanda (country code +250)" (ITU doc T02020000AE) - country code 250,
# national significant number minimum 8 and maximum 9 digits, destination codes
# 25 geographic/fixed, 72 / 75 / 78 mobile, 06 satellite (NSN 8). That document
# predates today's operators, so it is cross-checked against Google's
# libphonenumber metadata for territory RW, which is maintained: national
# prefix 0, international prefix 00, mobile `7[237-9]xxxxxxx`, fixed
# `(?:06|2[23568]x)xxxxxx`, toll-free `800xxxxxx`, premium `900xxxxxx` - all 9
# digits except the 8-digit satellite range.
#
# So a Rwandan subscriber number is 9 digits opening with 2 (fixed) or 7
# (mobile), written locally with the trunk prefix as 0788 123 456, and 250 +
# those 9 is the 12-digit canonical form. Three ranges from the plan are
# deliberately *not* accepted for a user account, and each exclusion is a
# choice, not an oversight:
#
#   * 800 / 900 - toll-free and premium rate are not subscriber lines. Nobody
#     signs up with one, and a premium-rate number in a field we will later
#     send OTPs to is a billing-fraud shape.
#   * 06 satellite - its destination code itself starts with a zero, which
#     cannot be told apart from the trunk prefix every real user relies on
#     (`06123456` reads as trunk + a 7-digit number). Rwanda Satellite is
#     defunct and libphonenumber records no live example; the ambiguity is not
#     worth carrying for a range with no subscribers. Widening this is one
#     regex, `RWANDA_NSN_REGEX`.
#
# Numbers from other countries are accepted only in full international form
# (`+254712345678`), because a bare national number cannot be attributed to a
# country and guessing would re-create the exact ambiguity this closes. We do
# not validate another country's plan - that needs libphonenumber's dataset,
# which is a dependency and a monthly data refresh; the shape check below is
# E.164's own bound and the honest limit of what we know.

#: The canonical **stored** form: E.164 without the plus. Every value written
#: to a phone column matches this, and `normalize_phone()` is what makes that
#: true. Kept as the field validator too, as a floor under the normaliser.
PHONE_REGEX = r"^[1-9][0-9]{7,14}$"

RWANDA_COUNTRY_CODE = "250"
RWANDA_TRUNK_PREFIX = "0"
RWANDA_NSN_LENGTH = 9

#: A Rwandan national significant number: 9 digits, fixed (2…) or mobile (7…).
#: Deliberately not the exact allocated destination codes (25, 72, 73, 78, 79):
#: RURA allocates new mobile codes, and a plan-perfect regex would refuse a
#: real new number at signup - the same friction bug in a new coat. Opening
#: digit is enough to keep the canonical space unambiguous, which is what this
#: is for.
RWANDA_NSN_REGEX = re.compile(r"^[27][0-9]{8}$")

#: A Rwandan number in canonical form: 250 + 9 digits.
_RWANDA_E164_LENGTH = len(RWANDA_COUNTRY_CODE) + RWANDA_NSN_LENGTH

#: E.164: 15 digits maximum, and no country code starts with 0.
_E164_REGEX = re.compile(r"^[1-9][0-9]{7,14}$")
_ASCII_DIGITS_REGEX = re.compile(r"^[0-9]+$")

#: Presentation only, in every convention: `+250 788 123 456`,
#: `(078) 812-3456`, `0788.123.456`. Stripped before anything is decided,
#: because a login identifier typed with the spaces the owner's own contact
#: card shows must resolve to their account. Deliberately not `\s`: a newline
#: or a carriage return is not a separator in any convention, it is two fields
#: pasted into one, and `\s` would silently splice them into one number.
_PHONE_SEPARATORS_REGEX = re.compile("[ \t\u00a0\u202f\u2009().\u2013\u2014-]+")

#: The widest *input* we will look at: `+250 788 123 456` is 16 characters and
#: a 15-digit international number written in groups can reach ~20. Bounds the
#: work done on hostile input, and is what the form field is sized to - the
#: column stores at most 15.
PHONE_INPUT_MAX_LENGTH = 24

_PHONE_HELP = _("Write a Rwandan number as 0788123456, or another country's as +254712345678.")

phone_validator = RegexValidator(
    regex=PHONE_REGEX,
    message=_PHONE_HELP,
    code="invalid_phone",
)


def _invalid_phone(message) -> ValidationError:
    return ValidationError(message, code="invalid_phone")


def normalize_phone(value) -> str:
    """Return the canonical stored form of `value`, or raise `ValidationError`.

    ::

        0788123456     -> 250788123456   (trunk prefix, how Rwandans write it)
        +250788123456  -> 250788123456
        250788123456   -> 250788123456
        788123456      -> 250788123456   (bare national: assume Rwanda)
        +254712345678  -> 254712345678   (another country, kept in full)

    **Where this runs.** `apps.accounts.models.PhoneField` calls it from
    `to_python()` (so `full_clean()` and every ModelForm normalise before the
    field validators, before `clean()`, and before `validate_unique()` runs its
    query), from `pre_save()` (so `save()`, `objects.create()`,
    `bulk_create()`, `loaddata` and the admin normalise even though they never
    call `full_clean()`) and from `get_prep_value()` (so `QuerySet.update()`,
    `bulk_update()` and an `=` lookup normalise at the SQL layer). Uniqueness -
    the unique index, and the `validate_unique()` query in front of it - is
    therefore always evaluated on canonical strings.

    **Closure.** Every string this returns normalises to itself
    (`tests/test_phone_normalization.py` fuzzes the property). Without that, a
    number could be stored under one string and looked up under another, which
    is the original defect one layer down.

    **At the login box** (task 5's multi-identifier backend), the identifier a
    person typed must come through here before it is compared with the stored
    column, or whoever signed up as `0788123456` cannot log in with it. An
    identifier that is not a phone number is not an error there, so ask
    forgiveness::

        try:
            phone = normalize_phone(identifier)
        except ValidationError:
            phone = None  # a username or an email address, then
    """
    if not isinstance(value, str):
        # An int has already lost the leading zero that decides the country,
        # and `None` is "no number" - neither is ours to guess at.
        raise _invalid_phone(_("Enter a phone number.") if value is None else _PHONE_HELP)

    raw = value.strip()
    if not raw:
        raise _invalid_phone(_("Enter a phone number."))
    if len(raw) > PHONE_INPUT_MAX_LENGTH:
        # Refused on width before any scanning: nothing this long is a number.
        raise _invalid_phone(_PHONE_HELP)

    plus_prefixed = raw.startswith("+")
    digits = _PHONE_SEPARATORS_REGEX.sub("", raw[1:] if plus_prefixed else raw)
    # `[0-9]`, never `\d`: `\d` matches Arabic-Indic and full-width digits, and
    # `٢٥٠…` stored next to `250…` is two strings for one number again. A `+`
    # anywhere but the front survives the strip and is caught here too.
    if not _ASCII_DIGITS_REGEX.fullmatch(digits):
        raise _invalid_phone(_PHONE_HELP)

    if digits.startswith("00"):
        # `00` is the prefix you *dial* to leave the country, not part of any
        # number. Refused rather than guessed at, because a leading zero is
        # already claimed by the trunk prefix.
        raise _invalid_phone(
            _("Do not start with 00: write +250788123456, or 0788123456 for a Rwandan number.")
        )

    if plus_prefixed:
        return _from_international(digits)
    if digits.startswith(RWANDA_TRUNK_PREFIX):
        # The trunk prefix is Rwandan by definition; no international number
        # opens with a zero, so there is nothing else this can be.
        return RWANDA_COUNTRY_CODE + _rwanda_nsn(digits[1:])
    if len(digits) == RWANDA_NSN_LENGTH and RWANDA_NSN_REGEX.fullmatch(digits):
        return RWANDA_COUNTRY_CODE + digits
    if digits.startswith(RWANDA_COUNTRY_CODE) and len(digits) == _RWANDA_E164_LENGTH:
        # 250 + 9 digits can only be a Rwandan number: 250 is the country code
        # and no shorter code (2, 25) is assigned.
        return _from_international(digits)
    return _international(digits)


def _rwanda_nsn(nsn: str) -> str:
    if not RWANDA_NSN_REGEX.fullmatch(nsn):
        raise _invalid_phone(
            _(
                "A Rwandan number is nine digits starting with 7 for mobile or 2 for "
                "a landline, written as 0788123456 or +250788123456."
            )
        )
    return nsn


def _from_international(digits: str) -> str:
    if digits.startswith(RWANDA_COUNTRY_CODE):
        return RWANDA_COUNTRY_CODE + _rwanda_nsn(digits[len(RWANDA_COUNTRY_CODE) :])
    return _international(digits)


def _international(digits: str) -> str:
    if not _E164_REGEX.fullmatch(digits):
        raise _invalid_phone(_PHONE_HELP)
    if len(digits) == RWANDA_NSN_LENGTH and RWANDA_NSN_REGEX.fullmatch(digits):
        # Refused to keep the canonical space closed: stored bare, this value
        # would normalise to `250…` next time it was read, so it could be
        # written under one string and looked up under another. No country's
        # real E.164 number is nine digits long opening with 2 or 7, so
        # nothing legitimate is being turned away.
        raise _invalid_phone(
            _(
                "A nine-digit number is read as Rwandan: write a Rwandan number as "
                "0788123456, or another country's in full, as +254712345678."
            )
        )
    return digits

#: Deliberately narrower than Django's `UnicodeUsernameValidator`: `@` is
#: excluded so a username can never look like an email address, and an
#: all-digit username can never look like a phone number. Login accepts all
#: three identifiers, so an ambiguous one could route a password reset to the
#: wrong account.
username_validator = RegexValidator(
    regex=r"^[\w.+-]+\Z",
    message=_("Use letters, digits and . + - _ only. No @ or spaces."),
    code="invalid_username",
)


def validate_username_not_numeric(value: str) -> None:
    if value and value.replace(".", "").replace("+", "").replace("-", "").isdigit():
        raise ValidationError(
            _("A username cannot be only digits: that is how phone numbers are written."),
            code="numeric_username",
        )


currency_code_validator = RegexValidator(
    regex=r"^[A-Z]{3}$",
    message=_("Enter a three-letter ISO 4217 currency code, for example RWF."),
    code="invalid_currency",
)


# --------------------------------------------------------------------------
# Time zones
# --------------------------------------------------------------------------
#
# A period boundary is computed in the organization's zone, so this column
# decides every month-end total the business ever reads. It used to be
# validated against `zoneinfo.available_timezones()`, which is a *scan of the
# filesystem*, and that was wrong in four measured ways at once:
#
#   * `localtime` is in the set. It is a symlink to the host's
#     `/etc/localtime`, so one database served from two machines gives one
#     organization two different month-ends, and neither is reproducible.
#   * `Factory` is in the set. It is the tz database's "you forgot to
#     configure this" placeholder.
#   * `Etc/GMT+5` is in the set, and its `utcoffset` was MEASURED as
#     **−05:00** - POSIX inverts the sign of that whole namespace. A ten-hour
#     error, in a spelling a form accepted without complaint.
#   * The set is not the same in two places. MEASURED: **486** keys inside the
#     container against **498** read elsewhere, because the backward-
#     compatibility links are a packaging choice. So `Africa/Asmera` and
#     `US/Pacific` - real, widely used aliases - are *rejected* in the
#     container while nonsense is accepted. Validation that differs by host is
#     not validation.
#
# What replaces it is a literal, curated set. Adding a zone is a deliberate
# edit with a test that pins its offset, which is the correct amount of
# friction for a value that moves every total in every report.

#: The zones an organization may bound its reporting periods with: Rwanda, its
#: East African neighbours, and UTC. Deliberately small. Widen it by editing
#: this set and adding the zone to the pinned-offset table in
#: `tests/test_timezone_allowlist.py` - never by widening it back to a
#: filesystem scan.
#:
#: Every key here is a canonical IANA `Region/City` identifier (plus `UTC`,
#: which is the one zone with no city), so it exists in any tzdata build,
#: with or without the backward links.
REPORTING_TIMEZONES = frozenset(
    {
        "Africa/Kigali",  # Rwanda. The default.
        "Africa/Bujumbura",  # Burundi
        "Africa/Nairobi",  # Kenya
        "Africa/Kampala",  # Uganda
        "Africa/Dar_es_Salaam",  # Tanzania
        "Africa/Lubumbashi",  # DR Congo, east (+02)
        "Africa/Kinshasa",  # DR Congo, west (+01)
        "Africa/Juba",  # South Sudan
        "Africa/Khartoum",  # Sudan
        "Africa/Addis_Ababa",  # Ethiopia
        "Africa/Mogadishu",  # Somalia
        "UTC",
    }
)

#: Keys that must stay refused however wide the allowlist gets, including for
#: the per-user *display* timezone `docs/PRODUCT.md` leaves room for. Each one
#: resolves, so nothing upstream refuses it for us.
#:
#: `localtime` and `local` follow the host clock. `Factory` is the placeholder.
#: `posixrules` is a legacy DST template - MEASURED resolving to −04:00 while
#: absent from `available_timezones()`, so `ZoneInfo` accepts strictly more
#: than that scan lists.
UNSAFE_TIMEZONE_KEYS = frozenset({"localtime", "local", "Factory", "posixrules"})

#: Namespaces that must stay refused for the same reason.
#:
#: `Etc/` because of the inverted sign measured above. `SystemV/` is a legacy
#: compatibility namespace with hard-coded pre-1987 US DST rules.
#: `right/` and `posix/` are alternate *encodings* of every other zone -
#: `right/` counts leap seconds, so its arithmetic differs from every other
#: spelling of the same place.
UNSAFE_TIMEZONE_PREFIXES = ("Etc/", "SystemV/", "right/", "posix/")


def unsafe_timezone_reason(value) -> str | None:
    """Why `value` may never bound a period, or `None` if there is no reason.

    Separate from `validate_timezone` and phrased as a reason string so the
    same rule can guard the future display-timezone setting - which will want
    a wider allowlist but not a wider *safety* rule - and so
    `common.periods.resolve_timezone` can refuse a value that predates this
    allowlist without importing Django's form machinery.
    """
    if not isinstance(value, str) or not value:
        return "it is not a time zone name"
    if value in UNSAFE_TIMEZONE_KEYS:
        return "it follows the machine's own clock instead of naming a place"
    for prefix in UNSAFE_TIMEZONE_PREFIXES:
        if value.startswith(prefix):
            return f"the {prefix} names are not places and do not mean what they read as"
    return None


def timezone_exists(value: str) -> bool:
    """Whether tzdata can resolve `value` at all. Used by the allowlist test,
    not by the validator: membership in `REPORTING_TIMEZONES` is the rule."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False
    return True


def validate_timezone(value: str) -> None:
    """Accept only a zone this product reports in.

    Period boundaries are computed in the organization's zone, so a value that
    is merely *resolvable* is not good enough: it has to be a real place we
    have checked, or every total silently shifts.
    """
    reason = unsafe_timezone_reason(value)
    if reason is not None:
        raise ValidationError(
            _("%(value)s cannot be a reporting time zone: %(reason)s."),
            code="unsafe_timezone",
            params={"value": value, "reason": reason},
        )
    if value not in REPORTING_TIMEZONES:
        raise ValidationError(
            _(
                "%(value)s is not a time zone Raporo reports in. Choose one of: "
                "%(allowed)s."
            ),
            code="invalid_timezone",
            params={"value": value, "allowed": ", ".join(sorted(REPORTING_TIMEZONES))},
        )


# --------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------

#: Raster formats only. An SVG is a document: it can carry script, and served
#: from our own origin that is stored XSS.
ALLOWED_IMAGE_EXTENSIONS = ["png", "jpg", "jpeg", "webp"]
ALLOWED_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
MAX_IMAGE_BYTES = 2 * 1024 * 1024

image_extension_validator = FileExtensionValidator(
    allowed_extensions=ALLOWED_IMAGE_EXTENSIONS,
    message=_("Upload a PNG, JPEG or WebP image."),
    code="invalid_image_extension",
)

#: Leading bytes of the only three formats we accept. Checked before Pillow
#: opens the file so a crafted upload with a `.png` name never reaches the dozens
#: of other parsers Pillow registers (the GD/FITS/etc. decompression-bomb CVEs
#: lived in parsers we never want to touch). PNG carries an 8-byte signature;
#: JPEG starts SOI + a marker (`FF D8 FF`); WebP is a RIFF container tagged
#: `WEBP` at offset 8.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_IMAGE_MAGIC_HEADER_BYTES = 12


def _has_allowed_magic(head: bytes) -> bool:
    if head.startswith(_PNG_MAGIC) or head.startswith(_JPEG_MAGIC):
        return True
    return len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP"


def validate_image_size(file) -> None:
    size = getattr(file, "size", None)
    if size is None:
        return
    if size > MAX_IMAGE_BYTES:
        raise ValidationError(
            _("Keep the image under %(limit)s KB (this one is %(size)s KB)."),
            code="image_too_large",
            params={"limit": MAX_IMAGE_BYTES // 1024, "size": size // 1024},
        )


def validate_image_content(file) -> None:
    """Open the upload and confirm it really is one of the allowed formats.

    The extension is attacker-chosen, so it proves nothing: this decodes the
    bytes and rejects anything Pillow does not recognise as PNG/JPEG/WebP.
    """
    if not file:
        return
    try:
        position = file.tell()
    except (AttributeError, ValueError, OSError):
        position = None
    try:
        file.seek(0)
        head = file.read(_IMAGE_MAGIC_HEADER_BYTES)
        if not _has_allowed_magic(head if isinstance(head, bytes) else bytes(head)):
            # Reject on the magic bytes before Pillow parses anything: an
            # attacker-named `.png` that is really some other format never
            # reaches that format's parser.
            raise ValidationError(
                _("That file is not a readable image."),
                code="invalid_image",
            )
        file.seek(0)
        with Image.open(file) as image:
            image_format = (image.format or "").upper()
            image.verify()
    except FileNotFoundError:
        # Nothing to validate (a stored file that has gone missing); the upload
        # path is where this validator earns its keep.
        return
    except Image.DecompressionBombError as exc:
        # A header claiming an enormous pixel count: refuse it as bad input
        # rather than letting it become an unhandled 500.
        raise ValidationError(
            _("That image is too large to process."),
            code="invalid_image",
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ValidationError(
            _("That file is not a readable image."),
            code="invalid_image",
        ) from exc
    finally:
        if position is not None:
            try:
                file.seek(position)
            except (ValueError, OSError):
                pass
    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise ValidationError(
            _("Upload a PNG, JPEG or WebP image (this one is %(format)s)."),
            code="invalid_image_format",
            params={"format": image_format or "unknown"},
        )
