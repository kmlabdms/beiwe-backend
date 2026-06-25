#!/usr/bin/env bash
#
# deploy_metadata_index.sh -- one-stop deploy/config for the Upload Metadata Index
# dashboard. Wraps the manual steps from METADATA_INDEX.md so a deploy is a single
# command instead of a checklist. Read-only by default where it can be.
#
# Subcommands:
#   web-arn            Resolve + print the IAM principal ARN the web tier uses at
#                      runtime (the principal that assumes the reader role). This is
#                      the value for `-c reader_principal_arn` (scopes the trust).
#   outputs            Print the deployed stack's outputs as KEY=VALUE.
#   set-env            Set the METADATA_INDEX_* vars on the EB web env from the stack
#                      outputs via `eb setenv`. Preview-only unless --apply.
#   deploy             cdk deploy the stack (enable + scoped trust), then set-env.
#                      Preview-only unless --apply.
#   reset              Print (or, with --execute, run) the DESTRUCTIVE ordered-drain
#                      reset for a schema change. Separate on purpose.
#
# Config (env vars or flags):
#   AWS_PROFILE              profile for aws + cdk calls (default: credential chain)
#   AWS_REGION               region (default: us-east-1)  -> METADATA_INDEX_REGION
#   STACK_NAME               CFN stack (default: MetadataIndexStack)
#   EB_ENV                   EB environment (default: eb's configured default)
#   READER_PRINCIPAL_ARN     override the auto-derived web principal ARN
#
# Flags: --apply (perform mutations), --execute (reset only), --profile, --region.
#
# NOTE: mutating actions (set-env, deploy, reset) are PREVIEW-ONLY unless you pass
# --apply / --execute, so a bare run never changes infrastructure.

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-MetadataIndexStack}"
AWS_PROFILE="${AWS_PROFILE:-}"
EB_ENV="${EB_ENV:-}"
READER_PRINCIPAL_ARN="${READER_PRINCIPAL_ARN:-}"
APPLY=false
EXECUTE=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"          # cluster_management/cdk
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"                        # repo root (has .elasticbeanstalk)

die() { echo "error: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1 || die "'$1' not found on PATH"; }

# aws/eb take --profile differently; build the aws profile arg once.
aws_profile_arg() { [ -n "$AWS_PROFILE" ] && printf -- '--profile %s' "$AWS_PROFILE" || true; }

cfn_output() {  # $1 = OutputKey
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" $(aws_profile_arg) \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null
}

# --- resolve the web principal ARN (the runtime assume-role caller) --------------
resolve_web_arn() {
  if [ -n "$READER_PRINCIPAL_ARN" ]; then
    echo "$READER_PRINCIPAL_ARN"; return
  fi
  # Best signal: ask STS who the web credentials actually are.
  if [ -n "${BEIWE_SERVER_AWS_ACCESS_KEY_ID:-}" ] && [ -n "${BEIWE_SERVER_AWS_SECRET_ACCESS_KEY:-}" ]; then
    AWS_ACCESS_KEY_ID="$BEIWE_SERVER_AWS_ACCESS_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$BEIWE_SERVER_AWS_SECRET_ACCESS_KEY" \
    AWS_SESSION_TOKEN="" \
      aws sts get-caller-identity --region "$AWS_REGION" --query Arn --output text && return
  fi
  # Fallback: derive from the access key id stored in the EB env (no secret needed,
  # but the deployer needs iam:GetAccessKeyLastUsed + sts:GetCallerIdentity).
  have eb
  local key_id account user
  key_id="$(cd "$REPO_ROOT" && eb printenv ${EB_ENV:+$EB_ENV} 2>/dev/null \
    | sed -n 's/.*BEIWE_SERVER_AWS_ACCESS_KEY_ID *= *//p' | tr -d '[:space:]')"
  [ -n "$key_id" ] || die "could not resolve the web principal ARN; set READER_PRINCIPAL_ARN or BEIWE_SERVER_AWS_* and retry"
  user="$(aws iam get-access-key-last-used --access-key-id "$key_id" $(aws_profile_arg) --query UserName --output text)"
  account="$(aws sts get-caller-identity --region "$AWS_REGION" $(aws_profile_arg) --query Account --output text)"
  echo "arn:aws:iam::${account}:user/${user}"
}

# --- subcommands ----------------------------------------------------------------
cmd_web_arn() { have aws; resolve_web_arn; }

cmd_outputs() {
  have aws
  local table reader
  table="$(cfn_output TableName)"; reader="$(cfn_output ReaderRoleArn)"
  [ -n "$table" ] && [ "$table" != "None" ] || die "no TableName output on stack '$STACK_NAME' (is it deployed?)"
  echo "METADATA_INDEX_TABLE_NAME=$table"
  echo "METADATA_INDEX_READER_ROLE_ARN=$reader"
  echo "METADATA_INDEX_REGION=$AWS_REGION"
}

cmd_set_env() {
  have aws; have eb
  local table reader
  table="$(cfn_output TableName)"; reader="$(cfn_output ReaderRoleArn)"
  [ -n "$table" ] && [ "$table" != "None" ] || die "no TableName output on stack '$STACK_NAME' (deploy it first)"
  local kv=(
    "METADATA_INDEX_ENABLED=true"
    "METADATA_INDEX_TABLE_NAME=$table"
    "METADATA_INDEX_REGION=$AWS_REGION"
    "METADATA_INDEX_READER_ROLE_ARN=$reader"
  )
  echo "eb setenv ${kv[*]} ${EB_ENV}"
  if $APPLY; then
    echo ">> applying to the EB web environment (rolling update)..."
    ( cd "$REPO_ROOT" && eb setenv "${kv[@]}" ${EB_ENV:+$EB_ENV} )
  else
    echo "(preview only -- re-run with --apply to set these on the EB environment)"
  fi
}

cmd_deploy() {
  have aws; have cdk
  local arn; arn="$(resolve_web_arn)"
  echo "reader_principal_arn (scoped trust) = $arn"
  echo "cdk deploy $STACK_NAME -c enable_metadata_index=true -c reader_principal_arn=$arn"
  if $APPLY; then
    ( cd "$SCRIPT_DIR" && cdk deploy "$STACK_NAME" \
        -c enable_metadata_index=true -c reader_principal_arn="$arn" \
        ${AWS_PROFILE:+--profile "$AWS_PROFILE"} )
    cmd_set_env
  else
    echo "(preview only -- re-run with --apply to deploy the stack and set the web env)"
  fi
}

cmd_reset() {
  have aws
  local rule queue dlq table
  rule="$(cfn_output EventBridgeRuleName)"; queue="$(cfn_output QueueUrl)"
  dlq="$(cfn_output DlqUrl)"; table="$(cfn_output TableName)"
  [ -n "$table" ] && [ "$table" != "None" ] || die "no stack outputs; nothing to reset"
  cat <<EOF
DESTRUCTIVE ordered-drain reset (schema change) for table: $table
  1. aws events disable-rule --name $rule
  2. aws sqs purge-queue --queue-url $queue   # and: $dlq
  3. aws dynamodb delete-table --table-name $table   (then redeploy recreates it)
  4. cdk deploy $STACK_NAME -c enable_metadata_index=true
  5. aws events enable-rule --name $rule
EOF
  if $EXECUTE; then
    [ "${I_UNDERSTAND_THIS_DELETES_DATA:-}" = "yes" ] || die "refusing: set I_UNDERSTAND_THIS_DELETES_DATA=yes to --execute the reset"
    echo ">> executing reset..."
    aws events disable-rule --name "$rule" --region "$AWS_REGION" $(aws_profile_arg)
    aws sqs purge-queue --queue-url "$queue" --region "$AWS_REGION" $(aws_profile_arg)
    aws sqs purge-queue --queue-url "$dlq" --region "$AWS_REGION" $(aws_profile_arg)
    echo "queues purged; waiting 60s for the purge to settle before wiping the table..."
    sleep 60
    aws dynamodb delete-table --table-name "$table" --region "$AWS_REGION" $(aws_profile_arg)
    aws dynamodb wait table-not-exists --table-name "$table" --region "$AWS_REGION" $(aws_profile_arg)
    ( cd "$SCRIPT_DIR" && cdk deploy "$STACK_NAME" -c enable_metadata_index=true ${AWS_PROFILE:+--profile "$AWS_PROFILE"} )
    aws events enable-rule --name "$rule" --region "$AWS_REGION" $(aws_profile_arg)
    echo ">> reset complete."
  else
    echo "(preview only -- re-run with --execute and I_UNDERSTAND_THIS_DELETES_DATA=yes to run it)"
  fi
}

# --- arg parsing ----------------------------------------------------------------
[ $# -ge 1 ] || die "usage: $0 {web-arn|outputs|set-env|deploy|reset} [--apply|--execute] [--profile P] [--region R]"
SUBCMD="$1"; shift
while [ $# -gt 0 ]; do
  case "$1" in
    --apply)    APPLY=true ;;
    --execute)  EXECUTE=true ;;
    --profile)  AWS_PROFILE="$2"; shift ;;
    --region)   AWS_REGION="$2"; shift ;;
    *) die "unknown flag: $1" ;;
  esac
  shift
done

case "$SUBCMD" in
  web-arn)  cmd_web_arn ;;
  outputs)  cmd_outputs ;;
  set-env)  cmd_set_env ;;
  deploy)   cmd_deploy ;;
  reset)    cmd_reset ;;
  *) die "unknown subcommand: $SUBCMD" ;;
esac
