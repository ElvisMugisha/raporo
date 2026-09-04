"""The generated denial matrix — the release gate for ADR 0011.

Elvis's ruling: *"A user may access more than one store, and only if they were
given access to both of them. But the owner of the org can access any store
under their org."* If `permitted_stores()` is wrong, every store in one
organization becomes readable and writable by every member of it and nothing
below Python stops it. The only detection is this file.

**Read the fixture's fifth actor first.** `a_decoy` holds a role literally
*named* `"Owner"` with only `sale.record`, while org A's real owner role is
named `Nyiricyubahiro`. It is the only row that proves the check is not
name-based, and a matrix that named the powerful role "Owner" would pass under
a name-based implementation. The mutation evidence for it is in this module's
docstring for `test_the_matrix`: flipping the resolver to
`role.name == "Owner"` sends `a_decoy → A2` from 404 to 200 **and**
`a_owner → A2` from 200 to 404. Both, or the matrix proves nothing.

Deliberately **not** in the fixture: a Seller. Manager and Seller differ on the
*permission* axis, which `require_permission()` owns and which has its own
tests in `tests/test_orgs_access.py`. Adding one here would dilute a matrix
whose subject is the store axis - which is also why every endpoint below is
gated on `sale.record`, a code **every** actor in the fixture holds. The only
variable is reach.

Two things this file carries that belong elsewhere once their owners land:

* `StoreDenialMiddleware` is a stand-in for the `process_exception` hook ADR
  0011 puts in `common/middleware.py` (another track owns that file). The
  translation table is the contract; the middleware here executes it.
* The URLconf below is a harness, not the product. There are no store views
  yet; when they exist the parametrisation should be generated over the router
  so a new endpoint is covered the day it is written. What is generated *today*
  is the model axis: `ROW_FACTORIES` is asserted to cover every registered
  store-scoped model, so a model added in slice 2 turns this file red until
  someone gives it a factory.
"""

import uuid

import pytest
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.urls import path

from apps.orgs.exceptions import StoreNotPermitted
from apps.orgs.models import Membership, Organization, Role, Store, StoreAccess
from apps.orgs.permissions import PERMISSIONS, SALE_RECORD, STORE_ACCESS_ALL
from apps.orgs.services import membership_for, require_store_permission
from common.models import StoreScopedModel
from tests.testapp.models import Category, Product, Sale, SaleLine, ScopedThing

pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.test_tenancy_matrix")]


# --------------------------------------------------------------------------
# The harness: two endpoints, one translator
# --------------------------------------------------------------------------


def _model(label):
    from django.apps import apps as django_apps

    return django_apps.get_model(label)


def read_row(request, store_id, label):
    """A store-addressing read, shaped like every detail page and fragment."""
    if not request.user.is_authenticated:
        # Harness choice. Production may redirect an HTML page to the login
        # screen instead; what this pins is that an anonymous request is
        # neither 200 nor 403, and that no resolver runs before authentication.
        return HttpResponse(status=401)
    membership = membership_for(request.user)
    store = require_store_permission(membership, store_id, SALE_RECORD)
    model = _model(label)
    return JsonResponse({"rows": model.objects.for_store(store).count()})


def write_row(request, store_id):
    """A store-addressing write. The gate runs before the write is built."""
    if not request.user.is_authenticated:
        return HttpResponse(status=401)
    membership = membership_for(request.user)
    store = require_store_permission(membership, store_id, SALE_RECORD)
    Sale.objects.create(store=store, reference="written")
    return HttpResponse(status=201)


class StoreDenialMiddleware:
    """`StoreNotPermitted` -> 404, byte-identical to a row that never existed.

    Never 403: a 403 confirms the row exists, which turns the override's
    complement into an existence oracle across sibling stores. This is the one
    place the translation happens, and it is why the exception must not
    subclass `PermissionDenied` - Django's own handler would render 403 before
    anything here ran.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, StoreNotPermitted):
            return HttpResponseNotFound("Not found.")
        return None


urlpatterns = [
    # The write route is declared first: Django matches in order, and
    # `<str:label>` would otherwise swallow the literal `write`.
    path("s/<str:store_id>/write/", write_row, name="matrix-write"),
    path("s/<str:store_id>/<str:label>/", read_row, name="matrix-read"),
]


@pytest.fixture(autouse=True)
def _translator(settings):
    settings.MIDDLEWARE = [
        *settings.MIDDLEWARE,
        "tests.test_tenancy_matrix.StoreDenialMiddleware",
    ]


# --------------------------------------------------------------------------
# The canonical fixture (tenancy design §I.7)
# --------------------------------------------------------------------------


def _category(store):
    return Category.objects.get_or_create(org_id=store.org_id, name="Drinks")[0]


#: One factory per registered store-scoped model, **in dependency order**, each
#: producing exactly one row per store. `made` carries the rows already built
#: for this store, so `SaleLine` reuses the matrix's own `Sale` and `Product`
#: instead of creating shadow rows - which would make "this store holds exactly
#: one row of this model" false and quietly weaken every 200 below.
#:
#: The dict is the generator's input and
#: `test_every_store_scoped_model_is_in_the_matrix` asserts it is complete, so
#: a model added in slice 2 makes this file red rather than quietly skipping
#: the new table.
ROW_FACTORIES = {
    "testapp.ScopedThing": lambda store, made: ScopedThing.objects.create(
        store=store, name="thing"
    ),
    "testapp.ScopedThingOwnMeta": lambda store, made: _model(
        "testapp.ScopedThingOwnMeta"
    ).objects.create(store=store, name="thing"),
    "testapp.Product": lambda store, made: Product.objects.create(
        store=store, category=_category(store), name="Fanta"
    ),
    "testapp.Sale": lambda store, made: Sale.objects.create(store=store, reference="S"),
    "testapp.SaleLine": lambda store, made: SaleLine.objects.create(
        store=store, sale=made["testapp.Sale"], product=made["testapp.Product"]
    ),
}


def _rows_for(store):
    made = {}
    for label, factory in ROW_FACTORIES.items():
        made[label] = factory(store, made)
    return made


def make_user(suffix):
    """No password, deliberately.

    `client.force_login()` does not need one, and Argon2 - correctly the only
    hasher configured - costs about a quarter of a second per call. Four actors
    across every parametrised row of this matrix made that the single most
    expensive thing in the suite. The subject here is authorization; the
    authentication path has its own tests.
    """
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        username=f"m{suffix}",
        email=f"m{suffix}@example.rw",
        phone=f"2507880002{suffix:02d}",
    )


class Matrix:
    """Two organizations, three stores, five actors."""

    def __init__(self):
        self.org_a = Organization.objects.create(name="Org A", slug="org-a")
        self.org_b = Organization.objects.create(name="Org B", slug="org-b")
        self.stores = {
            "A1": Store.objects.create(org=self.org_a, name="A1"),
            "A2": Store.objects.create(org=self.org_a, name="A2"),
            "B1": Store.objects.create(org=self.org_b, name="B1"),
        }

        # The real owner role of org A is NOT called "Owner". The name is
        # arbitrary; the power is the code.
        real_owner = Role.objects.create(
            org=self.org_a, name="Nyiricyubahiro", permissions=sorted(PERMISSIONS)
        )
        manager = Role.objects.create(
            org=self.org_a,
            name="Manager",
            permissions=sorted(PERMISSIONS - {STORE_ACCESS_ALL}),
        )
        # The decoy: named "Owner", holds one code, reaches one store.
        decoy = Role.objects.create(org=self.org_a, name="Owner", permissions=[SALE_RECORD])
        owner_b = Role.objects.create(
            org=self.org_b, name="Owner", permissions=sorted(PERMISSIONS)
        )

        self.actors = {}
        self.actors["a_owner"] = self._member(1, self.org_a, real_owner, stores=[])
        self.actors["a1_manager"] = self._member(2, self.org_a, manager, stores=["A1"])
        self.actors["a_decoy"] = self._member(3, self.org_a, decoy, stores=["A1"])
        self.actors["b_owner"] = self._member(4, self.org_b, owner_b, stores=[])
        self.actors["anonymous"] = None

        self.rows = {name: _rows_for(store) for name, store in self.stores.items()}

    def _member(self, suffix, org, role, *, stores):
        membership = Membership.objects.create(user=make_user(suffix), org=org, role=role)
        for name in stores:
            StoreAccess.objects.create(membership=membership, store=self.stores[name])
        return membership

    def login(self, client, actor):
        if self.actors[actor] is not None:
            client.force_login(self.actors[actor].user)


@pytest.fixture
def matrix(db):
    return Matrix()


# --------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------

#: (actor, target store, expected status, which layer refuses)
MATRIX = [
    ("a_owner", "A1", 200, "resolver: access_all includes A1"),
    ("a_owner", "A2", 200, "resolver: access_all includes A2 - THIS ROW IS THE RULING"),
    ("a_owner", "B1", 404, "resolver: B1 is not in org A"),
    ("a1_manager", "A1", 200, "resolver: a live StoreAccess row"),
    ("a1_manager", "A2", 404, "require_store -> StoreNotPermitted"),
    ("a_decoy", "A1", 200, "resolver: a live StoreAccess row"),
    ("a_decoy", "A2", 404, 'the role is NAMED "Owner" and holds no code'),
    ("b_owner", "B1", 200, "resolver: access_all includes B1"),
    ("b_owner", "A1", 404, "resolver: A1 is not in org B (and, later, RLS)"),
    ("b_owner", "A2", 404, "resolver: A2 is not in org B (and, later, RLS)"),
    ("anonymous", "A1", 401, "authentication, before any resolver runs"),
    ("anonymous", "A2", 401, "authentication, before any resolver runs"),
    ("anonymous", "B1", 401, "authentication, before any resolver runs"),
]


@pytest.mark.parametrize(
    ("actor", "target", "expected", "why"),
    MATRIX,
    ids=[f"{a}->{t}={e}" for a, t, e, _ in MATRIX],
)
@pytest.mark.parametrize("label", sorted(ROW_FACTORIES), ids=sorted(ROW_FACTORIES))
def test_the_matrix(client, matrix, actor, target, expected, why, label):
    """Every denial is 404. Never 403, and never a 200 with an empty list.

    Mutation evidence, recorded because a matrix nobody has broken proves
    nothing (all rows, every model):

    * delete the `access_all` branch from `permitted_stores()`:
      `a_owner -> A2` goes **200 -> 404**.
    * replace it with `membership.role.name == "Owner"`:
      `a_decoy -> A2` goes **404 -> 200** and `a_owner -> A2` goes
      **200 -> 404** in the same run. That pair is the measurement that a
      name-based check is a vulnerability rather than a style preference.
    """
    matrix.login(client, actor)

    response = client.get(f"/s/{matrix.stores[target].public_id}/{label}/")

    assert response.status_code == expected, why
    assert response.status_code != 403, "a 403 confirms the row exists"
    if expected == 200:
        assert response.json()["rows"] == 1


def test_a_denied_write_is_refused_before_anything_is_written(client, matrix):
    a2 = matrix.stores["A2"]
    before = Sale.all_objects.filter(store=a2).count()
    matrix.login(client, "a1_manager")

    response = client.post(f"/s/{a2.public_id}/write/")

    assert response.status_code == 404
    assert Sale.all_objects.filter(store=a2).count() == before


def test_the_owner_may_write_to_the_sibling_store(client, matrix):
    """The positive half. Without it, the row above is satisfied by a service
    that refuses everyone."""
    a2 = matrix.stores["A2"]
    matrix.login(client, "a_owner")

    response = client.post(f"/s/{a2.public_id}/write/")

    assert response.status_code == 201
    assert Sale.all_objects.filter(store=a2, reference="written").count() == 1


def test_a_denial_is_byte_identical_to_a_row_that_never_existed(client, matrix):
    """Otherwise the *shape* of the response is the oracle the status code is
    not."""
    matrix.login(client, "a1_manager")

    denied = client.get(f"/s/{matrix.stores['A2'].public_id}/testapp.Sale/")
    absent = client.get(f"/s/{uuid.uuid7()}/testapp.Sale/")

    assert denied.status_code == absent.status_code == 404
    assert denied.content == absent.content
    assert denied["Content-Type"] == absent["Content-Type"]


def test_without_the_translator_a_denial_is_never_a_403(client, matrix, settings):
    """The property the translator must not be trusted to provide.

    Measured, and it is why the structural assertion in
    `tests/test_orgs_access.py` matters more than it looks: a
    `process_exception` hook that names `StoreNotPermitted` explicitly returns
    404 **even if the exception subclasses `PermissionDenied`** - it runs before
    Django's own conversion - so the HTTP matrix cannot see that mistake. What
    it can see is this: with the translator removed, Django's default handling
    of the raw exception is a 500. Nothing in the stack can produce a 403 from
    a store denial by accident.
    """
    settings.MIDDLEWARE = [
        m for m in settings.MIDDLEWARE if not m.endswith("StoreDenialMiddleware")
    ]
    matrix.login(client, "a1_manager")

    with pytest.raises(StoreNotPermitted):
        client.get(f"/s/{matrix.stores['A2'].public_id}/testapp.Sale/")


@pytest.mark.parametrize(
    "junk", ["", "nope", "00000000-0000-0000-0000-000000000000", "1%20OR%201=1"]
)
def test_a_malformed_store_identifier_is_the_same_404(client, matrix, junk):
    matrix.login(client, "a_owner")

    response = client.get(f"/s/{junk}/testapp.Sale/")

    assert response.status_code == 404


def test_every_store_scoped_model_is_in_the_matrix():
    """The generation guarantee: a model added in slice 2 is covered the day it
    is written, because this test goes red until it has a factory."""
    from django.apps import apps as django_apps

    registered = {
        model._meta.label
        for model in django_apps.get_models()
        if issubclass(model, StoreScopedModel) and not model._meta.abstract
    }

    assert registered == set(ROW_FACTORIES), (
        f"missing factories for {sorted(registered - set(ROW_FACTORIES))}; "
        f"unknown models in ROW_FACTORIES: {sorted(set(ROW_FACTORIES) - registered)}"
    )


def test_the_decoy_role_really_is_called_owner(matrix):
    """If this ever stops being true the matrix stops testing what it claims:
    the name-based mutation would pass."""
    decoy = matrix.actors["a_decoy"].role
    real = matrix.actors["a_owner"].role

    assert decoy.name == "Owner"
    assert not decoy.has(STORE_ACCESS_ALL)
    assert real.name != "Owner"
    assert real.has(STORE_ACCESS_ALL)
