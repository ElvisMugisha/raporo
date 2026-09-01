"""Shared fixtures for the foundation test-suite."""

import pytest
from django.contrib.auth import get_user_model

from apps.orgs.models import Organization, Store
from tests.testapp.models import Category, Product, Sale


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
