SHELL := /usr/bin/env bash

# `make ci` is the one command CI runs (Depot CI and a laptop alike).
GITLEAKS_VERSION ?= 8.30.1
GITLEAKS ?= $(or $(shell command -v gitleaks),.cache/gitleaks/gitleaks)
TRIVY_VERSION ?= 0.71.2
SEMGREP_VERSION ?= 1.168.0
CHECKOV_VERSION ?= 3.3.2

.PHONY: ci secrets security lint scan bootstrap oidc verify

ci: verify secrets

# Scans the full git history. Downloads a pinned, checksum-verified gitleaks when none is installed.
secrets:
	@if [ ! -x "$(GITLEAKS)" ] && ! command -v gitleaks >/dev/null; then \
	  set -e; arch=$$(uname -m | sed 's/aarch64/arm64/;s/x86_64/x64/'); os=$$(uname -s | tr A-Z a-z); \
	  art=gitleaks_$(GITLEAKS_VERSION)_$${os}_$${arch}.tar.gz; \
	  base=https://github.com/gitleaks/gitleaks/releases/download/v$(GITLEAKS_VERSION); \
	  mkdir -p .cache/gitleaks; \
	  curl -fsSLo .cache/gitleaks/$$art $$base/$$art; \
	  curl -fsSLo .cache/gitleaks/sums.txt $$base/gitleaks_$(GITLEAKS_VERSION)_checksums.txt; \
	  (cd .cache/gitleaks && grep "  $$art$$" sums.txt | sha256sum -c -); \
	  tar -xzf .cache/gitleaks/$$art -C .cache/gitleaks gitleaks; \
	fi
	$(GITLEAKS) detect --source . --config=.gitleaks.toml --redact --no-banner

# Static analysis run on main (Depot CI security workflow): Semgrep, Trivy, Checkov at pinned versions.
security:
	python3 -m pip install --user "semgrep==$(SEMGREP_VERSION)" "checkov==$(CHECKOV_VERSION)"
	@set -e; mkdir -p .cache/trivy; \
	art=trivy_$(TRIVY_VERSION)_Linux-64bit.tar.gz; \
	base=https://github.com/aquasecurity/trivy/releases/download/v$(TRIVY_VERSION); \
	curl -fsSLo .cache/trivy/$$art $$base/$$art; \
	curl -fsSLo .cache/trivy/sums.txt $$base/trivy_$(TRIVY_VERSION)_checksums.txt; \
	(cd .cache/trivy && grep "  $$art$$" sums.txt | sha256sum -c -); \
	tar -xzf .cache/trivy/$$art -C .cache/trivy trivy
	PATH="$$HOME/.local/bin:$$PATH" semgrep scan --config p/ci --config p/security-audit --metrics=off --error --exclude .git .
	.cache/trivy/trivy fs --scanners vuln,secret,misconfig --severity HIGH,CRITICAL --exit-code 1 --skip-dirs .git --skip-dirs .cache .
	PATH="$$HOME/.local/bin:$$PATH" checkov -d . --quiet --compact --skip-path .git --skip-path .cache

lint:
	./scripts/verify.sh

scan:
	gitleaks detect --source=. --config=.gitleaks.toml --redact --no-banner
	semgrep scan --config=p/owasp-top-ten --config=p/secrets --error
	trivy fs --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed .
	checkov --config-file .checkov.yaml --directory .

bootstrap:
	@test -n "$(OWNER)" || (echo "OWNER is required, for example make bootstrap OWNER=acme REPO=service" >&2; exit 2)
	@test -n "$(REPO)" || (echo "REPO is required, for example make bootstrap OWNER=acme REPO=service" >&2; exit 2)
	./scripts/bootstrap-repo.sh "$(OWNER)" "$(REPO)"

oidc:
	./scripts/aws-oidc-setup.sh

verify:
	./scripts/verify.sh
