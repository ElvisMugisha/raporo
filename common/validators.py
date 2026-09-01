"""Reusable field validators.

User-facing messages are wrapped in `gettext_lazy`: they surface in forms.
"""

from functools import lru_cache
from zoneinfo import available_timezones

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, RegexValidator
from django.utils.translation import gettext_lazy as _
from PIL import Image, UnidentifiedImageError

#: Country code + subscriber number, digits only, no leading `+` and no zero
#: first digit (E.164 without the plus). Example: 250788123456.
PHONE_REGEX = r"^[1-9][0-9]{7,14}$"

phone_validator = RegexValidator(
    regex=PHONE_REGEX,
    message=_("Enter the phone number with its country code, digits only, without +."),
    code="invalid_phone",
)

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


@lru_cache(maxsize=1)
def _known_timezones() -> frozenset[str]:
    """Cached: `available_timezones()` walks the tz database on every call."""
    return frozenset(available_timezones())


def validate_timezone(value: str) -> None:
    """Reject anything that is not an IANA key.

    Period boundaries are computed in the organization's zone, so a typo here
    would silently shift every report.
    """
    if value not in _known_timezones():
        raise ValidationError(
            _("%(value)s is not a known time zone name."),
            code="invalid_timezone",
            params={"value": value},
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
