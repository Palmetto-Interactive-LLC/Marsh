# Secure GitHub Team Setup

This runbook turns a new repository created from this template into a GitHub
Team-compatible secure application repo. It assumes `main` is the default
branch. It intentionally contains no activated deployment target; adopters
own their deployment configuration in private operations source.

## 1. Create The Repository

If you own the source template repository, first mark it as a template in
GitHub repository settings. The settings script intentionally does not flip
`is_template` because it changes the source repository, not each generated
repository; an admin can set it with:

```bash
gh api --method PATCH /repos/OWNER/REPO -f is_template=true
```

1. In GitHub, choose **Use this template** and create the new private or
   internal repository.
2. Keep `main` as the default branch.
3. Clone the new repository.
4. Confirm the remote points at the generated repository, not this template:

   ```bash
   git remote -v
   ```

5. Add the first application files and push only after the setup below is
   complete enough for a trivial pull request.

## 2. Configure Commit Signing Before Rulesets

Do this before enabling a branch ruleset that requires signed commits.

1. Configure GPG, SSH, or S/MIME signing for the maintainer account.
2. Enable signing locally:

   ```bash
   git config commit.gpgsign true
   git config tag.gpgsign true
   ```

3. Make a small signed commit on a throwaway branch and push it.
4. Verify GitHub shows the commit as **Verified**.
5. Only then enable signed-commit enforcement on `main`.

If signed commits are required before the maintainer can produce a verified
commit, the first ruleset rollout will block normal maintenance.

## 3. Bootstrap Repository Settings

After the repository exists and signing works, review the exact target and run
the bootstrap script from the generated repository:

```bash
./scripts/bootstrap-repo.sh OWNER REPO
# or
./scripts/bootstrap-repo.sh OWNER/REPO
```

The bootstrap is expected to configure repository settings, environments,
rulesets, Dependabot, immutable releases, issue labels, and merge settings. It
writes real repository settings, so confirm the owner/repo before running it.

If the script is not present in the generated repository yet, apply the same
settings manually from this runbook and `docs/SECURITY-MODEL.md`.

## 4. Choose An AI Reviewer Path

This template is designed for an automated-review model: status checks and AI
review provide the normal merge signal, while `CODEOWNERS` documents ownership
without becoming a required approval gate.

### Path A: Codex Automatic Review

1. Set up Codex cloud for the repository.
2. Open Codex code review settings:
   <https://chatgpt.com/codex/settings/code-review>
3. Enable **Code review** for the repository.
4. Enable **Automatic reviews** so Codex reviews newly opened pull requests.
5. Keep repository review guidance in public contributor documentation or the
   pull-request template.
6. Verify manual review still works by commenting `@codex review` on the
   trivial pull request in step 8.

### Path B: Claude Workflow, OAuth, WIF, Or OpenAI Alternative

Use this path only when Codex automatic review is not available for the repo.
The committed `.github/workflows/ai-review.yml` is advisory and fork-safe.

1. Set repository variable `AI_REVIEW_PROVIDER=claude` to opt into the
   committed workflow. Without that variable, the workflow skips so Path A can
   be the no-secret default.
2. Default within Path B: run `claude setup-token` locally and store the resulting
   subscription OAuth token as repository secret `CLAUDE_CODE_OAUTH_TOKEN`.
3. More secure alternative: configure Anthropic Workload Identity Federation,
   remove the OAuth token line from the workflow, add job permission
   `id-token: write`, and set the Anthropic organization and federation rule
   inputs documented in the workflow.
4. OpenAI alternative: use the commented `openai/codex-action` path with
   `OPENAI_API_KEY` only if the repository owner explicitly opts into
   API-key billing or has an already-owned self-hosted Codex path.
5. Keep the AI review job out of required status checks by default. The gate is
   required conversation-thread resolution plus the required CI/security checks.

## 5. Configure OIDC For Deploys

Use OIDC for cloud deploy credentials. Do not store static AWS access keys in
GitHub secrets. Prefer Terraform in `infra/terraform/`; use
`scripts/aws-oidc-setup.sh` only as a quickstart.

### Terraform Pattern

Create an IAM OIDC provider for `https://token.actions.githubusercontent.com`
and one deploy role per environment. Scope each role trust policy to the
repository and environment:

```hcl
condition {
  test     = "StringEquals"
  variable = "token.actions.githubusercontent.com:aud"
  values   = ["sts.amazonaws.com"]
}

condition {
  test     = "StringLike"
  variable = "token.actions.githubusercontent.com:sub"
  values = [
    "repo:OWNER/REPO:environment:staging",
    "repo:OWNER/REPO:environment:production"
  ]
}
```

Give each role only the permissions required by its target:

- `eks`: assume role, describe cluster, and deploy to the allowed namespace.
- `aws`: deploy the named AWS service or stack only.
- `self-hosted`: retrieve deployment metadata only, then hand off to the
  self-hosted runner boundary.

### Shell Pattern

For a shell setup, create the same OIDC provider and roles with AWS CLI:

```bash
./scripts/aws-oidc-setup.sh \
  --account-id ACCOUNT_ID \
  --region AWS_REGION \
  --github-org OWNER \
  --github-repo REPO \
  --staging-role-name REPO-staging-deploy \
  --production-role-name REPO-production-deploy \
  --target eks \
  --cluster-name EKS_CLUSTER_NAME \
  --staging-namespaces app-staging \
  --production-namespaces app-production \
  --enable-eks-access
```

Keep staging and production roles separate even when they deploy the same
artifact type.

## 6. Set Variables And Secrets

Use repository variables for defaults and environment-level variables for
environment-specific deploy configuration:

- `AI_REVIEW_PROVIDER`, optional; set to `claude` only when using Path B.
- `DEPLOY_TARGET_TYPE`: `eks`, `aws`, or `self-hosted` as the default target.
- `AWS_REGION`
- `AWS_DEPLOY_ROLE_ARN`
- `AWS_EKS_CLUSTER_NAME`
- `AWS_EKS_NAMESPACE` or `K8S_NAMESPACE`
- `AWS_ECR_REGISTRY`
- `AWS_ECR_REPOSITORY`

Use secrets only for values that cannot use OIDC or OAuth:

- `CLAUDE_CODE_OAUTH_TOKEN`, only for the committed Claude review workflow.
- `OPENAI_API_KEY`, only for the explicitly opted-in OpenAI review alternative.
- Third-party deploy tokens that do not support OIDC.

Do not create `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` for deploys.

## 7. Enable Team-Compatible Gates

Configure branch and tag gates that work on GitHub Team:

1. Require pull requests before merging to `main`.
2. Require signed commits on `main`.
3. Require linear history.
4. Require the selected status checks.
5. Keep deployment targets and production environment policies in private
   operations source. Marsh source releases use signed, annotated CalVer tags
   without a `v` prefix.
6. Do not require code-owner review unless the repo intentionally moves away
   from the no-human-review model.

Optional break-glass: add a narrow bypass actor to the ruleset, such as a
single admin team or GitHub App. Keep it disabled by default where possible,
document every use in the incident or release record, and remove temporary
bypass actors immediately after use.

## 8. Run A Trivial Pull Request Check

1. Create a branch with a harmless docs-only change.
2. Push the branch and open a pull request.
3. Confirm the PR shows a verified signed commit.
4. Confirm required checks run and block merge while failing or pending.
5. Confirm the chosen AI reviewer posts or reports a review.
6. Merge through the normal path.
7. Confirm the push to `main` runs the required checks and does not deploy an
   adopter fleet.

## 9. Verify The Source-Release Path

1. Confirm immutable releases are enabled:

   ```bash
   gh api /repos/OWNER/REPO/immutable-releases
   ```

2. Create and publish a GitHub Release from a signed, annotated CalVer tag such
   as `2026.7.2`. Do not use a `v` prefix.
3. Confirm the release points to the verified `main` commit and contains no
   deployment-specific information.
4. Confirm a push to `main` does not deploy an adopter fleet.
5. If an adopter later adds a break-glass deployment workflow, keep it in the
   private operations source and require a selected existing release tag and an
   incident reason.

## 10. Known Gaps On GitHub Team

This template uses Team-compatible controls where possible. It does not claim
to replace the following paid or Enterprise controls:

- GHAS Secret Protection: push protection and private-repo secret scanning are
  stronger with Secret Protection enabled.
- GHAS Code Security: native code scanning management and some advanced
  security views require Code Security.
- Enterprise environment reviewers and wait timers: this template relies on
  immutable releases and tag environment policy gates instead of human
  environment approvals or timed delays.
- Public-only artifact attestations: do not rely on private-repo attestations
  unless your plan and repository visibility support them.
