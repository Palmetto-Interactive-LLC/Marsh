# Security Model

This template uses automated controls, signed history, least-privilege deploy
credentials, and AI review. It does not require human code-owner approval by
default.

| Control | Mechanism | Enforcement point | Blocks merge/deploy | Residual gap |
| --- | --- | --- | --- | --- |
| Branch protection | Repository ruleset `protect-main` requiring PRs, checks, signed commits, resolved threads, and linear history | GitHub rulesets | Blocks merge | `bypass_actors` is empty by default; adding break-glass actors weakens this control. |
| No-human-review model | Required status checks plus Codex automatic review by default; optional `ai-review.yml` Claude workflow when `AI_REVIEW_PROVIDER=claude`; `CODEOWNERS` is advisory | PR checks and review-thread resolution | Checks block merge; AI comments become a soft gate through required thread resolution | AI review can miss context that a domain expert would catch. |
| Signed commits | GitHub verified signatures required on `main` | Branch ruleset | Blocks merge | Does not prove code quality or intent. |
| Linear history | Required linear history plus squash-only repository merge settings | Branch ruleset/repository settings | Blocks merge | Bad changes can still land as a single clean commit. |
| Status checks | Required test, lint, build, security, and review checks | Branch ruleset | Blocks merge | Missing checks cannot protect untested surfaces. |
| Secrets | `gitleaks` in CI plus pre-commit hook | Required `secrets-scan` check and local hook | Blocks merge through required status check | Native private-repo push protection needs GHAS Secret Protection. |
| SAST | Semgrep OSS rulesets in CI; CodeQL is public-repo-only | Required `sast` check | Blocks merge | No private-repo Code Scanning Security tab without GHAS Code Security. |
| SCA/container | Dependabot, Trivy filesystem/image scan, OSV Scanner, and Grype | Dependabot PRs plus required `deps-scan`/`iac-scan` checks | Blocks merge through required checks | Coverage depends on package metadata and scanner databases. |
| IaC | Checkov plus Trivy misconfiguration scan | Required `iac-scan` check | Blocks merge | Cloud runtime drift can bypass repository scans. |
| Workflow hardening | Minimal workflow permissions and no untrusted write-token use on pull requests | Workflow `permissions` and event design | Blocks deploy if workflow fails | Workflow bugs can still expose data or skip validation. |
| Action pinning | Pin every third-party action to a full commit SHA with a version comment; Dependabot maintains updates | `actions-lint`, review guidance, and verify script | Blocks merge through required `actions-lint` check | SHA pins still require prompt updates when upstream security fixes land. |
| Least-privilege tokens | `GITHUB_TOKEN` scoped per workflow/job | Workflow permissions | Blocks deploy if permissions are too narrow | Overly broad permissions may go unnoticed without scanning. |
| OIDC | GitHub OIDC to cloud roles scoped by repo and environment | Cloud IAM trust policy | Blocks deploy when role assumption fails | A mis-scoped trust policy can grant more access than intended. |
| Personal repository runners | Repository-scoped profiles carry an exact owner/repository allowlist; JIT, busy checks, teardown, and sandbox reaping retain repository identity plus an exact `marsh-fleet-*` label | Marsh orchestrator + preflight + watchdog | Prevents one personal repo's runner from receiving or deleting another profile's work | App installation visibility must still be reviewed when the private-repo roster changes. |
| Environment isolation | Separate staging and production environments, vars, secrets, and cloud roles | GitHub environments and cloud IAM | Blocks deploy when env policy or IAM fails | GitHub Team lacks Enterprise reviewer/wait-timer gates for private repos. |
| Source-release gating | Signed, annotated non-prefixed CalVer tags from verified `main` commits | GitHub Releases, signed history, and release review | Blocks an unverified source release | The public project does not deploy a fleet; adopters must enforce their own private deployment gates. |
| Dependabot | Weekly updates for actions, Docker, and npm with grouped minor/patch PRs | Dependabot PRs and alerts | Does not block by itself | Maintainers must merge safe updates and handle majors deliberately. |
| Scorecard | OpenSSF Scorecard or equivalent repo posture check | CI security check | Blocks merge when required | Some recommendations are advisory or not applicable to private repos. |

## Paid Feature Gaps

This template stays inside paid GitHub Team plus free or already-owned tools.
The following controls are intentionally substituted rather than assumed:

| Paid feature not assumed | Why it is out of scope | Template substitute | Residual risk |
| --- | --- | --- | --- |
| GHAS Secret Protection for private repos | It is a separate paid product beyond Team. | `gitleaks` required check plus local pre-commit scanning. | A secret can reach the remote before CI fails; there is no native private-repo push protection. |
| GHAS Code Security for private repos | It is a separate paid product beyond Team. | Semgrep OSS, Trivy, OSV Scanner, Grype, and Checkov fail CI directly. CodeQL is kept public-repo-only. | Findings are enforced by jobs, not managed in GitHub's private-repo Security tab. |
| Enterprise environment required reviewers and wait timers | These environment protection rules are Enterprise-only for private repos. | Signed immutable source releases, protected `main`, and private adopter deployment controls. | No built-in human or timed production approval gate in the public source project. |
| Private-repo artifact attestations on Team | Artifact attestations are fenced as public-repo-only for this baseline. | Public-only attestation workflow plus SHA-pinned actions and scanner gates. | Private template consumers do not get native provenance enforcement from Team alone. |
| Paid runner hardening features | Private-repo insight/blocking modes can require paid add-ons depending on tool choice. | Workflow least privilege, action pinning, `zizmor`, `actionlint`, and optional audit-only runner hardening. | Network egress and runtime behavior are not blocked by a paid policy engine. |
