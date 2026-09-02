"""`common.validators.normalize_phone`: one human phone, one stored string.

The old regex validated *shape* and not *identity*: `788123456` and
`250788123456` both passed it, so one real subscriber could occupy two rows
that `unique=True` was powerless to stop, while `0788123456` - the form every
Rwandan writes - was refused. These tests pin the canonicalisation that closes
that, including the closure property the invariant rests on: every value we
store normalises to itself, so a login identifier and a stored identifier
cannot disagree.
"""

import random
import re

import pytest
from django.core.exceptions import ValidationError

from common.validators import (
    PHONE_INPUT_MAX_LENGTH,
    PHONE_REGEX,
    RWANDA_COUNTRY_CODE,
    RWANDA_NSN_LENGTH,
    normalize_phone,
)

STORED_FORM = re.compile(PHONE_REGEX)


# --------------------------------------------------------------------------
# The decision table
# --------------------------------------------------------------------------

ACCEPTED = [
    # Rwandan, the way people actually write it (trunk prefix 0).
    ("0788123456", "250788123456"),
    ("+250788123456", "250788123456"),
    ("250788123456", "250788123456"),
    # Bare national number: assume Rwanda, the only country we can assume.
    ("788123456", "250788123456"),
    # Another country's full international number, kept as it is.
    ("+254712345678", "254712345678"),
    # Nine digits that cannot be Rwandan (no destination code opens with 9) is
    # read as an international number already in canonical form - which is what
    # keeps a stored value normalising to itself.
    ("912345678", "912345678"),
    ("+912345678", "912345678"),
    # Presentation separators are not part of a number.
    ("0788 123 456", "250788123456"),
    ("+250 788 123 456", "250788123456"),
    ("(0788) 123-456", "250788123456"),
    ("0788.123.456", "250788123456"),
    ("  0788123456  ", "250788123456"),
    # Rwandan fixed lines are in the plan too: NSN starts with 2 (RURA's own
    # switchboard, +250 252 584 562).
    ("0252584562", "250252584562"),
    ("+250252584562", "250252584562"),
    ("252584562", "250252584562"),
]


@pytest.mark.parametrize(("raw", "canonical"), ACCEPTED)
def test_accepted_input_normalises_to_the_canonical_form(raw, canonical):
    assert normalize_phone(raw) == canonical


@pytest.mark.parametrize(("raw", "canonical"), ACCEPTED)
def test_every_accepted_form_stores_the_shape_the_column_promises(raw, canonical):
    """The canonical form is what the field validator and the column allow."""
    stored = normalize_phone(raw)

    assert STORED_FORM.fullmatch(stored)
    assert len(stored) <= 15


@pytest.mark.parametrize(
    "raw",
    [
        "25078812345a",  # letters
        "250788123456x",
        "+250-78A-123456",
        "",  # nothing at all
        "   ",
        "\t\n",
        "250788",  # absurdly short
        "7881234",  # 7 digits: not a Rwandan national number, not international
        "07881234",  # trunk prefix + 7 digits
        "07881234567",  # trunk prefix + 10 digits
        "2507881234567890",  # absurdly long
        "9999999999999999",
        "250+788123456",  # + in the middle
        "78812+3456",
        "++250788123456",
        "00250788123456",  # international dialling prefix
        "00254712345678",
        "+00250788123456",
        "+0788123456",  # no international number starts with a zero
        "0912345678",  # trunk prefix, but no such Rwandan destination code
        "0088123456",
        "+250912345678",  # country code 250 with a number outside the plan
        "+250788123",  # country code 250 with too few digits
        "+2507881234567",  # country code 250 with too many digits
        "+271234567",  # 9 digits would be re-read as Rwandan: refuse it
        "+7",
        "+",
        "0",
        "-",
        "()",
        "٢٥٠٧٨٨١٢٣٤٥٦",  # Arabic-Indic digits: str.isdigit() says yes, we do not
        "２５０７８８１２３４５６",  # full-width digits
        "250788123456\n250788999999",  # two numbers in one field
        "250788123456;DROP TABLE",
    ],
)
def test_refused_input_raises_a_validation_error(raw):
    with pytest.raises(ValidationError) as exc:
        normalize_phone(raw)

    assert exc.value.code == "invalid_phone"


def test_input_longer_than_the_accepted_width_is_refused_without_scanning_it():
    with pytest.raises(ValidationError) as exc:
        normalize_phone("0" * (PHONE_INPUT_MAX_LENGTH + 1))

    assert exc.value.code == "invalid_phone"


@pytest.mark.parametrize("value", [None, 250788123456, 250788123456.0, b"250788123456", ["2"]])
def test_non_string_input_is_refused_rather_than_coerced(value):
    """An int has already lost the leading zero that decides the country."""
    with pytest.raises(ValidationError) as exc:
        normalize_phone(value)

    assert exc.value.code == "invalid_phone"


# --------------------------------------------------------------------------
# Closure: a stored value normalises to itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "canonical"), ACCEPTED)
def test_normalising_a_stored_value_returns_it_unchanged(raw, canonical):
    assert normalize_phone(canonical) == canonical


@pytest.mark.parametrize(
    "raw",
    [row[0] for row in ACCEPTED] + ["254712345678", "12345678", "441234567890"],
)
def test_normalisation_is_idempotent(raw):
    once = normalize_phone(raw)

    assert normalize_phone(once) == once


def test_a_foreign_number_typed_without_its_plus_still_normalises_to_itself():
    """Otherwise a stored foreign number could not be used to log in."""
    assert normalize_phone("254712345678") == "254712345678"


def test_the_canonical_space_is_closed_under_normalisation():
    """Fuzzed: whatever comes out must go back in unchanged.

    This is the property the uniqueness invariant depends on. If any accepted
    input produced a value that normalised to something else, that value could
    be stored under one string and looked up under another - the original bug,
    one layer down.
    """
    rng = random.Random(20260902)
    alphabet = "0123456789+ -.()"
    checked = 0
    for _ in range(4000):
        candidate = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 18)))
        try:
            once = normalize_phone(candidate)
        except ValidationError:
            continue
        checked += 1
        assert STORED_FORM.fullmatch(once), f"{candidate!r} produced {once!r}"
        assert normalize_phone(once) == once, f"{candidate!r} -> {once!r} is not a fixed point"

    # Premise: the fuzz actually reached the accepting branches.
    assert checked > 50, f"only {checked} candidates were accepted; the fuzz proves nothing"


def test_hostile_input_only_ever_raises_validation_error():
    """No TypeError, IndexError or ValueError may escape to become a 500."""
    rng = random.Random(902)
    alphabet = "0123456789+ -.()abZ٠０\n\t;'\"\\%_"
    for _ in range(4000):
        candidate = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        try:
            normalize_phone(candidate)
        except ValidationError:
            pass


# --------------------------------------------------------------------------
# The shape task 5's auth backend needs
# --------------------------------------------------------------------------


def test_the_numbering_plan_constants_are_the_ones_rura_publishes():
    """Rwanda: country code 250, national significant number 9 digits."""
    assert RWANDA_COUNTRY_CODE == "250"
    assert RWANDA_NSN_LENGTH == 9
    assert len(normalize_phone("0788123456")) == len(RWANDA_COUNTRY_CODE) + RWANDA_NSN_LENGTH


def test_an_identifier_resolver_can_ask_forgiveness():
    """The login shape: try to read the identifier as a phone, fall through.

    Task 5 resolves username OR email OR phone from one box, so it must be
    able to reject a non-phone identifier without an exception escaping.
    """

    def as_phone(identifier):
        try:
            return normalize_phone(identifier)
        except ValidationError:
            return None

    assert as_phone("0788123456") == "250788123456"
    assert as_phone("eva.mugisha") is None
    assert as_phone("eva@example.rw") is None
    assert as_phone("") is None
    assert as_phone(None) is None
