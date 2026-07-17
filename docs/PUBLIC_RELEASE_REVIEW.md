# Public release review

Run this review against the exact commit proposed for a Marsh open-source
release. The review protects two boundaries: no credentials or private data,
and no live operational topology embedded in a reusable public project.

## Required evidence

Run the following commands from a clean checkout of the proposed release
commit:

```bash
gitleaks detect --no-git --redact --source .
make verify
git diff --check
```

Record the release tag, commit SHA, command results, and any unavailable local
tool in the release record. Do not attach environment files, provider
responses, host logs, or raw scan inputs.

## Public-boundary review

Confirm all of the following before tagging:

- The repository contains no populated `.env` file, access token, private key,
  Terraform state, credential reference, or local-machine path.
- Fleet profiles, manifests, and documentation are synthetic examples only.
- No active repository roster, runner-group identifier, GitHub App binding,
  host inventory, deployment record, or secret-manager mapping is published.
- No private endpoint, route, egress policy, firewall rule, or network
  connectivity evidence is published.
- Operational migration, cohort-control, and incident-recovery procedures are
  retained only in the private operations source.
- Public instructions are reproducible by an independent adopter using generic
  names and their own credentials.
- Public CI runs on GitHub-hosted runners or another reviewed public boundary;
  untrusted pull requests never receive access to a private runner group.
- The repository has an MIT license and current community and security files.

## Release decision record

Use this concise record in the GitHub Release or private delivery log:

```text
Release: 2026.7.3
Commit: <verified-main-commit>
Verification: <attach gitleaks, make verify, and git diff --check results>
Public-boundary review: <pass/fail with evidence>
Signed annotated tag: <verified>
```

Populate the record only after attaching evidence for the exact release commit.
Do not use it to list an adopter's environments, networks, hosts, or downstream
repositories.

## Verification

The final public gate is:

```bash
make verify
```
