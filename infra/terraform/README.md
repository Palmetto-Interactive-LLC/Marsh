# Marsh Terraform

This directory codifies the durable parts of a Marsh runner platform:

- a GitHub Actions runner group scoped to selected repositories;
- Daytona cache volumes;
- a GHCR registry connection used by Daytona to pull the runner image;
- snapshot registration for default and large runner classes.

Ephemeral runner sandboxes are not Terraform resources. The Marsh orchestrator
creates and deletes them at runtime based on the GitHub Actions queue.

## Prerequisites

- Terraform 1.6 or newer.
- GitHub token or GitHub App token with organization runner-group administration
  rights.
- Daytona API key.
- GHCR credentials with `read:packages`; use `write:packages` too if this token
  also pushes the runner image.

## Configure

Copy the example file and edit non-secret values:

```bash
cp -f terraform.tfvars.example terraform.tfvars
```

Inject secrets with environment variables:

```bash
export TF_VAR_github_token="<github token>"
export TF_VAR_daytona_api_key="<daytona api key>"
export TF_VAR_ghcr_user="<github user or bot>"
export TF_VAR_ghcr_token="<ghcr token>"
```

Never commit `terraform.tfvars`, Terraform state, tokens, private keys, or `.env`
files.

## Apply

```bash
terraform init
terraform plan
terraform apply
```

The apply creates the runner group and volumes, then calls:

- `../snapshots/setup-registry.sh`
- `../snapshots/register-snapshot.sh <tag>`

Those scripts are intentionally idempotent wrappers around Daytona API/CLI gaps.

## Runner Labels

The default example uses:

```yaml
runs-on: [self-hosted, marsh]
```

The large runner class uses:

```yaml
runs-on: [self-hosted, marsh, large]
```

Do not allow public repositories onto a self-hosted runner group unless you have
reviewed the workflows that can run there and accepted that trust boundary.

## Rolling A New Image

Build and push a new runner image, then update:

```hcl
marsh_runner_image_tag = "v2"
```

Run `terraform plan` and `terraform apply`. The snapshot registration wrapper
will re-register the sized snapshots from the new image.
