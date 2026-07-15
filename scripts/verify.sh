#!/usr/bin/env bash
set -euo pipefail

failures=0

note() {
  printf '%s\n' "$*"
}

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  failures=1
}

have() {
  command -v "$1" >/dev/null 2>&1
}

collect_files() {
  find "$@" -type f 2>/dev/null | sort
}

require_jq() {
  if ! have jq; then
    fail "jq is required for JSON validation"
    return 1
  fi
}

run_jq() {
  require_jq || return 0
  files=$(collect_files .github/rulesets infra/iam-policies | grep -E '\.json$' || true)
  if [ -z "$files" ]; then
    note "jq: no JSON files found"
    return 0
  fi

  note "jq: validating JSON"
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    if ! jq empty "$file" >/dev/null; then
      fail "jq validation failed for $file"
    fi
  done <<EOF
$files
EOF
}

run_shellcheck() {
  files=$(collect_files scripts infra | grep -E '\.sh$' || true)
  if [ -z "$files" ]; then
    note "shellcheck: no shell scripts found"
    return 0
  fi

  if ! have shellcheck; then
    note "shellcheck: skipped; command not found"
    return 0
  fi

  note "shellcheck: checking shell scripts"
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    if ! shellcheck "$file"; then
      fail "shellcheck reported issues in $file"
    fi
  done <<EOF
$files
EOF
}

run_python_syntax() {
  files=$(collect_files scripts infra orchestrator | grep -E '\.py$' || true)
  if [ -z "$files" ]; then
    note "python syntax: no Python files found"
    return 0
  fi

  note "python syntax: parsing Python files"
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    if ! python3 - "$file" <<'PY'
import ast
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
PY
    then
      fail "Python syntax validation failed for $file"
    fi
  done <<EOF
$files
EOF
}

run_python_tests() {
  if [ ! -d tests ]; then
    note "python tests: skipped; no tests directory"
    return 0
  fi

  note "python tests: running deterministic unittest suite"
  if ! python3 -m unittest discover -s tests -t . -v; then
    fail "python tests failed"
  fi
}

run_fleet_config_validation() {
  if [ ! -f config/fleets/manifest.toml ]; then
    note "fleet config: skipped; no production fleet manifest"
    return 0
  fi

  note "fleet config: validating manifest and profiles"
  if ! python3 scripts/fleet_config.py validate; then
    fail "fleet configuration validation failed"
  fi
}

workflow_files() {
  find .github/workflows -type f \( -name '*.yml' -o -name '*.yaml' \) 2>/dev/null | sort || true
}

run_actionlint() {
  files=$(workflow_files)
  if [ -z "$files" ]; then
    note "actionlint: skipped; no workflow files found"
    return 0
  fi

  if ! have actionlint; then
    note "actionlint: skipped; command not found"
    return 0
  fi

  note "actionlint: checking workflows"
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    if ! actionlint "$file"; then
      fail "actionlint reported issues in $file"
    fi
  done <<EOF
$files
EOF
}

run_zizmor() {
  files=$(workflow_files)
  if [ -z "$files" ]; then
    note "zizmor: skipped; no workflow files found"
    return 0
  fi

  if ! have zizmor; then
    note "zizmor: skipped; command not found"
    return 0
  fi

  note "zizmor: auditing workflows"
  if ! zizmor .github/workflows; then
    fail "zizmor reported issues"
  fi
}

check_unpinned_uses() {
  files=$(workflow_files)
  if [ -z "$files" ]; then
    note "pinning: skipped; no workflow files found"
    return 0
  fi

  note "pinning: checking workflow uses references"
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    while IFS= read -r match; do
      line_no=${match%%:*}
      ref=${match#*:}
      ref=${ref%%#*}
      ref=${ref#uses:}
      ref=${ref#"${ref%%[![:space:]]*}"}
      ref=${ref%"${ref##*[![:space:]]}"}
      ref=${ref%\"}
      ref=${ref#\"}
      ref=${ref%\'}
      ref=${ref#\'}

      case "$ref" in
        ./*|docker:*|\$\{\{*) continue ;;
      esac

      if [[ ! "$ref" =~ @[0-9a-fA-F]{40}$ ]]; then
        fail "$file:$line_no uses is not pinned to a full commit SHA: $ref"
      fi
    done < <(grep -En '^[[:space:]]*uses:[[:space:]]*[^#[:space:]]+' "$file" | sed -E 's/^[[:space:]]*([0-9]+):[[:space:]]*/\1:/' || true)
  done <<EOF
$files
EOF
}

check_forbidden_aws_static_credentials() {
  files=$(workflow_files)
  if [ -z "$files" ]; then
    note "aws static credentials: skipped; no workflow files found"
    return 0
  fi

  note "aws static credentials: checking workflows"
  if grep -RInE 'AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|aws-access-key-id|aws-secret-access-key|aws-session-token' .github/workflows; then
    fail "forbidden AWS static credential names found in workflows"
  fi
}

run_jq
run_shellcheck
run_python_syntax
run_python_tests
run_fleet_config_validation
run_actionlint
run_zizmor
check_unpinned_uses
check_forbidden_aws_static_credentials

if [ "$failures" -ne 0 ]; then
  printf 'Verification failed.\n' >&2
  exit 1
fi

printf 'Verification passed.\n'
