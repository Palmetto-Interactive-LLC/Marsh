# Release and deployment behavior

Marsh publishes immutable open-source source releases. It does not prescribe a
shared staging or production environment, because each adopter owns a separate
runner trust boundary. A published Marsh release is therefore a versioned
source artifact, not an instruction to deploy any fleet.

## Source-release mapping

Marsh uses non-prefixed Calendar Versioning (CalVer): `YYYY.M.PATCH`.

| Git ref | Artifact | Purpose |
| --- | --- | --- |
| `refs/heads/main` | integration branch | Verified source for future releases. |
| `refs/tags/2026.7.3` | immutable GitHub Release | Open-source source release; no fleet deployment implied. |

Create release tags only from verified commits on `main`, using a signed,
annotated tag such as `2026.7.3`. Do not add a `v` prefix.

## Adopter deployments

An adopter may build a deployment pipeline around a Marsh release, but that
pipeline belongs in its private operations source. Keep the following
environment-specific material there:

- cloud roles and deployment targets;
- host inventory and runtime configuration;
- activated fleet profiles and downstream repository rosters;
- runner-group policy, secrets, and private-network controls;
- rollout, rollback, and incident evidence.

The public repository deliberately carries no default deployment target. A
downstream pipeline should promote an exact signed Marsh release, validate it
against the adopter's private profile, then record the deployed revision in its
own operations system.

## Release procedure

Use [RELEASING.md](../RELEASING.md) for the public release steps. Before
publishing, run the verification gate and the current-tree secret and boundary
review. After publishing, confirm the GitHub Release points to the signed tag
and that no deployment-specific information appears in its notes or assets.

## Verification

Check the current release configuration with:

```bash
make verify
```
