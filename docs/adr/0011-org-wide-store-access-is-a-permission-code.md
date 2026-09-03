# 0011. Org-wide store access is a permission code, never a role name

Date: 2026-09-02
Status: Proposed
Supersedes: the `StoreAccess` docstring rule *"materialised even for owners: explicit rows
beat an implicit 'owners see everything' rule"* (`apps/orgs/models.py`)

## Context

Elvis has ruled on store access: *"A user may access more than one store, and only if they
were given access to both of them. But the owner of the org can access any store under their
org."*

The second half contradicts what is built. `StoreAccess` today is the whole answer: its
docstring says owner access is materialised as explicit rows, and `register_owner` creates
one per store. That design deliberately rejected the implicit rule Elvis has now asked for.

Two facts make the obvious implementation dangerous.

**`Role.name` is not an authorization primitive.** It is a user-editable `CharField`, unique
only among live rows and only within one organization. An org can rename its Owner role — and
should be able to; Raporo ships EN/RW/FR — which would silently *remove* the override. An org
can create a second role called "Owner" with no real power, which a name check would silently
*grant* the override to. The ledger already records the adjacent hazard: `PRESETS["Manager"]`
holds `member.manage` without `role.manage`, so a Manager can promote a member (including
themselves) into the Owner role. A name-based check would be a live vulnerability here, not a
style problem.

**There is no code for it.** `apps/orgs/permissions.py` carries twelve codes
(`member.manage`, `role.manage`, `store.manage`, `sale.record`, …) and none of them means
"may reach any store in the org". `store.manage` is about creating and editing stores, which
is a different question from reaching their rows.

And the override lands entirely in the layer the database does not cover. The RLS threat model
concludes: *"Inside an organization, RLS is blind and the application-layer guards are the
entire defence."* Org isolation is a database fact after ADR 0009; store isolation is Python,
and this decision is a store-isolation decision.

## Decision

We will add one permission code, **`store.access_all`**, labelled *"Access every store in the
organization"*, and resolve the permitted store set in exactly one function:
`apps/orgs/services/access.py::permitted_stores(membership) -> StoreSet`. A membership whose
role holds `store.access_all` gets every live store in its organization; every other
membership gets the live stores named by its live `StoreAccess` rows. Nothing else in the
codebase reads `StoreAccess` or decides which stores an actor may reach.

Four rules make that safe rather than merely tidy.

1. **Owner memberships get no `StoreAccess` rows.** The grant is the role, so a row that does
   not control access would be a decoy for anyone auditing "who can reach store A2". The
   docstring's auditability argument does not survive; the audit trail of role edits replaces
   it and is strictly better, because it records the *decision* rather than its fan-out.

2. **`store.access_all` widens reach and grants no rights.** Which stores (this resolver) and
   which actions (`Role.has(code)`) stay orthogonal axes, so a custom role may hold
   `store.access_all` with only `report.generate` — a company accountant who reads every
   branch and writes nowhere. Views must pass both gates; `require_store_permission()` exists
   so the pair is hard to half-use.

3. **Presets become exhaustive and are checked at startup.** `PRESETS["Manager"]` is defined
   today as `PERMISSIONS - {ROLE_MANAGE, STORE_MANAGE}`, so adding any code to the catalog
   grants it to Manager silently — which would hand Manager the override and break Elvis's
   rule on the day the code is introduced. Every preset is rewritten as an explicit
   `frozenset`, and `common.E010` fails startup unless every code in the catalog is either
   listed in a preset or listed in a declared `UNASSIGNED` set. A new code cannot enter the
   catalog without someone deciding, in writing, which presets receive it.

4. **The set is never cached across requests, and never in the session.** Revocation must take
   effect immediately, so the resolver queries on every call. At the product cap of five stores
   per organization that is two indexed queries returning at most five rows.

Rejected: **`role.name == "Owner"`** — a rename removes the override and a decoy role grants
it; this is the trap the decision exists to avoid. **A `Role.is_owner` boolean** — a second
authorization axis outside the permission catalog, needing its own editor, audit story and
startup check, when the catalog already is that mechanism. **Keeping materialised owner rows
in addition** — then `create_store` must fan out a row to every owner membership and
`soft_delete_store` must retract them, which is a denormalisation with an invalidation problem,
and when the two disagree the resolver wins, so the rows are decoration.

## Consequences

Easier: `register_owner` stops creating store-access rows for the founding owner and
`create_store` stops fanning out, so both services shrink. An owner sees a store the moment it
is created, with no propagation step. Revoking access is one `soft_delete` on one
`StoreAccess` row and takes effect on the next check. Store access becomes visible and
editable in the role editor rather than in a hidden join table.

Harder, and the sentence to rely on: **if `permitted_stores()` has a bug, every store in that
one organization becomes readable and writable by every member of it, and nothing below Python
will stop it** — RLS checks the organization and the organization is correct, the composite
foreign key checks the organization and the organization is correct, and the store predicate
the query carries is the one the buggy function produced. The blast radius is one organization,
entirely, reads and writes; it is *not* cross-tenant, because the org predicate comes from
`tenant()` and RLS rather than from this function; and the only detection is a test. That is
why the generated denial matrix (`tests/test_tenancy_matrix.py`) is a release gate for this
change and not a follow-up.

Also harder: this raises the severity of the already-recorded `PRESETS["Manager"]`
self-promotion hazard. A Manager holding `member.manage` without `role.manage` can move a
member into the Owner role, which now carries `store.access_all` — so what was "reshape roles
within my own store set" becomes "read and write every store in the organization". Routed to
`security-engineer`; it is a separate ruling, and rule 3 above does not close it.

Denials are **404, never 403**: a 403 confirms the row exists and turns the override's
complement into an existence oracle across sibling stores. `StoreNotPermitted` therefore must
not subclass `PermissionDenied`, whose default Django handler renders 403.

Revisit when a store's numbers become confidential *within* an organization — separate legal
entities, or a per-store investor portal. Then `store.access_all` is the code to withhold and
the store-id GUC of ADR 0009's rejected alternative becomes the additive next step. Revisit
also if `MAX_STORES_PER_ORG` rises past roughly one hundred, at which point enumerating the
owner's store set in an `IN` list stops being the cheapest correct pin.
