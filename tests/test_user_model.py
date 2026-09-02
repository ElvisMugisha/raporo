"""accounts.User: identity, phone format, language, password hashing."""

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.models import Language, User
from common.managers import HardDeleteForbidden

pytestmark = pytest.mark.django_db


def build_user(**overrides):
    """An unsaved, otherwise-valid user (password set so `full_clean` only
    complains about what a test is actually probing)."""
    fields = {
        "username": "someone",
        "email": "someone@example.rw",
        "phone": "250788123456",
    }
    fields.update(overrides)
    user = User(**fields)
    user.set_password("S3cure!passphrase")
    return user


def test_the_project_user_model_is_ours():
    assert get_user_model() is User
    assert settings.AUTH_USER_MODEL == "accounts.User"


def test_username_field_and_required_fields():
    assert User.USERNAME_FIELD == "username"
    assert User.EMAIL_FIELD == "email"
    # createsuperuser must ask for both: email is the password-reset channel.
    assert set(User.REQUIRED_FIELDS) == {"email", "phone"}


@pytest.mark.parametrize(
    "phone",
    [
        "250788",  # too short
        "2507881234567890",  # too long
        "25078812345a",  # letters
        "250+788123456",  # + in the middle
        "00250788123456",  # international dialling prefix
        "0912345678",  # no such Rwandan destination code
        "",
    ],
)
def test_phone_rejects_bad_formats(phone):
    with pytest.raises(ValidationError) as exc:
        build_user(phone=phone).full_clean()

    assert "phone" in exc.value.error_dict


@pytest.mark.parametrize("phone", ["250788123456", "12345678", "999999999999999"])
def test_phone_accepts_a_canonical_stored_number(phone):
    build_user(phone=phone).full_clean()  # must not raise


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("0788123456", "250788123456"),  # how a Rwandan writes it
        ("+250788123456", "250788123456"),
        ("788123456", "250788123456"),
        ("0788 123 456", "250788123456"),
        ("+254712345678", "254712345678"),  # another country, kept in full
    ],
)
def test_full_clean_canonicalises_the_phone_before_anything_compares_it(typed, stored):
    """`tests/test_phone_identity.py` owns the invariant this makes possible."""
    user = build_user(phone=typed)
    user.full_clean()

    assert user.phone == stored


def test_email_is_required():
    with pytest.raises(ValidationError) as exc:
        build_user(email="").full_clean()

    assert "email" in exc.value.error_dict


def test_email_must_be_unique_case_insensitively(actor):
    with pytest.raises(ValidationError):
        build_user(username="other", email=actor.email.upper(), phone="250788999999").full_clean()


def test_email_uniqueness_is_enforced_by_the_database_too(actor):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            User.objects.create(
                username="other",
                email=actor.email.upper(),
                phone="250788999999",
                password="x",
            )


def test_username_must_be_unique_case_insensitively(actor):
    with pytest.raises(ValidationError):
        build_user(
            username=actor.username.upper(), email="other@example.rw", phone="250788999999"
        ).full_clean()


def test_phone_must_be_unique(actor):
    with pytest.raises(ValidationError) as exc:
        build_user(username="other", email="other@example.rw", phone=actor.phone).full_clean()

    assert "phone" in exc.value.error_dict


def test_language_defaults_to_english(actor):
    assert actor.language == "en"


def test_language_choices_match_the_configured_languages():
    assert {code for code, _label in settings.LANGUAGES} == set(Language.values)


def test_language_rejects_an_unconfigured_code():
    with pytest.raises(ValidationError) as exc:
        build_user(language="sw").full_clean()

    assert "language" in exc.value.error_dict


def test_password_is_argon2(actor):
    assert actor.password.startswith("argon2")
    assert actor.check_password("S3cure!passphrase")


def test_create_user_validates_its_input():
    with pytest.raises(ValidationError):
        User.objects.create_user(
            username="a",
            email="a@example.rw",
            phone="0912345678",  # trunk prefix, but no such destination code
            password="S3cure!passphrase",
        )


@pytest.mark.parametrize("missing", ["username", "email", "phone"])
def test_create_user_requires_identity_fields(missing):
    kwargs = {
        "username": "a",
        "email": "a@example.rw",
        "phone": "250788123456",
        "password": "S3cure!passphrase",
    }
    kwargs[missing] = ""

    with pytest.raises(ValueError):
        User.objects.create_user(**kwargs)


def test_create_user_normalises_the_email_domain():
    user = User.objects.create_user(
        username="a",
        email="Eva@EXAMPLE.RW",
        phone="250788123456",
        password="S3cure!passphrase",
    )

    assert user.email == "Eva@example.rw"


def test_create_user_without_a_password_cannot_log_in():
    user = User.objects.create_user(
        username="a", email="a@example.rw", phone="250788123456"
    )

    assert not user.has_usable_password()


def test_create_superuser_is_staff_and_superuser():
    user = User.objects.create_superuser(
        username="root",
        email="root@example.rw",
        phone="250788123457",
        password="S3cure!passphrase",
    )

    assert user.is_staff and user.is_superuser and user.is_active


def test_create_superuser_refuses_to_be_downgraded():
    with pytest.raises(ValueError):
        User.objects.create_superuser(
            username="root",
            email="root@example.rw",
            phone="250788123457",
            password="S3cure!passphrase",
            is_staff=False,
        )


def test_new_users_are_active_and_not_staff(actor):
    assert actor.is_active is True
    assert actor.is_staff is False
    assert actor.date_joined is not None


# --------------------------------------------------------------------------
# D3 - the username namespace cannot overlap the other two identifiers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "username",
    [
        "victim@example.com",  # looks like an email
        "250788111111",  # looks like a phone number
        "250-788-111-111",
        "eva mugisha",  # spaces
        "eva@",
    ],
)
def test_username_cannot_impersonate_another_identifier(username):
    """Login resolves username OR email OR phone, so an ambiguous username
    could route a password reset to the wrong account."""
    with pytest.raises(ValidationError) as exc:
        build_user(username=username).full_clean()

    assert "username" in exc.value.error_dict


@pytest.mark.parametrize("username", ["eva", "eva.mugisha", "eva_m1", "eva-m", "eva+shop"])
def test_ordinary_usernames_are_accepted(username):
    build_user(username=username).full_clean()


def test_create_user_rejects_an_ambiguous_username():
    with pytest.raises(ValidationError):
        User.objects.create_user(
            username="250788111111",
            email="a@example.rw",
            phone="250788123456",
            password="S3cure!passphrase",
        )


# --------------------------------------------------------------------------
# B3 - users are deactivated, never deleted
# --------------------------------------------------------------------------


def test_a_user_cannot_be_hard_deleted(actor):
    with pytest.raises(HardDeleteForbidden):
        actor.delete()

    assert User.objects.filter(pk=actor.pk).exists()


def test_users_cannot_be_hard_deleted_in_bulk(actor):
    with pytest.raises(HardDeleteForbidden):
        User.objects.filter(pk=actor.pk).delete()
    with pytest.raises(HardDeleteForbidden):
        User.objects.all().delete()

    assert User.objects.count() == 1


def test_the_user_base_manager_cannot_hard_delete_either(actor):
    assert User._base_manager.name == "objects"

    with pytest.raises(HardDeleteForbidden):
        User._base_manager.filter(pk=actor.pk).delete()

    assert User.objects.count() == 1


def test_deactivation_is_the_supported_path(actor):
    actor.is_active = False
    actor.save(update_fields=["is_active"])
    actor.refresh_from_db()

    assert actor.is_active is False
    assert User.objects.filter(pk=actor.pk).exists()
