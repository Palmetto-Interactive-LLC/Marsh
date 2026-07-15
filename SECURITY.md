# Security Policy

Marsh provisions self-hosted GitHub Actions runners. Treat every change to runner
registration, image contents, cache handling, credentials, workflow permissions,
and teardown logic as security-sensitive.

## Reporting A Vulnerability

Please do not open a public GitHub issue for suspected vulnerabilities.

Use GitHub private vulnerability reporting for this repository. If that is not
available, contact the maintainers privately. Include affected versions,
reproduction steps, impact, and whether runner credentials, repository tokens,
or sandbox isolation may be exposed.

## Supported Versions

The default branch and the latest GitHub Release are supported.

| Version | Supported |
| --- | --- |
| `main` | Yes |
| latest release | Yes |

## Baseline Expectations

- Do not bake GitHub, Daytona, registry, cloud, model, or SSH credentials into
  the runner image.
- Do not commit populated `.env` files, Terraform state, private keys, tokens,
  or generated credentials.
- Keep public pull-request workflows on GitHub-hosted runners unless you have
  explicitly reviewed the self-hosted runner trust boundary.
- Use GitHub App JIT runner registration where possible.
- Keep sandboxes ephemeral: one runner, one job, teardown.
- Keep workflow permissions minimal and pin third-party actions by commit SHA.
- Keep cache writes whole-file and non-authoritative; caches must never be a
  source of trusted code or secrets.

## Coordinating Fixes

Security work may be tracked privately in GitHub Security Advisories. Public
issues are appropriate for non-sensitive bugs and feature requests, but do not
copy exploit details, secrets, customer data, or private advisory content into
public issues.
