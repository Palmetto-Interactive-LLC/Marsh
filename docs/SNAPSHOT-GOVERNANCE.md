# Snapshot inventory and retention

Marsh treats Daytona snapshots as immutable production artifacts. Activated
fleet profiles in the private operations source are the source of truth for
which snapshot names are in use; the Daytona inventory is observed state.
Public profile examples demonstrate the schema but are never an authoritative
production inventory. Inventory is read-only. Retirement is a separate,
explicitly approved operation because Daytona snapshot deletion is
irreversible.

Daytona documents the snapshot list endpoint and lifecycle states in its
[snapshot guide](https://www.daytona.io/docs/en/snapshots/). The audit command
uses only paginated `GET /snapshots` requests.

## Run the audit

Run a live inventory only from the private operations checkout that holds the
activated profile set. Use that deployment's secret manager to inject
`DAYTONA_API_KEY`, then run:

```bash
umask 077
python3 infra/snapshots/audit-snapshots.py \
  > /tmp/marsh-snapshot-audit.json
```

The command reads `DAYTONA_API_KEY` from the injected environment, derives the
API base and snapshot references from the activated private profile set, and
emits an allowlisted JSON report. It does not print request headers,
credentials, provider error bodies, or arbitrary response fields. Do not put a
credential value or secret-manager reference in the command or source; runtime
secret resolution belongs to the private operations environment.

An already captured response can be inspected without credentials or network
access:

```bash
umask 077
python3 infra/snapshots/audit-snapshots.py \
  --input /secure/path/daytona-snapshots.json \
  > /tmp/marsh-snapshot-audit.json
```

The raw input may contain organization metadata or provider error details. Keep
it outside the repository and remove it after producing the allowlisted report.

Each inventory row preserves Daytona's values, when present, for:

- snapshot ID, name, state, size, and creation timestamp;
- image name and snapshot reference;
- CPU, memory, disk, GPU, sandbox class, and regions;
- every activated private fleet profile and size class that references the
  snapshot.

`size` remains in the provider's raw unit because the API schema does not name
that unit. The audit does not relabel or convert it. Missing API fields are
reported as `null`, not inferred.

The report also lists:

- configured profile references absent from Daytona;
- duplicate immutable names that resolve to more than one provider snapshot ID;
- Daytona snapshots not referenced by any activated production fleet profile;
- duplicate candidates with the same reported image and resource shape;
- cross-fleet candidates that may be replaced by one shared snapshot.

`destructive_actions` is always an empty array. Neither the audit command nor
its tests contain a Daytona deletion request.

## Immutable naming

Every production build gets a new versioned snapshot name. Do not delete and
recreate an existing name.

Use one canonical name per resource class when fleets share the same runner
contract:

```text
marsh-runner-default-v7
marsh-runner-large-v7
```

The version identifies one immutable image build. The image should be addressed
by digest for final production registration. A mutable registry tag or matching
snapshot size is insufficient proof that two snapshots contain identical bits.

`infra/snapshots/register-snapshot.sh` currently deletes a named snapshot before
recreating it. That legacy wrapper must not be used to roll a shared production
snapshot. The shared-snapshot delivery must first replace that behavior with
create-new/versioned registration and an explicit profile migration.

## Candidate rules

The audit deliberately calls matches *candidates*, not confirmed duplicates. It
groups two or more differently named snapshots only when Daytona reports the
same:

- image name;
- CPU, memory, disk, and GPU allocation;
- sandbox class; and
- region set.

If any comparison dimension is absent, the audit does not create a candidate.
If one immutable name has multiple provider IDs, every matching row is marked
`ambiguous_name_collision` until the collision is resolved.

A group becomes a `shared_candidate` when its profile references span more than
one fleet. Before consolidation, record all of the following in the delivery
issue:

1. The source image digest is identical for every candidate, or every fleet is
   intentionally migrating to one newly built digest.
2. Dockerfile, entrypoint, architecture, installed tools, runner version, and
   cache hooks are one reviewed runner contract.
3. The canonical snapshot is `active` in every required region with the exact
   CPU, memory, and disk shape expected by the profiles.
4. No fleet-specific credential, network path, organization data, or mutable
   cache content was baked into the image.
5. A no-secret canary completes for each affected size class and each affected
   fleet.

`potential_reclaimable_size` is emitted only when every member reports the same
non-null raw size. It assumes one member remains and is an estimate, not deletion
authorization or billing truth.

## Migration and rollback

Move activated profiles in the private operations source through a pull request
from one immutable name to another. Keep the old name available throughout
rollout.

1. Build and register the new versioned snapshot without changing or deleting
   the old snapshot.
2. Audit the inventory and verify exact ID, name, state, resource shape, image
   digest, and region availability.
3. Canary one zero-floor class, then one fleet at a time. Verify queue pickup,
   job completion, sandbox teardown, watchdog health, and sibling fleets.
4. Merge the profile change and deploy the exact merged `main` commit.
5. If reliability, queue latency, or job duration regresses, restore the prior
   snapshot name in source, merge, and redeploy. Do not repair a version in
   place.

## Retention policy

Always retain:

- every snapshot referenced by an activated private profile or by the currently
  deployed fleet commit;
- the current canonical snapshot for each resource class;
- any snapshot supporting an active canary, investigation, or rollback; and
- the immediately previous known-good generation until both the time and usage
  gates below pass.

The previous generation becomes a retirement candidate only after all affected
fleets have stopped referencing it for at least 14 calendar days **and** each
affected fleet has completed at least 100 successful cycles on the replacement.
Any open reliability incident resets the observation period.

Failed or abandoned build snapshots become review candidates after seven days
if they were never profile-referenced and no investigation needs them. Other
unreferenced snapshots become review candidates after 30 days. Inactivity alone
is not approval: Daytona may automatically mark an unused snapshot inactive,
and inactive snapshots can still be required for rollback.

## Manual retirement gate

There is no automatic deletion path. A retirement change requires a Linear
issue and a fresh audit attached to it. The operator must verify:

- the snapshot ID and name match the approved candidate exactly;
- no activated profile, active sandbox, or rollback record references it;
- the retention period and successful-cycle gate passed;
- the replacement is active and its canaries are healthy; and
- a second inventory after retirement contains only the approved removals.

If any field or reference is missing or ambiguous, retain the snapshot and
investigate. Cost pressure does not override an incomplete rollback record.
