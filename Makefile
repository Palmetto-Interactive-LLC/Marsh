SHELL := /usr/bin/env bash

.PHONY: lint scan bootstrap oidc verify

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
