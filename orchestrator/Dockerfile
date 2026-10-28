# syntax=docker/dockerfile:1
# Marsh orchestrator image. Built for linux/arm64 (buildx + QEMU on the Marsh
# runners) and run on Graviton ECS hosts. Config, secrets and the GitHub App key
# arrive at run time (MARSH_RUNNER_CONFIG, DAYTONA_API_KEY, GH_APP_*); none are baked in.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY orchestrator/requirements.txt ./requirements.txt
RUN python -m pip install --requirement requirements.txt

COPY orchestrator/orchestrator.py orchestrator/watchdog.py ./

RUN useradd --system --uid 10001 --no-create-home marsh
USER 10001

CMD ["python", "orchestrator.py"]
