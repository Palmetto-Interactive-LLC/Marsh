# Contributing

## Issues And Linear

Use GitHub issues for external intake that is safe to share in the repository.
The supported human-facing types are **Bug**, **Feature**, **Epic**, and
**Issue**; see [docs/ISSUE-TRACKING.md](docs/ISSUE-TRACKING.md) for the exact
GitHub label and Linear work-item guidance.

Linear is the durable implementation tracker. When a GitHub issue becomes
implementation work, create or update the matching Linear issue or project and
link it from the pull request.

Never place vulnerability details, secrets, customer data, or private incident
notes in public GitHub issues.

## Pull Requests

1. Start from a Linear issue, Linear project, or GitHub intake issue.
2. Create a branch from `main`.
3. Use signed commits.
4. Keep history linear and changes focused.
5. Run the relevant tests, linters, builds, or document why the change is
   docs-only.
6. Complete the pull request template.
7. Wait for required checks and the configured AI reviewer.

`CODEOWNERS` documents ownership only. It is not intended to be a required
approval gate in the default no-human-review setup.

## Verification

Run the local gate before opening a pull request:

```bash
make verify
```

For release or security-sensitive changes, also run:

```bash
gitleaks detect --no-git --redact --source .
```

Runner image, orchestrator, cache, Terraform, and workflow changes should be
reviewed for least privilege, ephemeral teardown, secret handling, and public
pull-request safety.

Use GitHub issues for user-facing intake and Linear for work that must survive
handoff between local sessions.
