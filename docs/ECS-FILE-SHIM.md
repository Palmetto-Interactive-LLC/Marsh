# Running the orchestrator on ECS: file shim and image build

The orchestrator reads two files at start: the fleet profile
(`MARSH_RUNNER_CONFIG`, default `/etc/marsh/runners.toml`) and the GitHub App
private key (`GH_APP_KEY_PATH`). Kubernetes mounts both from a ConfigMap and a
Secret. ECS on EC2 can't mount either as a file, but it can inject environment
variables. `orchestrator/ecs_entrypoint.py` bridges the two.

## What the shim does

| Variable | Kind | Result |
|---|---|---|
| `MARSH_RUNNERS_TOML` | plain task-definition env (not a secret) | validated as TOML, written to the config path |
| `MARSH_GITHUB_APP_PEM` | task-definition `secrets` (Secrets Manager) | checked for a PEM header, written to the key path |

Both files are 0600 in a 0700 directory, written through a temp file and an
atomic rename. The shim then removes both variables from the environment and
`exec`s the orchestrator with `MARSH_RUNNER_CONFIG` and `GH_APP_KEY_PATH`
pointing at the written files. Nothing it logs contains a value. If neither
variable is set it only execs, so Kubernetes keeps working unchanged.

Target paths: the task definition's own `MARSH_RUNNER_CONFIG` / `GH_APP_KEY_PATH`
when set, otherwise `$MARSH_SHIM_DIR` (default `/tmp/marsh-config`). The
directory has to be writable. Give it a tmpfs so the key never touches disk:

```
linuxParameters.tmpfs = [
  { containerPath = "/tmp", size = 64, mountOptions = ["uid=1000", "gid=1000", "mode=0700"] },
  { containerPath = "/run", size = 8,  mountOptions = ["uid=1000", "gid=1000", "mode=0755"] },
]
```

The `/run` mount matters too. The orchestrator creates `/run/marsh` itself and
refuses a directory its own uid doesn't own, so the tmpfs has to be owned by
uid 1000 (the same reason the EKS manifests mount an emptyDir at `/run`, not
`/run/marsh`). Run the container as uid 1000 with a read-only root filesystem.

## Using it in the image

The Dockerfile that builds the deployed image lives in Marsh-ops
(`orchestrator/Dockerfile`), not here. Two lines there:

```
COPY orchestrator/ecs_entrypoint.py /app/ecs_entrypoint.py
ENTRYPOINT ["python", "/app/ecs_entrypoint.py"]
```

With no arguments the shim runs `python /app/orchestrator.py`, which is today's
entrypoint, so the image stays a drop-in for EKS. The shim is dependency-free
and needs Python 3.11 or newer (`tomllib`), and the image is 3.12.

## Image build and tag today

Nothing in CI builds the orchestrator image. The deployed tag
(`marsh/orchestrator:a3dc5c5` in ECR account 490600801675) is a Marsh-ops commit
SHA, built by hand with `docker buildx --platform linux/arm64` per the comment in
Marsh-ops' Dockerfile. Marsh-ops' `deploy.yml` can build and push, but it runs on
`[self-hosted, daytona]`, which is amd64 only, and it tags with the SHA of
whatever repo it runs in and only when the ECR variables are set.

One thing to settle before picking a tag: the orchestrator source in Marsh-ops
and the one in this repo have drifted apart (`orchestrator.py`, `watchdog.py`,
`requirements.txt` all differ, and `gpu_pool.py` exists only in Marsh-ops). The
image is built from the Marsh-ops tree, so the honest tag today is the Marsh-ops
SHA. A Marsh-SHA tag only means something once the two trees are reconciled and
the Dockerfile moves here.

## Simplest CI path

Reuse the pattern `oswaldcooke.com` uses: a CodeBuild ARM runner project for the
repo, with the workflow authenticating to ECR through the repo's own OIDC
publisher role.

1. pi-platform: add `pi-marsh-ops` to `local.codebuild_runners` in
   `opentofu/live/prod/main.tf` (repo URL
   `https://github.com/Palmetto-Interactive-LLC/Marsh-ops`, `BUILD_GENERAL1_SMALL`)
   and an OIDC publisher role scoped to the `marsh/orchestrator` ECR repository.
2. Marsh-ops: one workflow, on push to `main` touching `orchestrator/**`.

```yaml
name: orchestrator-image
on:
  push:
    branches: [main]
    paths: ["orchestrator/**"]
  workflow_dispatch:
permissions: {}
jobs:
  build:
    runs-on: codebuild-pi-platform-runner-pi-marsh-ops-${{ github.run_id }}-${{ github.run_attempt }}
    permissions: { contents: read, id-token: write }
    steps:
      - uses: actions/checkout@<pinned>
        with: { persist-credentials: false }
      - uses: aws-actions/configure-aws-credentials@<pinned>
        with: { role-to-assume: "${{ vars.AWS_DEPLOY_ROLE_ARN }}", aws-region: us-east-1 }
      - uses: aws-actions/amazon-ecr-login@<pinned>
        id: ecr
      - run: |
          set -euo pipefail
          image="${{ steps.ecr.outputs.registry }}/marsh/orchestrator:${GITHUB_SHA::7}"
          docker build -f orchestrator/Dockerfile -t "$image" .
          docker push "$image"
```

The runner is native arm64, so a plain `docker build` produces the right
architecture with no buildx or QEMU. The tag is the short SHA of the repo the
workflow runs in. The ECS task definitions then pin that tag in
`ecs-apps.tf`; promotion is a one-line change in the PR that deploys it.
Marsh-ops pins its actions by SHA, so fill in the `<pinned>` values from its
existing workflows.
