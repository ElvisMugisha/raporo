"""System checks that keep invariant #1 structurally true.

The store-scope guard in `common/managers.py` protects the queries it is asked
to run. These checks protect the *shape* of the models, which is where the
leaks the guard cannot see come from:

- a reverse accessor (`category.products`, `sale.lines`) hands out rows with no
  store filter and no soft-delete filter (E004). Hiding the accessor with
  `related_name="+"` is what E004 demands; `common.managers.GuardedQuery` then
  refuses the literal `+` query name too, so the relation is not reachable by
  a hand-built lookup key either;
- a foreign key into a store-scoped model from something that is not
  store-scoped has no store to be checked against (E006);
- a store-scoped model that loses, weakens or re-spells its denormalised `org`
  pointer takes the composite foreign key with it, and the database stops being
  what keeps a row's organization and its store in step (E007);
- a unique constraint that names no tenant column turns `full_clean()` into a
  cross-tenant existence oracle - "this code already exists" for a row the
  caller cannot see (E005);
- a model that declares any manager of its own silently takes over
  `_default_manager`, which Django uses for unique validation, forms and the
  admin (E001/E002).

Every one of these is a startup error, not a convention.
"""

from django.apps import apps as global_apps
from django.conf import settings
from django.core.checks import Error, Tags, register
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Model, Q, UniqueConstraint, UUIDField

from common.managers import STORE_FIELD, STORE_LABEL, StoreScopedManager
from common.models import (
    IDENTITY_COLUMNS,
    ORG_COLUMNS,
    ORG_FIELD,
    ORG_LABEL,
    PUBLIC_ID_FIELD,
    STORE_COLUMNS,
    TENANT_COLUMNS,
    StoreScopedModel,
)

DEFAULT_MANAGER_NAME = "all_objects"

#: The spelling `org` replaced, and which must not come back. Four SHA-256-pinned
#: `RunSQL` statements already contain the literal `REFERENCES orgs_store (id,
#: org_id)`, and the stability contract forbids editing shipped pinned text - so
#: a table spelling it `organization_id` would produce composite keys whose two
#: sides name the same concept differently, on every future review.
REJECTED_ORG_SPELLING = "organization"

#: Names for the primary key. A unique constraint that includes the primary key
#: is logically implied by the primary key, so it enforces nothing - see
#: `_check_fk_target`.
PK_COLUMNS = frozenset({"id", "pk"})

#: A unique constraint enforced across an organization's stores rather than
#: within one store has to say so in its own name. Not inferred from its
#: shape: `UniqueConstraint(fields=["org", "name"])` is what a developer types
#: by habit when they meant per store, and inferring intent turns that typo
#: into a constraint that rejects a legitimate row in the second shop, in
#: production, months later. The suffix is one extra statement of the same
#: intent, it lives on the database object, and it is what an operator reads
#: out of an `IntegrityError`.
PER_ORG_SUFFIX = "_per_org"

#: The two names a composite-FK target may carry, keyed by the tenant column it
#: pairs with the primary key.
FK_TARGET_SUFFIXES = {"org": "_id_org_uniq", "store": "_id_store_uniq"}


def _is_concrete_scoped_model(model) -> bool:
    return (
        isinstance(model, type)
        and issubclass(model, StoreScopedModel)
        and not model._meta.abstract
    )


def _expression_names(expression) -> set[str]:
    """Column names an expression tree refers to (F, Lower(F), ...)."""
    names = set()
    name = getattr(expression, "name", None)
    if isinstance(name, str):
        names.add(name)
    sources = getattr(expression, "get_source_expressions", None)
    if callable(sources):
        for source in sources():
            if source is not None:
                names |= _expression_names(source)
    return names


def _requires_live_rows(condition) -> bool:
    """True when `condition` insists, at AND level, on `deleted_at IS NULL`."""
    if condition is None:
        return False
    if getattr(condition, "negated", False):
        return False
    children = getattr(condition, "children", [])
    connector = getattr(condition, "connector", Q.AND)
    if connector != Q.AND and len(children) > 1:
        return False
    for child in children:
        if isinstance(child, tuple) and child == ("deleted_at__isnull", True):
            return True
        if isinstance(child, Q) and _requires_live_rows(child):
            return True
    return False


def _check_managers(model, label) -> list[Error]:
    errors = []
    if not isinstance(model.objects, StoreScopedManager):
        errors.append(
            Error(
                f"{label}.objects is not a StoreScopedManager, so unscoped queries "
                f"would run silently.",
                hint="Do not override `objects` on a store-scoped model.",
                obj=model,
                id="common.E001",
            )
        )
    if model._default_manager.name != DEFAULT_MANAGER_NAME:
        errors.append(
            Error(
                f"{label}._default_manager is {model._default_manager.name!r}, not "
                f"{DEFAULT_MANAGER_NAME!r}. Django uses the default manager for unique "
                f"validation, forms and the admin: a scope-guarded one makes "
                f"full_clean() raise, and a bespoke one silently changes what those "
                f"see.",
                hint=(
                    "Declare no manager of your own on a store-scoped model. If you "
                    'must, set Meta.default_manager_name = "all_objects" - Django '
                    "skips the inherited default the moment a model declares any "
                    "local manager."
                ),
                obj=model,
                id="common.E002",
            )
        )
    return errors


def _check_store_field(model, label) -> list[Error]:
    try:
        store_field = model._meta.get_field(STORE_FIELD)
    except FieldDoesNotExist:  # pragma: no cover - the base always declares it
        store_field = None
    related = getattr(store_field, "related_model", None)
    if store_field is None or related is None:
        return [
            Error(f"{label} has no `store` foreign key.", obj=model, id="common.E003")
        ]
    # `related` is still a string when the target app is not loaded (the
    # isolated registries the check's own tests build).
    related_label = (related if isinstance(related, str) else related._meta.label).lower()
    if related_label != STORE_LABEL or store_field.null:
        return [
            Error(
                f"{label}.store must be a non-nullable foreign key to {STORE_LABEL}.",
                obj=model,
                id="common.E003",
            )
        ]
    return []


def _check_org_field(model, label) -> list[Error]:
    """E007: the denormalised organization pointer, in exactly one shape.

    Worth a check rather than a test because it constrains a *class* of models
    that new code joins by inheriting one base: every slice-2 table gets the
    column from `StoreScopedModel`, and the ways to lose it are all quiet ones -
    a subclass re-declaring `org` to add a `related_name`, to add an index "for
    the planner", or to make it nullable so a fixture loads. Each of those
    silently disarms the composite foreign key that is the *only* reason the
    column exists, and none of them fails a query.

    Every clause below is load-bearing:

    * **present, and a foreign key to `orgs.organization`** - the composite key
      `(store_id, org_id) -> orgs_store (id, org_id)` has nothing to reference
      otherwise.
    * **NOT NULL** - PostgreSQL's MATCH SIMPLE skips a composite foreign key
      entirely when either column is NULL, so a nullable `org` is not a weaker
      guarantee, it is *no* guarantee.
    * **`editable=False`** - the value is derived from `store`; a form that
      could set it could re-home a row.
    * **hidden (`related_name="+"`)** - a reverse accessor
      `organization.product_set` hands out rows with neither the store filter
      nor the soft-delete filter. `common.E004` fires on it too; this repeats
      the requirement where the field is described, because E004's message
      talks about accessors and this one talks about the column.
    * **`db_constraint=False`** - measured: a real `org_id -> orgs_organization`
      key takes `FOR KEY SHARE` on the organization row for every insert into
      every store-scoped table, so `create_store`'s row lock would block every
      sale in the organization. The composite key already proves the
      organization exists, transitively through `orgs_store.org_id`.
    * **`db_index=False`** - the indexes that serve a tenant predicate lead with
      `org`, and Django's automatic single-column FK index is a redundant prefix
      of every one of them: pure write cost on the hottest tables in the
      product.
    """
    try:
        field = model._meta.get_field(ORG_FIELD)
    except FieldDoesNotExist:
        field = None
    related = getattr(field, "related_model", None)
    if field is None or related is None:
        return [
            Error(
                f"{label} has no `{ORG_FIELD}` foreign key, so the "
                f"{model._meta.db_table}_store_same_org_fk composite key cannot "
                f"exist and nothing but application code keeps a row's "
                f"organization and its store in step.",
                hint=(
                    f"Do not re-declare `{ORG_FIELD}`: inherit it from "
                    f"StoreScopedModel, which declares it in the one shape the "
                    f"composite key needs."
                ),
                obj=model,
                id="common.E007",
            )
        ]

    # `related` is still a string when the target app is not loaded (the
    # isolated registries the check's own tests build).
    related_label = (related if isinstance(related, str) else related._meta.label).lower()
    wrong = []
    if related_label != ORG_LABEL:
        wrong.append(f"points at {related_label}, not {ORG_LABEL}")
    if field.null:
        wrong.append(
            "is nullable, and PostgreSQL MATCH SIMPLE skips the composite key "
            "entirely when either column is NULL"
        )
    if field.editable:
        wrong.append("is editable, so a form could re-home the row")
    if not getattr(field.remote_field, "hidden", False):
        wrong.append(
            'is not hidden, so `related_name="+"` is missing and the reverse '
            "accessor hands out rows with no store filter"
        )
    if field.db_constraint:
        wrong.append(
            "carries db_constraint=True, which locks the organization row on "
            "every insert and guarantees nothing the composite key does not"
        )
    if field.db_index:
        wrong.append(
            "carries db_index=True, a redundant prefix of every org-leading "
            "index and pure write cost"
        )
    if not wrong:
        return []
    return [
        Error(
            f"{label}.{ORG_FIELD} " + "; ".join(wrong) + ".",
            hint=(
                f"Delete the local declaration and inherit `{ORG_FIELD}` from "
                f"StoreScopedModel."
            ),
            obj=model,
            id="common.E007",
        )
    ]


def _check_org_spelling(models) -> list[Error]:
    """E007, the other half: `organization` is not a column name here.

    Applies to *every* model, not only store-scoped ones, because the damage is
    a schema in which one concept is `org_id` on five tables and
    `organization_id` on fifteen - and because a composite key would then read
    `FOREIGN KEY (organization_id, store_id) REFERENCES orgs_store (id,
    org_id)`, which looks like a bug on every future review. The name is
    settled by four SHA-256-pinned `RunSQL` statements that already contain the
    literal `REFERENCES orgs_store (id, org_id)`.
    """
    errors = []
    for model in models:
        if not isinstance(model, type) or not issubclass(model, Model):
            continue
        if model._meta.abstract:
            continue
        for field in model._meta.local_fields:
            if field.name != REJECTED_ORG_SPELLING:
                continue
            errors.append(
                Error(
                    f"{model._meta.label}.{REJECTED_ORG_SPELLING} spells the tenancy "
                    f"column the long way. It is `{ORG_FIELD}` / `{ORG_FIELD}_id` "
                    f"everywhere in this schema, including inside four already "
                    f"shipped, SHA-256-pinned SQL statements that cannot be edited.",
                    hint=f"Rename the field to `{ORG_FIELD}`.",
                    obj=model,
                    id="common.E007",
                )
            )
    return errors


def _check_relations(model, label) -> list[Error]:
    """E004: no traversable relation may reach this model's rows.

    E006: only store-scoped models may point at a store-scoped model, because
    only they have a store for `save()` to compare against.
    """
    errors = []

    # Relations pointing *into* this model. `related_objects` already excludes
    # hidden (`related_name="+"`) relations, so anything left is traversable.
    for rel in model._meta.related_objects:
        accessor = rel.get_accessor_name()
        if getattr(rel, "hidden", False) or (accessor or "").endswith("+"):
            continue
        source = rel.field.model
        errors.append(
            Error(
                f"{source._meta.label}.{rel.field.name} exposes {label} rows through "
                f"the reverse accessor `{accessor}`, which applies neither the store "
                f"filter nor the soft-delete filter.",
                hint=(
                    'Use related_name="+" on that foreign key and read through '
                    f"{model.__name__}.objects.for_store(store)."
                ),
                obj=model,
                id="common.E004",
            )
        )

    # Everything that points at this model, hidden or not, must be store-scoped.
    for rel in model._meta.get_fields(include_hidden=True):
        if not rel.is_relation or getattr(rel, "concrete", False):
            continue
        field = getattr(rel, "field", None)
        source = getattr(field, "model", None)
        if source is None or source._meta.abstract:
            continue
        if not (isinstance(source, type) and issubclass(source, StoreScopedModel)):
            errors.append(
                Error(
                    f"{source._meta.label}.{field.name} points at store-scoped {label}, "
                    f"but {source._meta.label} is not store-scoped, so nothing can check "
                    f"that the two rows share a store.",
                    hint=(
                        "Make the referencing model store-scoped, or reference the "
                        "store-level parent instead."
                    ),
                    obj=model,
                    id="common.E006",
                )
            )

    # Relations *out* of this model must not create an accessor on the target.
    for field in model._meta.get_fields():
        if not getattr(field, "concrete", False) or not field.is_relation:
            continue
        remote = getattr(field, "remote_field", None)
        if remote is None or getattr(remote, "hidden", False):
            continue
        target = field.related_model
        if isinstance(target, str):
            target_label = target
        else:
            target_label = getattr(target, "_meta", None) and target._meta.label
        errors.append(
            Error(
                f"{label}.{field.name} creates the reverse accessor "
                f"`{remote.get_accessor_name()}` on {target_label}, which would hand out "
                f"{label} rows with no store filter.",
                hint='Use related_name="+" on this foreign key.',
                obj=model,
                id="common.E004",
            )
        )
    return errors


def is_public_id_surrogate(field) -> bool:
    """True for the one non-primary-key `unique=True` E005 allows.

    Deliberately narrow on all five counts, because this is the single hole in
    a rule whose whole job is refusing global uniqueness:

    * **the name** - `public_id` and nothing else. A `token` column with the
      same type and the same `editable=False` is still the cross-tenant
      existence oracle E005 exists for;
    * **the type** - a `UUIDField`, so the value is 122 random bits and not a
      guessable sequence;
    * **`editable=False`** - it appears in no `ModelForm`, so no form can
      report "already taken" for another tenant's value, and nothing can
      rewrite an identifier a URL already names;
    * **a default** - the row cannot be written without one;
    * **`NOT NULL`** - PostgreSQL treats NULLs as distinct in a unique index,
      so a nullable identifier column would be unique in name only, and a NULL
      `public_id` has no URL at all.

    Both `has_default()` and `has_db_default()` count: `PublicIdModel` uses a
    Python default (see its docstring for why), and a later addition of
    `db_default=UUID7()` alongside it must not turn this check red.
    """
    return (
        field.name in IDENTITY_COLUMNS
        and isinstance(field, UUIDField)
        and not field.editable
        and not field.null
        and (field.has_default() or field.has_db_default())
    )


def _check_fk_target(model, label, constraint, tenant) -> list[Error]:
    """The third valid shape: `(id, <tenant>)`, backing a composite FK.

    Reached for any unique constraint that names the primary key, because such
    a constraint is *logically implied by the primary key* - `id` is already
    globally unique, so `(id, anything)` enforces nothing that was not already
    enforced. Its one legitimate purpose is being the referenced side of a
    `FOREIGN KEY (child_id, org_id) REFERENCES parent (id, org_id)`, which is
    how this schema refuses a row that mixes two organizations.

    Two consequences, and neither is discretionary:

    * it is **exempt from the live-rows rule**, because PostgreSQL refuses a
      partial unique index as a foreign-key target (measured: `ADD FOREIGN KEY`
      answers "there is no unique constraint matching given keys for referenced
      table"), and because it carries no existence oracle the primary key did
      not already grant;
    * conversely, a **conditioned** one is an error rather than a tolerated
      oddity: it disarms the constraint's only purpose, and it fails later and
      further away - in a migration, at `ADD FOREIGN KEY` time.
    """
    fields = set(constraint.fields or ())
    suffix = FK_TARGET_SUFFIXES[ORG_FIELD if tenant <= ORG_COLUMNS else STORE_FIELD]

    if len(tenant) != 1 or fields != {"id"} | tenant:
        return [
            Error(
                f"{label}: unique constraint {constraint.name!r} includes the primary "
                f"key, so it is implied by the primary key and enforces nothing. The "
                f"only shape that has a purpose is exactly (id, org) or (id, store), "
                f"backing a same-organization composite foreign key.",
                hint=(
                    "Drop the primary key from the constraint and condition the "
                    "business key on deleted_at, or reduce it to (id, org) / "
                    "(id, store) and name it accordingly."
                ),
                obj=model,
                id="common.E005",
            )
        ]

    errors = []
    if not (constraint.name or "").endswith(suffix):
        errors.append(
            Error(
                f"{label}: unique constraint {constraint.name!r} is shaped like a "
                f"composite-foreign-key target but is not named like one, so it is "
                f"exempt from the live-rows rule without saying why.",
                hint=f"Name it <table>{suffix}.",
                obj=model,
                id="common.E005",
            )
        )
    if constraint.condition is not None:
        errors.append(
            Error(
                f"{label}: unique constraint {constraint.name!r} is a "
                f"composite-foreign-key target and must be unconditional. A condition "
                f"makes it a partial unique index, and PostgreSQL refuses a partial "
                f"unique index as a foreign-key target.",
                hint="Remove the condition; this constraint reserves nothing.",
                obj=model,
                id="common.E005",
            )
        )
    return errors


def _check_unique_constraint(model, label, constraint) -> list[Error]:
    """One `UniqueConstraint`, classified into exactly one kind.

    Classification is by the column *names the constraint references* and never
    by the model's field set. That is what makes the rule correct both before
    and after the `org` column arrives on `StoreScopedModel`: today no
    store-scoped model has one, so shape 2 and the `(id, org)` target are
    simply unreachable (Django's own `models.E012` reports a constraint naming
    a field that does not exist), and the day the column lands they become
    reachable with no change here.
    """
    fields = set(constraint.fields or ())
    referenced = set(fields)
    for expression in getattr(constraint, "expressions", ()) or ():
        referenced |= _expression_names(expression)

    if referenced & PK_COLUMNS:
        # `fields`, not `referenced`: an expression-based constraint cannot back
        # a foreign key at all, so `UniqueConstraint(Lower("id"), F("org"))`
        # falls into the "enforces nothing" branch and is rejected there.
        return _check_fk_target(model, label, constraint, fields & TENANT_COLUMNS)

    errors = []
    in_store = bool(referenced & STORE_COLUMNS)
    in_org = bool(referenced & ORG_COLUMNS)
    declared_org_wide = (constraint.name or "").endswith(PER_ORG_SUFFIX)

    if not (in_store or in_org):
        if referenced & IDENTITY_COLUMNS:
            errors.append(
                Error(
                    f"{label}: unique constraint {constraint.name!r} names the public "
                    f"identifier. Its uniqueness is declared once, on the field, and "
                    f"a second declaration in Meta is one a subclass with its own Meta "
                    f"can silently drop.",
                    hint="Remove it: PublicIdModel already carries unique=True.",
                    obj=model,
                    id="common.E005",
                )
            )
        else:
            errors.append(
                Error(
                    f"{label}: unique constraint {constraint.name!r} names neither "
                    f"`store` nor `org`, so it is enforced across every tenant and "
                    f"full_clean() reports another tenant's value as taken.",
                    hint=(
                        'Add "store" to the constraint\'s fields, or "org" plus a '
                        f'name ending in "{PER_ORG_SUFFIX}" if the key really is '
                        "unique across the organization's stores."
                    ),
                    obj=model,
                    id="common.E005",
                )
            )
    elif in_store and declared_org_wide:
        errors.append(
            Error(
                f"{label}: unique constraint {constraint.name!r} is named "
                f'"{PER_ORG_SUFFIX}" but includes `store`, so the database enforces it '
                f"per store while the name claims organization-wide. The name is what "
                f"an operator reads out of an IntegrityError.",
                hint=(
                    f"Drop `store` from the fields, or drop the {PER_ORG_SUFFIX!r} "
                    f"suffix from the name."
                ),
                obj=model,
                id="common.E005",
            )
        )
    elif in_org and not in_store and not declared_org_wide:
        # `not in_store` is what keeps the recommended per-store shape legal:
        # once `org` is on the base, `(org, store, name)` is the index the
        # planner wants under RLS, and it is a *per store* key that happens to
        # lead with the organization. Only `org` without `store` is org-wide.
        errors.append(
            Error(
                f"{label}: unique constraint {constraint.name!r} is enforced across "
                f"the whole organization, which has to be declared rather than "
                f"inferred - it is also what a per-store key looks like when `org` "
                f"was typed by habit.",
                hint=(
                    f"If it really is unique across the organization's stores, name "
                    f"it <table>_unique_live_<rule>{PER_ORG_SUFFIX}. Otherwise use "
                    f'"store" instead of "org".'
                ),
                obj=model,
                id="common.E005",
            )
        )

    if not _requires_live_rows(constraint.condition):
        errors.append(
            Error(
                f"{label}: unique constraint {constraint.name!r} is not limited to "
                f"live rows, so a soft-deleted row reserves its value for good.",
                hint="Add condition=Q(deleted_at__isnull=True).",
                obj=model,
                id="common.E005",
            )
        )
    return errors


def _check_uniqueness(model, label) -> list[Error]:
    """E005: uniqueness on a store-scoped table comes in exactly three shapes.

    **(1) Per store** - the default: the constraint names `store`, and its
    condition insists at AND level on `deleted_at IS NULL`. **(2) Per
    organization, across its stores** - an invoice number unique across an
    organization's five shops: it names `org`, excludes `store`, still insists
    on live rows, and its name ends in `_per_org`. **(3) A composite-FK
    target** - exactly `(id, org)` or `(id, store)`, named for it, and exempt
    from the live-rows rule (`_check_fk_target`).

    Everything else is a startup error: `unique=True` on a non-primary-key
    field, except the `public_id` surrogate (`is_public_id_surrogate`);
    `unique_together`, which cannot be conditioned on `deleted_at` at all; a
    constraint naming neither tenant column; a correct organization-wide shape
    that does not declare itself; a `_per_org` name that includes `store`; and
    any of the three shapes missing what it needs.

    E005 deliberately does **not** adjudicate whether a given business key
    *should* be per store or per organization. `UniqueConstraint(fields=["org",
    "name"], condition=LIVE, name="..._per_org")` on a table whose natural key
    is per store is a business bug, not a tenancy leak, and a check that
    guesses intent would be wrong in both directions. E005 refuses leaks; the
    per-model choice belongs to product-owner and database-engineer.
    """
    errors = []
    for field in model._meta.local_fields:
        if field.primary_key or not field.unique:
            continue
        if is_public_id_surrogate(field):
            continue
        errors.append(
            Error(
                f"{label}.{field.name} is unique=True, which is global: it leaks "
                f"whether another tenant already used a value, and it never expires "
                f"when a row is soft-deleted.",
                hint=(
                    "Replace it with UniqueConstraint(fields=[\"store\", "
                    f'"{field.name}"], condition=Q(deleted_at__isnull=True), ...). '
                    f"The only exemption is the {PUBLIC_ID_FIELD} surrogate on "
                    f"PublicIdModel."
                ),
                obj=model,
                id="common.E005",
            )
        )
    if model._meta.unique_together:
        errors.append(
            Error(
                f"{label} uses unique_together, which cannot be conditioned on "
                f"deleted_at.",
                hint="Use Meta.constraints with UniqueConstraint(condition=...).",
                obj=model,
                id="common.E005",
            )
        )
    for constraint in model._meta.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        errors += _check_unique_constraint(model, label, constraint)
    return errors


def audit_store_scoped_models(models) -> list[Error]:
    """Return one `Error` per store-scoped model that broke the contract."""
    errors = []
    for model in models:
        if not _is_concrete_scoped_model(model):
            continue
        label = model._meta.label
        errors += _check_managers(model, label)
        errors += _check_store_field(model, label)
        errors += _check_org_field(model, label)
        errors += _check_relations(model, label)
        errors += _check_uniqueness(model, label)
    errors += _check_org_spelling(models)
    return errors


@register(Tags.models)
def check_store_scoped_models(app_configs, **kwargs):
    if app_configs is None:
        models = global_apps.get_models()
    else:
        models = [model for config in app_configs for model in config.get_models()]
    return audit_store_scoped_models(models)


@register(Tags.security)
def check_database_is_not_test_named(app_configs, **kwargs):
    """E100: refuse a production database whose name starts with `test_`.

    Gated on `settings.ENFORCE_NON_TEST_DATABASE`, set only by prod settings, so
    it never fires while Django is building/using a real `test_*` database in the
    suite. The append-only trigger waives its TRUNCATE guard for `test_*` names;
    a mis-named production database would inherit that waiver silently, so a
    pre-boot `manage.py check` refuses to start on one.

    NOT `Tags.database`, which is where this started and where it was inert:
    `CheckRegistry.run_checks` drops every database-tagged check unless an alias
    is passed explicitly ("they do more than mere static code analysis"), and
    plain `manage.py check` passes none - so the guard never ran. This one is
    pure `settings.DATABASES` string inspection and opens no connection, so it
    belongs with the security checks, and asking the entrypoint for
    `check --database default` would have been the wrong repair: it would make a
    connection-free guard depend on a reachable database at boot.
    `tests/test_common_checks.py` drives it through the registry, never directly,
    because a direct call passed with the broken tag.
    """
    if not getattr(settings, "ENFORCE_NON_TEST_DATABASE", False):
        return []
    errors = []
    for alias, config in settings.DATABASES.items():
        name = str(config.get("NAME") or "")
        if name.startswith("test_"):
            errors.append(
                Error(
                    f"Database {alias!r} is named {name!r}, which starts with "
                    f"'test_'. The append-only TRUNCATE guard waives itself for "
                    f"such names, so this must never be a production database.",
                    hint="Rename the database, or unset ENFORCE_NON_TEST_DATABASE.",
                    id="common.E100",
                )
            )
    return errors


@register(Tags.security)
def check_ssl_redirect_trusts_a_proxy_header(app_configs, **kwargs):
    """E101: refuse to boot into an HTTPS redirect loop.

    `SECURE_SSL_REDIRECT = True` behind a TLS-terminating proxy, with no
    `SECURE_PROXY_SSL_HEADER`, is a total outage and not a subtle one: the proxy
    speaks TLS to the client and plain HTTP to us, so Django sees `http`,
    answers `301 -> https`, and the proxy forwards the retry as `http` again.
    Every request loops until the client gives up. Measured under
    `config.settings.prod`, which shipped exactly that pair.

    The fix cannot be a value here - which header carries the client's scheme
    is a fact about the proxy in front of a deployment that does not exist yet -
    so this is a refusal instead: `config/settings/prod.py` reads the header
    from `DJANGO_SECURE_PROXY_SSL_HEADER`, validates it, and leaves it unset
    when the operator has not chosen one. Then this check stops the boot.

    Four properties, each learned from `common.E100`:

    * **`Tags.security`, never `Tags.database`.** `CheckRegistry.run_checks`
      drops database-tagged checks unless an alias is passed, and a bare
      `manage.py check` passes none, so E100 sat inert for two review rounds.
    * **No database connection.** Pure `settings` inspection, so a pre-boot
      `manage.py check` in a container does not need a reachable database to
      report this.
    * **An `Error`, not a `Warning`.** Django's own `security.W008` covers the
      missing-redirect half, but it is `--deploy`-only and a warning does not
      fail `manage.py check`.
    * **The gate is the condition, not a second flag.** Dev and test settings
      never set `SECURE_SSL_REDIRECT`, so this cannot fire inside the suite and
      there is no `ENFORCE_...` switch to forget.

    Deliberately not covered: a process that terminates TLS itself needs no
    proxy header and would be refused here. This product has one deployment
    shape - a container behind a reverse proxy - and inventing an opt-out for a
    shape nobody runs would be a setting nobody sets and a hole anybody could
    use. Add it, with a measurement, the day the shape exists.
    """
    if not getattr(settings, "SECURE_SSL_REDIRECT", False):
        return []
    header = getattr(settings, "SECURE_PROXY_SSL_HEADER", None)
    well_formed = (
        isinstance(header, (tuple, list))
        and len(header) == 2
        and all(isinstance(part, str) and part for part in header)
    )
    if well_formed:
        return []
    return [
        Error(
            f"SECURE_SSL_REDIRECT is on and SECURE_PROXY_SSL_HEADER is {header!r}. "
            f"Behind a TLS-terminating proxy Django sees 'http', redirects to "
            f"'https', and the proxy forwards the retry as 'http': every request "
            f"loops for ever.",
            hint=(
                "Set DJANGO_SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https "
                "(or whichever header your proxy overwrites) so Django can tell a "
                "proxied HTTPS request from a plain one. Only for a header the "
                "proxy overwrites unconditionally: Django trusts it absolutely, so "
                "a client-supplied one would turn the redirect off. If nothing "
                "terminates TLS in front of this process, turn SECURE_SSL_REDIRECT "
                "off instead - it cannot work there either."
            ),
            id="common.E101",
        )
    ]
