# Fleet profile contract

Marsh publishes a credential-free fleet-profile contract, not a production
operations bundle. The public source shows how a profile is shaped and how the
orchestrator enforces runner scope. Activated profiles and their deployment
records belong in a private operations source.

## Public and private boundary

Keep these artifacts out of the public repository:

- activated fleet manifests and repository rosters;
- runner-group identifiers, GitHub App installation bindings, and secret-manager
  references;
- host inventory, deployment commands, and runtime state;
- private network targets, routes, egress allowlists, and connectivity evidence;
- incident records, migration plans, and environment-specific rollout history.

The public repository may contain only generic, credential-free examples. It
must be possible for an adopter to create an independent deployment without
learning anything about another deployment.

The tracked `config/fleets/manifest.toml` and
`config/fleets/example/runners.toml` files are those public examples. They are
not activation records.

## Example profile

The example below uses synthetic names. It illustrates an organization-scoped
default class and a larger class; it is not an activated configuration.

```toml
[github]
org = "example-org"
runner_group = "daytona"

[daytona]
api_base = "https://app.daytona.io/api"
target = "example-target"

[cache]
volume = "example-marsh-cache"

[[size_class]]
name = "default"
labels = ["self-hosted", "daytona", "marsh"]
snapshot = "marsh-runner-default-example"
cpu = 2
memory_gib = 4
disk_gib = 10
min_idle = 0
max = 10

[[size_class]]
name = "large"
labels = ["self-hosted", "daytona", "marsh", "large"]
snapshot = "marsh-runner-large-example"
cpu = 4
memory_gib = 8
disk_gib = 10
min_idle = 0
max = 4
```

Use an organization-scoped profile only when every selected repository belongs
to the same reviewed trust boundary. Use a repository-scoped profile for a
small, explicit private-repository allowlist. The controller should mint a
JIT registration only for the scope declared by that profile.

## Operating a fleet

Maintain the activated profile in the private operations source and review
changes there before deployment. Each activation should establish all of the
following:

1. The GitHub App has only the repository and runner-administration access the
   profile requires.
2. The runner group is private and selected only for reviewed repositories.
3. The snapshot name, resource shape, and registry source are immutable and
   recorded for rollback.
4. Secrets are injected at runtime and are absent from the profile and image.
5. A no-secret canary proves queue pickup, one-job execution, and teardown.
6. The private operations record captures the deployed revision and recovery
   procedure without publishing host or network detail.

Do not copy an active profile back into this repository for convenience. If a
new feature needs an example, create a fresh synthetic example instead.

## Network-restricted workloads

A workload that requires a non-public network needs its own private,
least-privilege profile. Keep its routes, endpoint list, firewall rules,
repository roster, and positive/negative connectivity evidence in the private
operations source. Give the workload a dedicated runner group and label that
cannot be selected by ordinary jobs.

Never extend a standard public runner label to reach private infrastructure.
That changes the trust boundary for every workflow that can select the label.

## Snapshot updates

Treat a snapshot change as a production change even when the profile shape is
unchanged:

1. Register a new immutable snapshot name in the private operations source.
2. Verify the image digest and declared resource shape.
3. Run a no-secret canary for each affected size class.
4. Promote the profile only after successful pickup, job completion, and
   teardown evidence.
5. Retain the prior known-good snapshot until the rollback and retention gates
   in [SNAPSHOT-GOVERNANCE.md](SNAPSHOT-GOVERNANCE.md) pass.

## Verification

Validate the public profile contract and documentation with:

```bash
make verify
```
