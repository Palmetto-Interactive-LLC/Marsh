# syntax=docker/dockerfile:1
# Marsh runner - universal GitHub Actions self-hosted runner for Daytona sandboxes.
#
# Base (daytonaio/sandbox) already ships git, gh, node 20, claude-code — do NOT
# reinstall them. We add: the GitHub Actions runner (core), go + rust + sccache,
# docker/buildx, a pre-seeded RUNNER_TOOL_CACHE (fixes setup-python on Debian —
# setup-python on Debian), the scanners our workflows run (best-effort), and the cache
# job-hooks. Cache-heavy mutable state lives on a Daytona Volume via the hooks
# (hooks/cache-*.sh), NOT baked here.
#
# Registered as two sized snapshots off this one image:
#   daytona snapshot create marsh-runner-default -f runner-image/Dockerfile -c . --cpu 2 --memory 4 --disk 10
#   daytona snapshot create marsh-runner-large   -f runner-image/Dockerfile -c . --cpu 4 --memory 8 --disk 10
FROM daytonaio/sandbox:0.8.0

USER root
ARG DEBIAN_FRONTEND=noninteractive
ARG GO_VERSION=1.23.5

# Core CI deps (must succeed on any build host).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl jq git unzip zip tar xz-utils build-essential \
      python3 python3-venv python3-pip pkg-config libssl-dev \
 && rm -rf /var/lib/apt/lists/*
# Docker (docker-ce + cli + containerd + buildx) from Docker's official apt repo.
# Daytona sandboxes support Docker-in-Docker (verified: dockerd starts, `docker run`
# works); the daemon is launched at runner boot by run-ephemeral.sh. This gives the
# runner FULL parity — `docker build`/buildx and container-based actions (e.g. OSSF
# scorecard, which runs as a Docker action) work. daytona user joins the docker group.
# Explicit packages (NOT the get.docker.com convenience script, which also pulls
# docker-model-plugin and trips dpkg on this base).
RUN install -m0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
 && usermod -aG docker daytona \
 && rm -rf /var/lib/apt/lists/*
# shellcheck (also a scanner) — best-effort, not in every base's apt repo.
RUN apt-get update && apt-get install -y --no-install-recommends shellcheck 2>/dev/null || true; rm -rf /var/lib/apt/lists/* || true

# ── gh CLI (base 0.8.0 does NOT ship it; PINNED for reproducible builds — no
#    unauthenticated GitHub API calls during build, which rate-limit per host) ──
ARG GH_VERSION=2.95.0
RUN curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" | tar -xz -C /tmp \
 && install -m0755 "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh && rm -rf /tmp/gh_*

# ── GitHub Actions runner (CORE; PINNED) ────────────────────────────────────
ARG RUNNER_VERSION=2.335.1
RUN mkdir -p /opt/actions-runner && cd /opt/actions-runner \
 && curl -fsSL "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" | tar -xz \
 && ./bin/installdependencies.sh \
 && chown -R daytona:daytona /opt/actions-runner

# ── Go (stable URL) ─────────────────────────────────────────────────────────
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" | tar -xz -C /usr/local \
 && ln -sf /usr/local/go/bin/go /usr/local/bin/go && ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt

# ── Rust + sccache (for the daytona user; on PATH at runtime) ────────────────
# Ship the latest STABLE, not a hard pin: a stale pin silently breaks builds when
# deps raise their minimum rustc (1.85.0 broke on crates needing 1.88+). rustup is
# present, so any repo needing a fixed toolchain drops a rust-toolchain.toml and it
# auto-installs at job time. This mirrors ubuntu-latest (tracks recent stable).
# /home/daytona/.local/bin FIRST: security jobs `pip install --user` PINNED scanners
# (semgrep==, checkov==) there and invoke them by bare name. Without this, a baked
# newer copy in /usr/local/bin shadowed the pinned one and changed scan results
# (sast passed on main but failed here). This mirrors ubuntu-latest's PATH.
ENV RUSTUP_HOME=/opt/rust/rustup CARGO_HOME=/opt/rust/cargo PATH=/home/daytona/.local/bin:/opt/rust/cargo/bin:/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin
RUN mkdir -p /opt/rust && chown -R daytona:daytona /opt/rust
USER daytona
RUN curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable \
 && rustup component add clippy rustfmt
USER root
# sccache prebuilt binary, PINNED (no API call → no per-host rate-limit).
ARG SCCACHE_VERSION=v0.8.2
RUN ( curl -fsSL "https://github.com/mozilla/sccache/releases/download/${SCCACHE_VERSION}/sccache-${SCCACHE_VERSION}-x86_64-unknown-linux-musl.tar.gz" | tar -xz -C /tmp \
      && install -m0755 /tmp/sccache-*/sccache /usr/local/bin/sccache ) || echo "warn: sccache"

# ── Scanners, PINNED (no API calls; best-effort so a bad URL never fails build) ─
ARG ACTIONLINT_VERSION=1.7.7
ARG GITLEAKS_VERSION=8.24.3
ARG TRIVY_VERSION=0.72.0
RUN set -x; \
  ( curl -fsSL "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" | tar -xz -C /usr/local/bin actionlint ) || echo "warn: actionlint"; \
  ( curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" | tar -xz -C /usr/local/bin gitleaks ) || echo "warn: gitleaks"; \
  ( curl -fsSL "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" | tar -xz -C /usr/local/bin trivy ) || echo "warn: trivy"
# NOTE: semgrep/checkov/zizmor are deliberately NOT baked. The security workflows
# `pip install --user` their own PINNED versions (semgrep==1.168.0, checkov==3.3.2,
# zizmor==…) and call them by bare name; a baked newer copy would shadow those and
# change results. ~/.local/bin is first on PATH (above) so the pinned copies win.

# ── Pre-seed RUNNER_TOOL_CACHE (best-effort; fixes setup-python on Debian) ───
ENV RUNNER_TOOL_CACHE=/opt/hostedtoolcache
RUN mkdir -p "$RUNNER_TOOL_CACHE" && chown -R daytona:daytona "$RUNNER_TOOL_CACHE"
COPY runner-image/seed-toolcache.sh /usr/local/bin/seed-toolcache.sh
# Mount the GH token (BuildKit secret) so the python-build-standalone API lookup is
# authenticated (unauth API rate-limits per build host). Secret is NOT baked into
# any layer. Build with: --secret id=ghtoken,src=<token-file> (optional).
RUN --mount=type=secret,id=ghtoken chmod 0755 /usr/local/bin/seed-toolcache.sh \
 && (/usr/local/bin/seed-toolcache.sh || echo "warn: toolcache seed")

# ── Runner entrypoint + transparent cache hooks ─────────────────────────────
COPY runner-image/run-ephemeral.sh /usr/local/bin/run-ephemeral.sh
COPY runner-image/hooks/ /opt/hooks/
RUN chmod 0755 /usr/local/bin/run-ephemeral.sh /opt/hooks/*.sh

# Sanity: essentials only (soft tools may be absent on a bad upstream day).
RUN test -f /opt/actions-runner/run.sh && git --version && node --version && go version && gh --version

WORKDIR /home/daytona
USER daytona
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD test -f /opt/actions-runner/run.sh || exit 1
