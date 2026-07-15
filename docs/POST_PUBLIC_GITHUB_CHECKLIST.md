# Post-Public GitHub Checklist

Run this immediately after the repository is made public.

## Security Features

Enable or verify:

```bash
gh api repos/OWNER/REPO \
  --jq '.security_and_analysis | {secret_scanning: .secret_scanning.status, push_protection: .secret_scanning_push_protection.status}'
```

If needed:

```bash
printf '{"security_and_analysis":{"secret_scanning":{"status":"enabled"},"secret_scanning_push_protection":{"status":"enabled"}}}' \
  | gh api -X PATCH repos/OWNER/REPO --input -
```

Enable private vulnerability reporting:

```bash
gh api -X PUT repos/OWNER/REPO/private-vulnerability-reporting
```

## Repository Metadata

Set topics:

```bash
gh api -X PUT repos/OWNER/REPO/topics \
  --field names[]="github-actions" \
  --field names[]="self-hosted-runners" \
  --field names[]="daytona" \
  --field names[]="developer-tools" \
  --field names[]="ci" \
  --field names[]="automation"
```

Set description:

```bash
gh repo edit OWNER/REPO \
  --description "Elastic GitHub Actions runners on Daytona sandboxes" \
  --homepage "https://example.com"
```

## Community Profile

Verify the public community profile has:

- README
- LICENSE
- SECURITY
- CONTRIBUTING
- CODE_OF_CONDUCT
- issue templates
- pull request template

## Workflow Health

Confirm the first public push or pull request runs on GitHub-hosted runners.
Do not allow public repositories onto a Marsh self-hosted runner group until the
runner trust boundary has been reviewed.
