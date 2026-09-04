# 0012. Audit-log destruction at end of retention is a GUC-gated, year-floored DELETE

Date: 2026-09-04
Status: Accepted
Deciders: `database-engineer` (owner) · `privacy-compliance` (accepted the overrule) · `data-reporting-engineer` (period semantics) · `security-engineer` (RLS, and the amended forgery guarantee) · `tech-lead`
Supersedes: the range-partitioning prescription in `docs/superpowers/specs/2026-09-02-privacy-law-058-2021-ruling.md` §R3
Relates to: [ADR 0009](0009-row-level-security-for-organization-isolation.md) (RLS) · [ADR 0010](0010-uuidv7-public-identifiers.md) (UUIDv7 public identifiers)

## Context

`audit_auditlog` is append-only at the database level: `raporo_append_only()` refuses
`UPDATE` and `DELETE` unconditionally and `TRUNCATE` except under `raporo.allow_truncate`,
and `raporo_app` holds no write privilege on the table beyond `INSERT`.

Rwanda Law No. 058/2021 gives a data subject a right to erasure. The retention duty that
overrides it for accounting evidence is ten years, running **from 1 January following the
fiscal year to which the records relate**. At the end of that period rows must genuinely go.

Today the only mechanism is a whole-table `TRUNCATE` behind a session GUC: all-or-nothing,
never per-organization, and a manual step nobody will remember in 2036.

## Decision

End-of-retention destruction is a **GUC-gated, year-floored `DELETE`**, implemented as
`CREATE_APPEND_ONLY_FUNCTION_V2` plus one migration. The trigger keeps refusing `UPDATE`
unconditionally and keeps refusing `DELETE`, with exactly one exemption: the session has set
`raporo.allow_retention_delete = 'on'` **and** every row the statement touches is past the
statutory floor. Rows inside the window are refused row by row, so a purge with a wrong bound
removes **nothing** rather than removing some.

The floor is:

```sql
extract(year from OLD.at AT TIME ZONE 'Africa/Kigali')
  <= extract(year from now() AT TIME ZONE 'Africa/Kigali') - 11
```

**`AT TIME ZONE 'Africa/Kigali'` is load-bearing, and it is a correction to a formula that had
already been reviewed and accepted.** Measured: an audit entry recorded at 00:30 on 1 January
2027 in Kigali reads as year **2026** in a UTC session and year **2027** in a Kigali session.
`config/settings/base.py` sets `TIME_ZONE = "UTC"` and the live application session reports
`TimeZone = UTC`, so the unpinned floor classifies a Rwandan New Year row a full year early and
licenses destroying it twelve months inside the statutory window.

```
one row: 2027-01-01 00:30:00+02  (just after midnight, Kigali New Year)

  UTC session                  -> 2026-12-31 22:30:00+00 -> extract(year) = 2026
  Kigali session               -> 2027-01-01 00:30:00+02 -> extract(year) = 2027
  zone-pinned (either session) -> extract(year from at AT TIME ZONE 'Africa/Kigali') = 2027
  live application session      TimeZone = UTC ; unpinned floor reads 2026
```

That is the **same defect, one layer down**, as the one `privacy-compliance` caught in
`now() - interval '10 years'`. Two independent reviewers each found a timezone-shaped hole in
this one predicate, which is why the zone is written into the ADR rather than left to the
implementer.

The `- 11` (not `- 10`) is `privacy-compliance`'s and is correct: Rwanda's ten years starts on
the 1 January *following* the fiscal year, so a March 2026 row is protected to 31 December 2036
and becomes destroyable on 1 January 2037.

**Standing rule attached: the direction of error is over-retention.** Over-retaining an audit
row by under twelve months on a legally mandated ten-year record is de minimis and defensible.
Under-retaining destroys a customer's tax evidence during an audit window.

### Rejected: range partitioning by year on `at`, with destruction as DETACH + DROP

The textbook answer, and wrong here. Four measured reasons and one process reason.

**1. It breaks ADR 0010.** `UNIQUE (public_id)` is refused on a partitioned table unless it
includes the partition key. Measured:

```
ERROR:  unique constraint on partitioned table must include all partitioning columns
DETAIL:  UNIQUE constraint on table "p_audit" lacks column "at" which is part of
         the partition key.
```

`UNIQUE (public_id, at)` is accepted and is **not** global uniqueness — the same UUID inserted
into the 2026 and 2027 partitions is accepted twice. `public_id` is the identifier that crosses
the process boundary, and ADR 0010 requires it globally unique and never reissued. Partitioning
demotes that from a database guarantee to a hope, on the one table where forgery is the threat.

**2. It punches a hole in the append-only guard, and the hole is silent.** The
`BEFORE UPDATE OR DELETE` **row** trigger propagates to every partition; the `BEFORE TRUNCATE`
**statement** trigger does not — it exists on the parent only. Measured:

```
 tgname  |   on_table   | tgtype
 p_row   | p_audit      |     27     <- row trigger, parent
 p_trunc | p_audit      |     34     <- statement TRUNCATE trigger, parent ONLY
 p_row   | p_audit_2026 |     27     <- propagated
 p_row   | p_audit_2027 |     27     <- propagated
                                     <- NO p_trunc on either partition

TRUNCATE p_audit        -> ERROR: append-only: TRUNCATE refused
TRUNCATE p_audit_2026   -> TRUNCATE TABLE          <- SUCCEEDED, a year destroyed
rows_left = 1, guard fully installed, every test green
```

Closing it needs a manual `CREATE TRIGGER` on each partition every January — a yearly step whose
omission produces no error. That is the phase-1-silent-no-op failure mode this codebase has been
bitten by four times.

**3. The partition boundary's timezone comes from a session GUC at migration-apply time.**
`data-reporting-engineer` measured identical DDL producing `+00` in one session and `+02` in
another. A Rwandan calendar year does not fit a UTC partition — Kigali is +02:00, so the first
two hours of every Rwandan year sit in the previous partition — and `DROP PARTITION` therefore
destroys records **early**. Same direction of error as the floor defect above, arriving through
DDL instead of a predicate.

**4. Pruning does not happen where the reads are.** The period key is `business_date`
(ADR-level ruling, Elvis 2026-09-03); the partition key would be `at`. Measured: `Append` over
four partitions where the unpartitioned table gives a single `Index Scan`. Partitioning would
make every report slower in order to make a once-a-decade delete faster.

**5. The prescribing gate withdrew it.** `privacy-compliance` stated its own prescription was
"reasoned, not measured", and weighted two findings in its own lane above the ORM ones: the
per-partition RLS gap is *a cross-tenant disclosure manufactured by a control whose entire
purpose was data protection*; and its retention mechanism would have destabilised its own
erasure mechanism, because erasure-by-referent depends on exactly the pointer stability that
partitioning removes.

### Also rejected: whole-table TRUNCATE under `raporo.allow_truncate` (the status quo)

All-or-nothing, never per-organization, and it destroys rows inside the window alongside rows
outside it. The GUC stays — it is the reviewed break-glass — but it is not the retention
mechanism.

### Also rejected: application-side deletion through the ORM

`SoftDeleteModel.delete()` raises and `AuditLog` is not soft-deletable. More importantly, a floor
enforced in Python is a floor that a data migration, a `psql` session and a future service can
each forget. The floor belongs in the trigger, where it is the same rule for every caller.

## Consequences

- `CREATE_APPEND_ONLY_FUNCTION_V2` is added **next to** V1 in `common/db.py` — never by editing
  V1 — with `reverse_sql = CREATE_APPEND_ONLY_FUNCTION_V1`, never a `DROP FUNCTION`, which
  Postgres refuses while any guarded table still has a trigger depending on it.
- Its migration must declare a dependency on `("audit", "0002_append_only_trigger")` **and on
  every later migration installing an earlier version**, or a fresh install can apply V2 before
  V1 and end on the wrong body while every migrated database ends on the right one.
- Both hashes go into `PINNED_SQL` in `tests/test_db_stability.py` in the same commit.
- **Sequencing is the database owner's convenience.** `privacy-compliance`'s R3 deadline
  ("before slice 2's four ledger tables") existed only because partition cost multiplied across
  five tables. V2 is one reusable trigger plus one migration, so slice-2 ledger tables acquire
  destruction by declaring the same trigger.
- **`store_name` / `org_name` retention in the trail is bounded by this mechanism, not by
  partitions.** That residual was accepted as lawful *because* an executable destruction path
  with a date exists. If this ADR is never implemented, the residual stops being a bounded
  exposure and becomes permanent retention of a sole trader's personal name.
- The purge predicate is an *expression* on `at`, so `audit_auditlog_at_55383e01` cannot serve
  it. Either drop that index with the redundant-index batch, or replace it with a matching
  expression index on `extract(year from at AT TIME ZONE 'Africa/Kigali')`. Decide with the
  purge, not before.
- A purge is destructive: it needs a `pg_dump -Fc` backup step, a rehearsed rollback with
  `devops-engineer`, and a runbook. No production DDL or DML by hand, ever.
- The purge command must enumerate organizations whose retention has expired, which is the same
  scheduled runner the 30-day post-closure erasure already needs. One runner, two rules.
- **The ten-year figure is graded Medium-High and must be confirmed with a Rwandan tax adviser
  before it is hard-coded.** The formula also assumes a calendar fiscal year; a customer on a
  non-calendar accounting period moves the floor to the later boundary.
- **No early-closure DELETE exemption is granted.** An organization that closes early gets
  anonymisation of referents, per the privacy ruling §4. Anonymisation, not destruction, remains
  the standard answer to an exiting customer — recorded here so nobody later adds an exemption
  believing it was an oversight.
- **The GUC is a safety catch, not the security boundary.** Any role can `SET` a custom GUC —
  the limitation ADR 0009 already accepted. The boundary is `raporo_app` holding no `DELETE` on
  `audit_auditlog`, which is measured and watched refusing.

## Acceptance tests

The guard is unverified until each of these has been **watched refusing**:

1. `DELETE` without the GUC → refused.
2. `DELETE` with the GUC, row inside the window → refused, naming the row's year and the floor.
3. `DELETE` with the GUC, row past the floor → permitted.
4. A statement spanning both → **removes nothing**.
5. `UPDATE` with the GUC set → still refused. The GUC is a retention key, not a write key.
6. The floor computes identically with the session `TimeZone` set to `UTC` and to
   `Africa/Kigali` — **the test that would have caught the defect this ADR corrects.**
7. `TRUNCATE` behaviour is unchanged from V1.
8. One `audit.retention_purged` row per run, carrying the organization, the year range and the
   row count — never row contents — and the command asserts the deleted count against a
   pre-computed count, so a correct purge is distinguishable from one whose predicate was wrong.
