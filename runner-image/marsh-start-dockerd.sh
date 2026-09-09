#!/usr/bin/env bash
# OPP-524: the exact, sole command the `daytona` user may run as root via sudo
# (see the sudoers drop-in in runner-image/Dockerfile). Replaces the former
# blanket-sudo `sh -c "nohup dockerd ... &"` one-liner: a fixed root-owned
# script with a fixed argv is something sudoers can pin by exact path, whereas
# an inline `sh -c` string is not a meaningful sudo restriction (any command
# text is technically "the same command"). This script is intentionally the
# ONLY thing the runner image's sudoers rule allows daytona to run as root.
#
# It never widens /var/run/docker.sock beyond dockerd's own default mode
# (0660, group=docker); the daytona user already has docker-group membership,
# so a world-writable (0666) socket was never required.
set -euo pipefail

nohup dockerd >/var/log/dockerd.log 2>&1 &
disown
