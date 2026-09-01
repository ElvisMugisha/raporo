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
- a unique constraint that omits `store` turns `full_clean()` into a
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
from django.db.models import Q, UniqueConstraint

from common.managers import STORE_FIELD, STORE_LABEL, StoreScopedManager
from common.models import StoreScopedModel

DEFAULT_MANAGER_NAME = "all_objects"
STORE_COLUMNS = {STORE_FIELD, f"{STORE_FIELD}_id"}


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


def _check_uniqueness(model, label) -> list[Error]:
    """E005: uniqueness on a store-scoped table is per store, among live rows."""
    errors = []
    for field in model._meta.local_fields:
        if field.primary_key or not field.unique:
            continue
        errors.append(
            Error(
                f"{label}.{field.name} is unique=True, which is global: it leaks "
                f"whether another tenant already used a value, and it never expires "
                f"when a row is soft-deleted.",
                hint=(
                    "Replace it with UniqueConstraint(fields=[\"store\", "
                    f'"{field.name}"], condition=Q(deleted_at__isnull=True), ...).'
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
        referenced = set(constraint.fields or ())
        for expression in getattr(constraint, "expressions", ()) or ():
            referenced |= _expression_names(expression)
        if not referenced & STORE_COLUMNS:
            errors.append(
                Error(
                    f"{label}: unique constraint {constraint.name!r} does not include "
                    f"`store`, so it is enforced across every tenant.",
                    hint="Add \"store\" to the constraint's fields.",
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


def audit_store_scoped_models(models) -> list[Error]:
    """Return one `Error` per store-scoped model that broke the contract."""
    errors = []
    for model in models:
        if not _is_concrete_scoped_model(model):
            continue
        label = model._meta.label
        errors += _check_managers(model, label)
        errors += _check_store_field(model, label)
        errors += _check_relations(model, label)
        errors += _check_uniqueness(model, label)
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
