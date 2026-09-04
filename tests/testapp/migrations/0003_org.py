"""The test-only stand-ins get the denormalised `org` column and its key.

`StoreScopedModel` now carries `org`, so every concrete subclass needs the
column and the composite foreign key
`(store_id, org_id) -> orgs_store (id, org_id)` that makes invariant #1 a
property of the schema. `tests.testapp` is installed by `config.settings.test`
only, so this migration never reaches a real database - and it is the *only*
migration this change needs, because `StoreScopedModel` has no concrete
production subclass yet. `makemigrations --check --dry-run` under
`config.settings.dev` proves that rather than asserting it.

SAFE ONLINE, trivially: these tables are created and dropped by the test
runner, so `ADD COLUMN org_id bigint NOT NULL` with no default is correct here.
A production table is a different problem and will need the standard three
steps (add nullable, backfill in batches, `SET NOT NULL` behind a validated
CHECK) plus `ADD CONSTRAINT ... NOT VALID` followed by `VALIDATE CONSTRAINT`,
which takes only a `SHARE UPDATE EXCLUSIVE` lock. Slice 2's tables are created
with the column, so they need neither.

One `RunSQL` per table with the table name written out, never a loop over
`StoreScopedModel.__subclasses__()`: a migration that reads the model registry
at apply time emits different SQL depending on which models happen to exist
when it runs, which is exactly the fork the SHA-256 pins in
`tests/test_db_stability.py` exist to prevent - and the pinned text would
itself become a function of the registry. The loop lives in
`tests/test_tenancy_org_column.py`, where it enumerates the registry and
asserts the key per table, with a premise assertion so it cannot pass on an
empty set.
"""

import django.db.models.deletion
from django.db import migrations, models

from common.db import same_org_fk_v1

#: One call per table, table name written out, no loop - not even over a
#: literal list. See the module docstring: the shape a reviewer has to be able
#: to read at a glance is "this migration installs exactly these five keys".
PRODUCT_FK, PRODUCT_FK_REVERSE = same_org_fk_v1("testapp_product")
SALE_FK, SALE_FK_REVERSE = same_org_fk_v1("testapp_sale")
SALELINE_FK, SALELINE_FK_REVERSE = same_org_fk_v1("testapp_saleline")
SCOPEDTHING_FK, SCOPEDTHING_FK_REVERSE = same_org_fk_v1("testapp_scopedthing")
OWNMETA_FK, OWNMETA_FK_REVERSE = same_org_fk_v1("testapp_scopedthingownmeta")


def org_field():
    """The field as `StoreScopedModel` declares it. `db_constraint=False` is
    measured, not stylistic: a real `org_id -> orgs_organization` foreign key
    takes `FOR KEY SHARE` on the organization row for every insert into every
    store-scoped table, so `create_store`'s `SELECT ... FOR UPDATE` on that row
    would block every sale in the organization. The composite key above already
    proves the organization exists, transitively through `orgs_store.org_id`.
    `db_index=False` because the org-leading composite indexes are the ones the
    planner wants and Django's automatic single-column FK index would be a
    redundant prefix of every one of them."""
    return models.ForeignKey(
        db_constraint=False,
        db_index=False,
        editable=False,
        on_delete=django.db.models.deletion.PROTECT,
        related_name="+",
        to="orgs.organization",
        verbose_name="organization",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("orgs", "0001_initial"),  # orgs_store_id_org_uniq, the key's target
        ("testapp", "0002_public_id"),
    ]

    operations = [
        migrations.AddField(model_name="product", name="org", field=org_field()),
        migrations.AddField(model_name="sale", name="org", field=org_field()),
        migrations.AddField(model_name="saleline", name="org", field=org_field()),
        migrations.AddField(model_name="scopedthing", name="org", field=org_field()),
        migrations.AddField(
            model_name="scopedthingownmeta", name="org", field=org_field()
        ),
        migrations.RunSQL(sql=PRODUCT_FK, reverse_sql=PRODUCT_FK_REVERSE),
        migrations.RunSQL(sql=SALE_FK, reverse_sql=SALE_FK_REVERSE),
        migrations.RunSQL(sql=SALELINE_FK, reverse_sql=SALELINE_FK_REVERSE),
        migrations.RunSQL(sql=SCOPEDTHING_FK, reverse_sql=SCOPEDTHING_FK_REVERSE),
        migrations.RunSQL(sql=OWNMETA_FK, reverse_sql=OWNMETA_FK_REVERSE),
    ]
