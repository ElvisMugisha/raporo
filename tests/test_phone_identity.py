"""The invariant: one human phone number cannot occupy two user rows.

`unique=True` alone never guaranteed that. It compares strings, so
`788123456`, `0788123456` and `250788123456` - one subscriber, one SIM - were
three different strings and three allowed rows. The guarantee is
normalisation *before* uniqueness is evaluated, on every path that can write
the column, so these tests probe the paths rather than the function:
`create_user()`, `objects.create()`, `bulk_create()`, `save()`,
`QuerySet.update()`, `full_clean()` and `loaddata`.
"""

import pytest
from django.core.exceptions import ValidationError
from django.core.serializers import deserialize
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from common.validators import PHONE_INPUT_MAX_LENGTH

pytestmark = pytest.mark.django_db

CANONICAL = "250788123456"
#: Every way the same subscriber's number reaches us.
SAME_NUMBER = ["0788123456", "+250788123456", "250788123456", "788123456", "0788 123 456"]


def make_user(phone, *, username="someone", email=None):
    return User.objects.create_user(
        username=username,
        email=email or f"{username}@example.rw",
        phone=phone,
        password="S3cure!passphrase",
    )


# --------------------------------------------------------------------------
# Normalisation happens before uniqueness is evaluated
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", SAME_NUMBER)
def test_create_user_stores_the_canonical_form(raw):
    user = make_user(raw)

    assert user.phone == CANONICAL
    user.refresh_from_db()
    assert user.phone == CANONICAL


@pytest.mark.parametrize("second", SAME_NUMBER)
def test_the_same_number_in_another_format_cannot_be_registered_twice(second):
    """The invariant, stated as the defect it closes."""
    make_user("0788123456", username="first")

    with pytest.raises(ValidationError) as exc:
        make_user(second, username="second")

    assert "phone" in exc.value.error_dict


@pytest.mark.parametrize("second", SAME_NUMBER)
def test_the_database_refuses_the_duplicate_too(second):
    """`create()` skips `full_clean()`, so the column must still be the arbiter."""
    make_user("0788123456", username="first")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            User.objects.create(
                username="second", email="second@example.rw", phone=second, password="x"
            )


def test_full_clean_reports_a_duplicate_written_in_another_format():
    make_user("0788123456", username="first")
    candidate = User(username="second", email="second@example.rw", phone="+250788123456")
    candidate.set_password("S3cure!passphrase")

    with pytest.raises(ValidationError) as exc:
        candidate.full_clean()

    assert "phone" in exc.value.error_dict


# --------------------------------------------------------------------------
# Every write path, named
# --------------------------------------------------------------------------


def test_objects_create_normalises():
    user = User.objects.create(
        username="eva", email="eva@example.rw", phone="0788123456", password="x"
    )

    assert user.phone == CANONICAL  # the in-memory instance, not only the row
    assert User.objects.get(pk=user.pk).phone == CANONICAL


def test_bulk_create_normalises():
    User.objects.bulk_create(
        [
            User(username="a", email="a@example.rw", phone="0788123456", password="x"),
            User(username="b", email="b@example.rw", phone="+250788999999", password="x"),
        ]
    )

    assert set(User.objects.values_list("phone", flat=True)) == {CANONICAL, "250788999999"}


def test_bulk_create_cannot_smuggle_in_a_duplicate_in_another_format():
    make_user("0788123456", username="first")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            User.objects.bulk_create(
                [User(username="b", email="b@example.rw", phone="788123456", password="x")]
            )


def test_queryset_update_normalises(actor):
    User.objects.filter(pk=actor.pk).update(phone="0788123456")
    actor.refresh_from_db()

    assert actor.phone == CANONICAL


def test_queryset_update_cannot_bypass_uniqueness_with_another_format(actor):
    other = make_user("0788123456", username="other")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            User.objects.filter(pk=actor.pk).update(phone="788123456")

    other.refresh_from_db()
    assert other.phone == CANONICAL


def test_instance_save_normalises(actor):
    actor.phone = "+250788777777"
    actor.save(update_fields=["phone"])

    assert actor.phone == "250788777777"
    assert User.objects.get(pk=actor.pk).phone == "250788777777"


def test_a_save_that_does_not_write_the_phone_is_not_held_up_by_it(actor):
    """`update_fields` without `phone` must not validate a column it will not write."""
    actor.phone = "not a phone at all"
    actor.save(update_fields=["language"])

    assert User.objects.get(pk=actor.pk).phone == "250788000001"


@pytest.mark.parametrize("raw", ["not a phone", "", "0912345678", "00250788123456"])
def test_a_write_of_an_uncanonicalisable_number_is_refused(raw):
    with pytest.raises(ValidationError):
        User.objects.create(username="x", email="x@example.rw", phone=raw, password="x")

    assert not User.objects.filter(username="x").exists()


@pytest.mark.parametrize("raw", ["not a phone", "0912345678", ""])
def test_a_queryset_update_of_an_uncanonicalisable_number_is_refused(actor, raw):
    """`update()` refuses it at the SQL layer, inside its own transaction.

    The `atomic()` here is not decoration: `QuerySet.update()` runs under
    `mark_for_rollback_on_error`, so the refusal marks the surrounding
    transaction. A caller who wants to carry on afterwards has to give the
    update a savepoint of its own - which is why `save()` canonicalises
    *before* it opens one.
    """
    with pytest.raises(ValidationError):
        with transaction.atomic():
            User.objects.filter(pk=actor.pk).update(phone=raw)

    actor.refresh_from_db()
    assert actor.phone == "250788000001"


def test_a_refused_save_does_not_poison_the_callers_transaction(actor):
    """A service that catches the refusal must still be able to query."""
    actor.phone = "0912345678"

    with pytest.raises(ValidationError):
        actor.save(update_fields=["phone"])

    assert User.objects.filter(pk=actor.pk).exists()  # the connection is usable


def test_bulk_update_normalises(actor, other_actor):
    actor.phone = "0788111111"
    other_actor.phone = "+250788222222"

    User.objects.bulk_update([actor, other_actor], ["phone"])

    assert User.objects.get(pk=actor.pk).phone == "250788111111"
    assert User.objects.get(pk=other_actor.pk).phone == "250788222222"


def test_an_in_lookup_normalises_every_value_it_is_given(actor):
    """Task 5 may resolve an identifier with `__in`; it must match too."""
    found = User.objects.filter(phone__in=["0788000001", "788999999"])

    assert [user.pk for user in found] == [actor.pk]


def test_the_form_field_is_wide_enough_for_what_people_type():
    """`+250 788 123 456` is 16 characters; the column is 15."""
    field = User._meta.get_field("phone").formfield()

    assert field.max_length == PHONE_INPUT_MAX_LENGTH
    assert field.max_length > User._meta.get_field("phone").max_length


def test_loaddata_normalises_what_a_fixture_carries():
    payload = """[{"model": "accounts.user", "pk": 4242, "fields": {
        "username": "fixture", "email": "fixture@example.rw", "phone": "0788456123",
        "password": "!unusable", "language": "en", "is_active": true, "is_staff": false,
        "is_superuser": false, "date_joined": "2026-09-01T17:38:35.240Z"
    }}]"""

    for obj in deserialize("json", payload):
        obj.save()

    assert User.objects.get(pk=4242).phone == "250788456123"


# --------------------------------------------------------------------------
# The login side (task 5 builds the backend; the lookup already normalises)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("typed", SAME_NUMBER)
def test_a_user_is_found_by_every_format_of_their_number(typed):
    user = make_user("0788123456")

    assert User.objects.get(phone=typed).pk == user.pk


def test_a_lookup_for_an_impossible_number_is_refused_not_silently_empty(actor):
    with pytest.raises(ValidationError):
        User.objects.filter(phone="0912345678").exists()


# --------------------------------------------------------------------------
# Nullable: the erasure path (privacy ruling P-4)
# --------------------------------------------------------------------------


def test_the_phone_column_is_nullable_so_erasure_need_not_invent_a_number():
    assert User._meta.get_field("phone").null is True


def test_a_phone_is_still_required_of_every_live_account():
    """Nullable at the database level, mandatory everywhere an account is made."""
    field = User._meta.get_field("phone")
    assert field.blank is False

    candidate = User(username="eva", email="eva@example.rw")
    candidate.set_password("S3cure!passphrase")
    with pytest.raises(ValidationError) as exc:
        candidate.full_clean()
    assert exc.value.error_dict["phone"][0].code == "blank"

    with pytest.raises(ValueError):
        make_user("")


def test_erasure_can_null_the_column_for_more_than_one_user(actor, other_actor):
    """Multiple NULLs coexist under a unique index on PostgreSQL."""
    User.objects.filter(pk__in=[actor.pk, other_actor.pk]).update(phone=None)

    nulled = User.objects.filter(phone__isnull=True).order_by("pk")

    assert list(nulled.values_list("pk", flat=True)) == sorted([actor.pk, other_actor.pk])


def test_erasure_by_instance_save_also_clears_the_column(actor):
    actor.phone = None
    actor.save(update_fields=["phone"])
    actor.refresh_from_db()

    assert actor.phone is None
