#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/aws-oidc-setup.sh \
    --account-id <aws-account-id> \
    --region <aws-region> \
    --github-org <github-org> \
    --github-repo <github-repo> \
    --staging-role-name <iam-role-name> \
    --production-role-name <iam-role-name> \
    --target eks|generic \
    [--cluster-name <eks-cluster-name>] \
    [--staging-namespaces <namespace[,namespace...]>] \
    [--production-namespaces <namespace[,namespace...]>] \
    [--enable-eks-access]

Environment:
  AWS_PROFILE                 Optional AWS CLI profile.
  GITHUB_OIDC_THUMBPRINTS     Optional comma-separated thumbprint list for older IAM setups.
  EKS_ACCESS_POLICY_ARN       Optional EKS access policy ARN; defaults to AmazonEKSEditPolicy.

Notes:
  - No static AWS keys are created or stored.
  - The GitHub OIDC trust is scoped to environment claims:
    repo:<org>/<repo>:environment:staging
    repo:<org>/<repo>:environment:production
USAGE
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

json_tmp() {
  mktemp "${TMPDIR:-/tmp}/aws-oidc-setup.XXXXXX.json"
}

split_csv_json() {
  jq -Rn --arg value "$1" '$value | split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(length > 0))'
}

ACCOUNT_ID=
REGION=
GITHUB_ORG=
GITHUB_REPO=
STAGING_ROLE_NAME=
PRODUCTION_ROLE_NAME=
TARGET=
CLUSTER_NAME=
STAGING_NAMESPACES=
PRODUCTION_NAMESPACES=
ENABLE_EKS_ACCESS=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --account-id)
      ACCOUNT_ID=${2:?}
      shift 2
      ;;
    --region)
      REGION=${2:?}
      shift 2
      ;;
    --github-org)
      GITHUB_ORG=${2:?}
      shift 2
      ;;
    --github-repo)
      GITHUB_REPO=${2:?}
      shift 2
      ;;
    --staging-role-name)
      STAGING_ROLE_NAME=${2:?}
      shift 2
      ;;
    --production-role-name)
      PRODUCTION_ROLE_NAME=${2:?}
      shift 2
      ;;
    --target)
      TARGET=${2:?}
      shift 2
      ;;
    --cluster-name)
      CLUSTER_NAME=${2:?}
      shift 2
      ;;
    --staging-namespaces)
      STAGING_NAMESPACES=${2:?}
      shift 2
      ;;
    --production-namespaces)
      PRODUCTION_NAMESPACES=${2:?}
      shift 2
      ;;
    --enable-eks-access)
      ENABLE_EKS_ACCESS=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

need aws
need jq

[ -n "$ACCOUNT_ID" ] || die "--account-id is required"
[ -n "$REGION" ] || die "--region is required"
[ -n "$GITHUB_ORG" ] || die "--github-org is required"
[ -n "$GITHUB_REPO" ] || die "--github-repo is required"
[ -n "$STAGING_ROLE_NAME" ] || die "--staging-role-name is required"
[ -n "$PRODUCTION_ROLE_NAME" ] || die "--production-role-name is required"
[ -n "$TARGET" ] || die "--target is required"

case "$TARGET" in
  eks|generic) ;;
  *) die "--target must be eks or generic" ;;
esac

case "$ACCOUNT_ID" in
  *[!0-9]*|'') die "--account-id must be the 12-digit AWS account ID" ;;
esac
[ "${#ACCOUNT_ID}" -eq 12 ] || die "--account-id must be 12 digits"

if [ "$TARGET" = "eks" ] || [ "$ENABLE_EKS_ACCESS" = true ]; then
  [ -n "$CLUSTER_NAME" ] || die "--cluster-name is required for EKS setup"
fi

if [ "$ENABLE_EKS_ACCESS" = true ]; then
  [ -n "$STAGING_NAMESPACES" ] || die "--staging-namespaces is required with --enable-eks-access"
  [ -n "$PRODUCTION_NAMESPACES" ] || die "--production-namespaces is required with --enable-eks-access"
fi

SCRIPT_DIR=$(unset CDPATH; cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(unset CDPATH; cd -- "$SCRIPT_DIR/.." && pwd)
POLICY_DIR=$REPO_ROOT/infra/iam-policies
POLICY_FILE=$POLICY_DIR/deploy-aws-generic.json
if [ "$TARGET" = "eks" ]; then
  POLICY_FILE=$POLICY_DIR/deploy-eks.json
fi
[ -f "$POLICY_FILE" ] || die "policy file not found: $POLICY_FILE"

OIDC_URL=https://token.actions.githubusercontent.com
OIDC_HOSTPATH=token.actions.githubusercontent.com
OIDC_AUDIENCE=sts.amazonaws.com
OIDC_PROVIDER_ARN=arn:aws:iam::"$ACCOUNT_ID":oidc-provider/"$OIDC_HOSTPATH"
EKS_ACCESS_POLICY_ARN=${EKS_ACCESS_POLICY_ARN:-arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy}

printf 'Configuring GitHub OIDC for %s/%s in AWS account %s\n' "$GITHUB_ORG" "$GITHUB_REPO" "$ACCOUNT_ID"

if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_PROVIDER_ARN" >/dev/null 2>&1; then
  printf 'OIDC provider exists: %s\n' "$OIDC_PROVIDER_ARN"
else
  create_args=(iam create-open-id-connect-provider --url "$OIDC_URL" --client-id-list "$OIDC_AUDIENCE")
  if [ -n "${GITHUB_OIDC_THUMBPRINTS:-}" ]; then
    IFS=',' read -r -a thumbprints <<<"$GITHUB_OIDC_THUMBPRINTS"
    create_args+=(--thumbprint-list "${thumbprints[@]}")
  fi
  aws "${create_args[@]}" >/dev/null
  printf 'Created OIDC provider: %s\n' "$OIDC_PROVIDER_ARN"
fi

render_trust_policy() {
  env_name=$1
  output_file=$2
  jq -n \
    --arg provider_arn "$OIDC_PROVIDER_ARN" \
    --arg oidc_hostpath "$OIDC_HOSTPATH" \
    --arg audience "$OIDC_AUDIENCE" \
    --arg subject "repo:$GITHUB_ORG/$GITHUB_REPO:environment:$env_name" \
    '{
      Version: "2012-10-17",
      Statement: [
        {
          Effect: "Allow",
          Principal: {Federated: $provider_arn},
          Action: "sts:AssumeRoleWithWebIdentity",
          Condition: {
            StringEquals: {
              ($oidc_hostpath + ":aud"): $audience,
              ($oidc_hostpath + ":sub"): $subject
            }
          }
        }
      ]
    }' >"$output_file"
}

render_inline_policy() {
  output_file=$1
  jq \
    --arg account_id "$ACCOUNT_ID" \
    --arg region "$REGION" \
    --arg cluster_name "$CLUSTER_NAME" \
    --arg github_org "$GITHUB_ORG" \
    --arg github_repo "$GITHUB_REPO" \
    'walk(
      if type == "string" then
        gsub("\\$\\{AWS_ACCOUNT_ID\\}"; $account_id)
        | gsub("\\$\\{AWS_REGION\\}"; $region)
        | gsub("\\$\\{EKS_CLUSTER_NAME\\}"; $cluster_name)
        | gsub("\\$\\{GITHUB_ORG\\}"; $github_org)
        | gsub("\\$\\{GITHUB_REPO\\}"; $github_repo)
      else
        .
      end
    )' "$POLICY_FILE" >"$output_file"
}

upsert_role() {
  env_name=$1
  role_name=$2
  trust_file=$(json_tmp)
  policy_file=$(json_tmp)

  render_trust_policy "$env_name" "$trust_file"
  render_inline_policy "$policy_file"

  if aws iam get-role --role-name "$role_name" >/dev/null 2>&1; then
    aws iam update-assume-role-policy \
      --role-name "$role_name" \
      --policy-document "file://$trust_file" >/dev/null
    printf 'Updated trust policy for role: %s\n' "$role_name"
  else
    aws iam create-role \
      --role-name "$role_name" \
      --assume-role-policy-document "file://$trust_file" \
      --description "GitHub Actions $env_name deployment role for $GITHUB_ORG/$GITHUB_REPO" \
      --max-session-duration 3600 >/dev/null
    printf 'Created role: %s\n' "$role_name"
  fi

  aws iam put-role-policy \
    --role-name "$role_name" \
    --policy-name GitHubActionsDeploy \
    --policy-document "file://$policy_file" >/dev/null
  printf 'Attached inline deployment policy to role: %s\n' "$role_name"

  rm -f "$trust_file" "$policy_file"
}

role_arn() {
  role_name=$1
  printf 'arn:aws:iam::%s:role/%s' "$ACCOUNT_ID" "$role_name"
}

ensure_eks_access() {
  role_name=$1
  namespaces_csv=$2
  principal_arn=$(role_arn "$role_name")
  namespace_json=$(split_csv_json "$namespaces_csv")
  namespace_args=()

  while IFS= read -r namespace; do
    namespace_args+=("$namespace")
  done < <(printf '%s\n' "$namespace_json" | jq -r '.[]')
  namespaces_arg=$(printf '%s\n' "$namespace_json" | jq -r 'join(",")')

  if [ "${#namespace_args[@]}" -eq 0 ]; then
    die "at least one namespace is required for $role_name"
  fi

  if aws eks describe-access-entry \
    --region "$REGION" \
    --cluster-name "$CLUSTER_NAME" \
    --principal-arn "$principal_arn" >/dev/null 2>&1; then
    printf 'EKS access entry exists for: %s\n' "$principal_arn"
  else
    aws eks create-access-entry \
      --region "$REGION" \
      --cluster-name "$CLUSTER_NAME" \
      --principal-arn "$principal_arn" \
      --type STANDARD >/dev/null
    printf 'Created EKS access entry for: %s\n' "$principal_arn"
  fi

  existing_policy=$(
    aws eks list-associated-access-policies \
      --region "$REGION" \
      --cluster-name "$CLUSTER_NAME" \
      --principal-arn "$principal_arn" \
      --query "associatedAccessPolicies[?policyArn=='$EKS_ACCESS_POLICY_ARN'].policyArn | [0]" \
      --output text
  )

  if [ "$existing_policy" = "$EKS_ACCESS_POLICY_ARN" ]; then
    printf 'EKS access policy already associated for: %s\n' "$principal_arn"
    printf '  Review namespace scope manually if you changed namespace inputs.\n'
  else
    aws eks associate-access-policy \
      --region "$REGION" \
      --cluster-name "$CLUSTER_NAME" \
      --principal-arn "$principal_arn" \
      --policy-arn "$EKS_ACCESS_POLICY_ARN" \
      --access-scope type=namespace,namespaces="$namespaces_arg" >/dev/null
    printf 'Associated AmazonEKSEditPolicy for %s on namespaces: %s\n' "$principal_arn" "$namespaces_csv"
  fi
}

upsert_role staging "$STAGING_ROLE_NAME"
upsert_role production "$PRODUCTION_ROLE_NAME"

if [ "$ENABLE_EKS_ACCESS" = true ]; then
  ensure_eks_access "$STAGING_ROLE_NAME" "$STAGING_NAMESPACES"
  ensure_eks_access "$PRODUCTION_ROLE_NAME" "$PRODUCTION_NAMESPACES"
fi

cat <<SUMMARY

AWS OIDC setup summary
- OIDC provider: $OIDC_PROVIDER_ARN
- Staging role: $(role_arn "$STAGING_ROLE_NAME")
- Production role: $(role_arn "$PRODUCTION_ROLE_NAME")
- Inline policy source: $POLICY_FILE

Human-only follow-up
- Store the role ARNs as GitHub environment variables or secrets.
- Confirm GitHub environments are named exactly staging and production.
- Replace starter policy wildcards with service-specific resources before production use.
- For EKS, confirm Kubernetes RBAC and namespace names match the access entry scopes.
SUMMARY
