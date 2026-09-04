"""One live membership per user (Elvis's ruling; schema plan §J).

**SAFE ONLINE — today only, and that is a window that closes.**

`AddConstraint` on a conditional `UniqueConstraint` emits
`CREATE UNIQUE INDEX ... WHERE deleted_at IS NULL`. Against the zero rows every
table in every environment currently holds, that is instantaneous under an
`AccessExclusiveLock` held for a moment, so this ships as an ordinary atomic,
non-concurrent migration. Reverse is `RemoveConstraint`: a metadata-only
`DROP INDEX`, which is also the whole allow-multi-org-later path (see
`Membership`'s docstring — do not replace this table with a FK on `User`).

On a **populated** table the same one line costs a project, so slice 2 should
read this as a deadline:

1. the index build fails outright and names exactly one offender per attempt
   (`Key (user_id)=(45) is duplicated`) — you cannot iterate your way out of
   300 duplicates by re-running `migrate`;
2. so it starts with a detection query:

       SELECT user_id, count(*), array_agg(org_id ORDER BY id)
       FROM   orgs_membership
       WHERE  deleted_at IS NULL
       GROUP  BY user_id
       HAVING count(*) > 1;

3. then a decision *per offender* — which org keeps this person is a business
   question with a human on the other end, never a data-migration default;
4. then the build under `ShareLock`, which blocks writes to `orgs_membership`
   for its duration: **NEEDS A WINDOW** with a `pg_dump -Fc` backup and a
   rehearsed rollback, or `AddIndexConcurrently`-style handling with
   `atomic = False` and an INVALID-index rollback path.

The constraint is also the index the hot path wants — "which org and role is
this logged-in user" runs on every authenticated request — so it pays for
itself as a lookup index. No `INCLUDE`: measured elsewhere at 4 µs saved for
+80 % index size on a table this small.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orgs", "0002_public_id"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="membership",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True)),
                fields=("user",),
                name="orgs_membership_unique_live_user",
                violation_error_code="unique",
                violation_error_message="That account already belongs to an organization. Remove it there first, or invite a different email address.",
            ),
        ),
    ]
