"""Shared fixtures for the foundation test-suite."""

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS, connections

from apps.orgs.models import Organization, Store
from tests.testapp.models import Category, Product, Sale


@pytest.fixture
def load_fixture(db):
    """`loaddata`, with the composite foreign keys re-armed afterwards.

    Load fixtures through this, not through `call_command("loaddata", ...)`.

    Postgres' `check_constraints()` - which `loaddata` runs at the end of every
    load - finishes with `SET CONSTRAINTS ALL DEFERRED`, and that lasts until
    the transaction ends. Every `db` test is one transaction, so from the first
    plain `loaddata` onwards the four `*_same_org_fk` keys are checked at commit
    time instead of statement time: a cross-organization write made after the
    load is *accepted*, whatever `pytest.raises` you wrapped it in fails, and
    the violation finally surfaces at teardown attributed to whichever test ran
    last. `SET CONSTRAINTS ALL IMMEDIATE` puts them back.

    Both halves are measured in `tests/test_fixture_loading.py`, including the
    one test that deliberately does *not* use this fixture, because its subject
    is the landmine itself.
    """

    def load(*fixtures, **kwargs):
        kwargs.setdefault("verbosity", 0)
        call_command("loaddata", *fixtures, **kwargs)
        with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    return load


@pytest.fixture
def actor(db):
    return get_user_model().objects.create_user(
        username="eva",
        email="eva@example.rw",
        phone="250788000001",
        password="S3cure!passphrase",
    )


@pytest.fixture
def other_actor(db):
    return get_user_model().objects.create_user(
        username="jean",
        email="jean@example.rw",
        phone="250788000002",
        password="S3cure!passphrase",
    )


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Eva Shop", slug="eva-shop")


@pytest.fixture
def other_org(db):
    return Organization.objects.create(name="Rival Shop", slug="rival-shop")


@pytest.fixture
def store(org):
    return Store.objects.create(org=org, name="Main")


@pytest.fixture
def other_store(org):
    """A second store inside the SAME org - the realistic leak vector."""
    return Store.objects.create(org=org, name="Kimironko")


@pytest.fixture
def foreign_store(other_org):
    """A store in a different org entirely."""
    return Store.objects.create(org=other_org, name="Main")


# --------------------------------------------------------------------------
# Slice-2-shaped data: an org-level parent with store-scoped children, and a
# store-scoped parent with store-scoped children.
# --------------------------------------------------------------------------


@pytest.fixture
def category(org):
    return Category.objects.create(org=org, name="Drinks")


@pytest.fixture
def foreign_category(other_org):
    return Category.objects.create(org=other_org, name="Drinks")


@pytest.fixture
def product(store, category):
    return Product.objects.create(store=store, category=category, name="my-secret-product")


@pytest.fixture
def foreign_product(foreign_store, foreign_category):
    return Product.objects.create(
        store=foreign_store, category=foreign_category, name="RIVAL-SECRET-PRODUCT"
    )


@pytest.fixture
def sale(store):
    return Sale.objects.create(store=store, reference="S-1")


@pytest.fixture
def foreign_sale(foreign_store):
    return Sale.objects.create(store=foreign_store, reference="S-1")
