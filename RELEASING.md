# Releasing Marsh

Marsh uses Calendar Versioning (CalVer): `YYYY.M.PATCH`.

Examples:

- `2026.7.0`
- `2026.7.1`
- `2026.8.0`

## Before Tagging

Run the local verification gate:

```bash
make verify
```

Run the public-release exposure scan:

```bash
gitleaks detect --no-git --redact --source .
```

Confirm the release branch contains no populated `.env` files, Terraform state,
private keys, access tokens, internal-only operational material, local-machine
paths, activated fleet profiles, downstream repository rosters, host inventory,
or private-network topology. The release source must be reproducible from
generic examples alone; retain live deployment material in a private operations
source.

## Cut A Release

1. Update [CHANGELOG.md](CHANGELOG.md).
2. Commit the release notes.
3. Create a signed, annotated CalVer tag. Marsh release tags do **not** use a
   `v` prefix:

   ```bash
   git tag -s 2026.7.2 -m "Marsh 2026.7.2"
   git push origin 2026.7.2
   ```

4. Create a GitHub Release from the tag. This source release does not itself
   deploy a running fleet.
5. Attach any built runner-image artifacts only if they are reproducible from
   the committed Dockerfile and scripts.

## Verify

After publishing:

```bash
gh release view 2026.7.2
gh repo view OWNER/REPO --json visibility,licenseInfo
```

Confirm:

- repository visibility is public;
- license is MIT;
- secret scanning and push protection are enabled;
- private vulnerability reporting is enabled;
- CI and security workflows are green on the release commit.
- the current-tree public-boundary review contains only generic examples.
