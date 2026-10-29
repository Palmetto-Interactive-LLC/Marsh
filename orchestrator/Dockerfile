# syntax=docker/dockerfile:1
# Marsh orchestrator image. Built for linux/arm64 (native buildx on the org
# CodeBuild ARM runner, no QEMU) and run on Graviton ECS hosts. Config, secrets and the GitHub App key
# arrive at run time (MARSH_RUNNER_CONFIG, DAYTONA_API_KEY, GH_APP_*); none are baked in.
FROM 490600801675.dkr.ecr.us-east-1.amazonaws.com/mirror/python:3.12-slim-bookworm-20261028-e697c818@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY orchestrator/requirements.txt ./requirements.txt
RUN python -m pip install --requirement requirements.txt

COPY orchestrator/orchestrator.py orchestrator/watchdog.py ./

# The orchestrator mkdirs /run/marsh and refuses a directory its uid does not
# own, and /run is root-owned, so the image creates it for the service user
# (as Marsh-ops does). The HEALTHCHECK below reads the status file there.
RUN useradd --system --uid 10001 --no-create-home marsh \
    && install -d -o 10001 -g 10001 -m 0755 /run/marsh
USER 10001

# Liveness: the service loop rewrites /run/marsh/<fleet>.json every poll
# interval (write_runtime_status, about 20 s), so a status file older than five
# minutes means the loop is stuck. The orchestrator has no always-on HTTP
# endpoint: its /healthz belongs to the optional webhook listener, which the
# fleets leave off.
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python", "-c", "import glob, os, sys, time; f = glob.glob('/run/marsh/*.json'); sys.exit(0 if f and time.time() - max(map(os.path.getmtime, f)) < 300 else 1)"]

CMD ["python", "orchestrator.py"]
