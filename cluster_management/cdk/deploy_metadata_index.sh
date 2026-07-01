#!/usr/bin/env bash
#
# deploy_metadata_index.sh -- one-stop deploy/config for the Upload Metadata Index
# dashboard. Wraps the manual steps from METADATA_INDEX.md so a deploy is a single
# command instead of a checklist. Uses only the `aws` CLI + `cdk` (no `eb` CLI).
#
# Subcommands:
#   web-arn            Resolve + print the IAM principal ARN the web tier uses at
#                      runtime (the principal that assumes the reader role). This is
#                      the value for `-c reader_principal_arn` (scopes the trust).
#   outputs            Print the deployed stack's outputs as KEY=VALUE.
#   set-env            Set the METADATA_INDEX_* vars on the EB web env from the stack
#                      outputs (elasticbeanstalk update-environment). Preview unless --apply.
#   deploy             cdk deploy the stack (enable + scoped trust), then set-env.
#                      Preview unless --apply.
#   reset              Print (or, with --execute, run) the DESTRUCTIVE ordered-drain
#                      reset for a schema change. Separate on purpose.
#
# Config (env vars or flags):
#   AWS_PROFILE              profile for aws + cdk calls (default: credential chain)
#   AWS_REGION               region (default: us-east-1)  -> METADATA_INDEX_REGION
#   STACK_NAME               CFN stack (default: MetadataIndexStack)
#   EB_APP                   EB application (default: beiwe-application)
#   EB_ENV                   EB environment (default: kowalski-beiwe)
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
EB_APP="${EB_APP:-beiwe-application}"
EB_ENV="${EB_ENV:-kowalski-beiwe}"
READER_PRINCIPAL_ARN="${READER_PRINCIPAL_ARN:-}"
APPLY=false
EXECUTE=false
_BF_RULE=""           # backfill: rule name + disabled-state, for the re-enable trap
_BF_RULE_DISABLED=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"          # cluster_management/cdk

die() { echo "error: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1 || die "'$1' not found on PATH"; }
aws_profile_arg() { [ -n "$AWS_PROFILE" ] && printf -- '--profile %s' "$AWS_PROFILE" || true; }

cfn_output() {  # $1 = OutputKey
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" $(aws_profile_arg) \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text 2>/dev/null
}

eb_env_value() {  # $1 = env var name set on the EB web environment
  aws elasticbeanstalk describe-configuration-settings \
    --application-name "$EB_APP" --environment-name "$EB_ENV" --region "$AWS_REGION" $(aws_profile_arg) \
    --query "ConfigurationSettings[0].OptionSettings[?Namespace=='aws:elasticbeanstalk:application:environment' && OptionName=='$1'].Value | [0]" \
    --output text 2>/dev/null
}

# --- resolve the web principal ARN (the runtime assume-role caller) --------------
# The web tier either uses explicit IAM-user keys (BEIWE_SERVER_AWS_*, set as EB env
# properties) -> the principal is that IAM user; or it falls back to the EB instance
# profile -> the principal is that role. Handle both, all via the aws CLI so it works
# from a laptop with just an admin profile (no eb CLI, no server secrets).
resolve_web_arn() {
  if [ -n "$READER_PRINCIPAL_ARN" ]; then echo "$READER_PRINCIPAL_ARN"; return; fi
  have aws
  local account; account="$(aws sts get-caller-identity --region "$AWS_REGION" $(aws_profile_arg) --query Account --output text)" \
    || die "aws sts get-caller-identity failed -- check AWS_PROFILE/credentials"

  local key_id; key_id="$(eb_env_value BEIWE_SERVER_AWS_ACCESS_KEY_ID)"
  if [ -n "$key_id" ] && [ "$key_id" != "None" ]; then
    local user; user="$(aws iam get-access-key-last-used --access-key-id "$key_id" $(aws_profile_arg) --query UserName --output text 2>/dev/null)"
    [ -n "$user" ] && [ "$user" != "None" ] || die "could not map the web access key to an IAM user; set READER_PRINCIPAL_ARN"
    echo "arn:aws:iam::${account}:user/${user}"; return
  fi

  # No IAM-user keys on the env -> the web tier uses its instance-profile role.
  local profile_name; profile_name="$(aws elasticbeanstalk describe-configuration-settings \
    --application-name "$EB_APP" --environment-name "$EB_ENV" --region "$AWS_REGION" $(aws_profile_arg) \
    --query "ConfigurationSettings[0].OptionSettings[?OptionName=='IamInstanceProfile'].Value | [0]" --output text 2>/dev/null)"
  [ -n "$profile_name" ] && [ "$profile_name" != "None" ] || die "could not resolve the web principal; set READER_PRINCIPAL_ARN"
  local role_arn; role_arn="$(aws iam get-instance-profile --instance-profile-name "$profile_name" $(aws_profile_arg) \
    --query 'InstanceProfile.Roles[0].Arn' --output text 2>/dev/null)"
  [ -n "$role_arn" ] && [ "$role_arn" != "None" ] || die "could not resolve the instance-profile role; set READER_PRINCIPAL_ARN"
  echo "$role_arn"
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
  have aws
  local table reader
  table="$(cfn_output TableName)"; reader="$(cfn_output ReaderRoleArn)"
  [ -n "$table" ] && [ "$table" != "None" ] || die "no TableName output on stack '$STACK_NAME' (deploy it first)"
  local ns="aws:elasticbeanstalk:application:environment"
  local opts=(
    "Namespace=$ns,OptionName=METADATA_INDEX_ENABLED,Value=true"
    "Namespace=$ns,OptionName=METADATA_INDEX_TABLE_NAME,Value=$table"
    "Namespace=$ns,OptionName=METADATA_INDEX_REGION,Value=$AWS_REGION"
    "Namespace=$ns,OptionName=METADATA_INDEX_READER_ROLE_ARN,Value=$reader"
  )
  echo "set on EB env '$EB_ENV' (app '$EB_APP'):"
  echo "  METADATA_INDEX_ENABLED=true"
  echo "  METADATA_INDEX_TABLE_NAME=$table"
  echo "  METADATA_INDEX_REGION=$AWS_REGION"
  echo "  METADATA_INDEX_READER_ROLE_ARN=$reader"
  if $APPLY; then
    echo ">> applying via elasticbeanstalk update-environment (rolling update)..."
    aws elasticbeanstalk update-environment --application-name "$EB_APP" --environment-name "$EB_ENV" \
      --region "$AWS_REGION" $(aws_profile_arg) --option-settings "${opts[@]}"
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

_sqs_depth() {  # $1 = url; sets _Q_VIS/_Q_NOTVIS in the current shell (so a failed aws aborts)
  local out
  out="$(aws sqs get-queue-attributes --queue-url "$1" --region "$AWS_REGION" $(aws_profile_arg) \
      --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
      --query 'Attributes.[ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible]' --output text)" \
    || die "could not read queue attributes for $1 (aws error) -- aborting before any write"
  read -r _Q_VIS _Q_NOTVIS <<<"$out"
  [[ "${_Q_VIS:-}" =~ ^[0-9]+$ && "${_Q_NOTVIS:-}" =~ ^[0-9]+$ ]] \
    || die "unexpected queue attributes for $1: '$out'"
}

_wait_drained() {  # $1 = main queue url; poll until empty (the live writer drains it)
  local url="$1" tries=0
  while :; do
    _sqs_depth "$url"
    [ "$_Q_VIS" = "0" ] && [ "$_Q_NOTVIS" = "0" ] && break
    tries=$((tries + 1))
    [ "$tries" -gt 60 ] && die "main queue did not drain after ~5min: $url ($_Q_VIS visible / $_Q_NOTVIS in-flight)"
    echo "   draining $url: $_Q_VIS visible / $_Q_NOTVIS in-flight ..."
    sleep 5
  done
}

_assert_empty() {  # $1 = DLQ url; one-shot -- a DLQ never self-drains, so don't poll it
  _sqs_depth "$1"
  [ "$_Q_VIS" = "0" ] && [ "$_Q_NOTVIS" = "0" ] \
    || die "DLQ not empty ($_Q_VIS + $_Q_NOTVIS messages): redrive or clear it before backfilling -- $1"
}

_reenable_rule_on_exit() {  # safety net: if we exit after disabling, turn ingestion back on
  [ "$_BF_RULE_DISABLED" = "1" ] || return 0
  echo ">> (cleanup) re-enabling ingestion rule after an early exit ..." >&2
  aws events enable-rule --name "$_BF_RULE" --region "$AWS_REGION" $(aws_profile_arg) || true
}

cmd_backfill() {
  # Non-destructive: derive the participant-daily aggregate + first-seen from the
  # existing per-stream rollups via SET, while ingestion is paused. Drains the main
  # queue by WAITING (the live writer finishes queued uploads into the rollups) --
  # never purges; the DLQ must already be empty.
  have aws; have python3
  local rule queue dlq
  rule="$(cfn_output EventBridgeRuleName)"; queue="$(cfn_output QueueUrl)"; dlq="$(cfn_output DlqUrl)"
  [ -n "$rule" ] && [ "$rule" != "None" ] || die "no stack outputs; deploy MetadataIndexStack first"
  local py=("$SCRIPT_DIR/backfill_participant_daily.py" --region "$AWS_REGION" --stack "$STACK_NAME")
  [ -n "$AWS_PROFILE" ] && py+=(--profile "$AWS_PROFILE")
  if ! $APPLY; then
    echo "PREVIEW: assert DLQ empty -> disable rule $rule -> wait for main queue to drain -> SET-backfill -> re-enable rule."
    python3 "${py[@]}"
    echo "(preview only -- re-run with --apply to pause ingestion and write)"
    return
  fi
  _assert_empty "$dlq"                              # fail fast, before touching ingestion
  _BF_RULE="$rule"
  trap _reenable_rule_on_exit EXIT                  # re-enable the rule even if a later step dies
  echo ">> disabling ingestion rule $rule ..."
  aws events disable-rule --name "$rule" --region "$AWS_REGION" $(aws_profile_arg)
  _BF_RULE_DISABLED=1
  echo ">> draining main queue (waiting, not purging) ..."
  _wait_drained "$queue"
  echo ">> backfilling (SET from existing rollups) ..."
  python3 "${py[@]}" --apply
  echo ">> re-enabling ingestion rule ..."
  aws events enable-rule --name "$rule" --region "$AWS_REGION" $(aws_profile_arg)
  _BF_RULE_DISABLED=0
  trap - EXIT
  echo ">> backfill complete; ingestion resumed."
}

# --- arg parsing ----------------------------------------------------------------
[ $# -ge 1 ] || die "usage: $0 {web-arn|outputs|set-env|deploy|backfill|reset} [--apply|--execute] [--profile P] [--region R]"
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
  backfill) cmd_backfill ;;
  reset)    cmd_reset ;;
  *) die "unknown subcommand: $SUBCMD" ;;
esac
