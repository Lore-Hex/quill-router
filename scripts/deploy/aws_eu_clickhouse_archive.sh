#!/usr/bin/env bash
# Provision the AWS-EU ClickHouse Parquet archive: bucket, dedicated node
# identity, and the least-privilege grant between them.
#
# Idempotent. Without --apply it only prints the mutations it would make.
#
# WHY THIS EXISTS
#
# clickhouse/archive_daily.py is the only point-in-time copy of ClickHouse
# history that exists. It ran on GCP only, so the AWS-EU node's analytics
# history -- which is NOT a copy of GCP's, it is the rows this cloud's own
# gateway produced -- was protected by replication alone. Replication does not
# protect against logical corruption or deletion, because those replicate.
#
# WHY eu-west-3 AND NOT THE ACCOUNT'S USUAL us-east-1
#
# Every pre-existing bucket in this account is us-east-1. This one must not be.
# tr-eu-clickhouse-1 runs in eu-west-3, and scripts/deploy/aws_eu_clickhouse.sh
# stakes an EU audit claim on operational rows never leaving the EU. Archiving
# EU operational history into Virginia would break that claim quietly -- the
# archive would work perfectly and the residency statement would become false.
# So the region is asserted, not defaulted, and creation in us-east-1 is refused.
#
# WHY A DEDICATED INSTANCE PROFILE
#
# tr-eu-clickhouse-1 currently shares quill-enclave-instance-profile with five
# quill-enclave instances -- the attested gateway. Granting the archive bucket
# to that role would grant it to the enclave too.
#
# Separating them is narrower in BOTH directions, which is why it is worth the
# swap rather than just the grant:
#
#   the node stops carrying  : ecr:* (PullECR) and kms:Decrypt on the two
#                              cross-cloud SA keys -- enclave concerns it never
#                              uses
#   the enclave stops gaining: s3:PutObject on the archive bucket
#   the node's secret scope  : narrowed from quill/* to the one password secret
#
# WHY NO OBJECT LOCK
#
# Deliberately absent, and it must stay absent. The archive's immutability is
# enforced by conditional writes (IfNoneMatch on create), not by bucket policy,
# because put_json_pointer MUST overwrite _latest.json every time a revision is
# added. Object Lock would block that overwrite and wedge the archive after its
# first day. Versioning plus the noncurrent-version lifecycle rule gives the
# recoverability Object Lock would nominally provide, without breaking the
# pointer.
#
# WHY SSE-S3 AND NOT SSE-KMS
#
# SSE-KMS PutObject requires kms:GenerateDataKey. The node's role has only
# kms:Decrypt and kms:DescribeKey, so SSE-KMS would fail every write. SSE-S3
# (AES256) is enforced on the bucket instead.
#
# THE PROFILE SWAP IS A SEPARATE, GATED STEP
#
# --attach-profile is not part of the default run. The shared role grants
# dsql:DbConnect, and the analytics drain depends on it: swapping the node onto
# a role missing that permission stops delivery, and a stalled drain looks
# exactly like a quiet one. So the swap runs a pre-flight that proves the new
# role carries DSQL connect and Secrets Manager read BEFORE replacing the
# association, and the association is reversible in one command (printed on
# success).
set -euo pipefail

ACCOUNT="${TR_AWS_ACCOUNT:-330422590279}"
REGION="${TR_AWS_EU_REGION:-eu-west-3}"
NODE_NAME="${TR_AWS_EU_CLICKHOUSE_NAME:-tr-eu-clickhouse-1}"
BUCKET="${TR_AWS_EU_ARCHIVE_BUCKET:-quill-tr-clickhouse-archive-${ACCOUNT}}"
ROLE="${TR_AWS_EU_CLICKHOUSE_ROLE:-tr-eu-clickhouse-role}"
PROFILE="${TR_AWS_EU_CLICKHOUSE_PROFILE:-tr-eu-clickhouse-instance-profile}"
SECRET_ID="${TR_AWS_EU_SECRET_ID:-quill/tr-eu-clickhouse-password}"
DSQL_CLUSTER="${TR_AWS_EU_DSQL_CLUSTER:-tnt642i3ofzpn5z62msacutpuu}"
# 2555 days ~= 7 years, matching the GCP archive bucket's retention.
CURRENT_RETENTION_DAYS="${TR_AWS_EU_ARCHIVE_RETENTION_DAYS:-2555}"
NONCURRENT_RETENTION_DAYS="${TR_AWS_EU_ARCHIVE_NONCURRENT_DAYS:-30}"

APPLY=0
ATTACH_PROFILE=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --attach-profile) ATTACH_PROFILE=1 ;;
    *)
      echo "usage: $0 [--apply] [--attach-profile]" >&2
      exit 2
      ;;
  esac
done

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

run() {
  if [ "$APPLY" -eq 0 ]; then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

# Read-only helper. Mutations go through run() with an explicit --region so the
# dry-run output is a command an operator can actually paste.
aws_() { aws --region "$REGION" "$@"; }

# The residency claim is the reason this script exists in a second region, so
# it is checked rather than assumed.
if [ "$REGION" = "us-east-1" ]; then
  echo "refusing to create the EU archive in us-east-1: see the residency note above" >&2
  exit 2
fi

require_node_region() {
  log "confirming ${NODE_NAME} really is in ${REGION}"
  local found
  found="$(aws_ ec2 describe-instances \
    --filters "Name=tag:Name,Values=${NODE_NAME}" "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].InstanceId' --output text)"
  if [ -z "$found" ]; then
    echo "no running ${NODE_NAME} in ${REGION}; refusing to provision an archive" \
      "for a node that is not there" >&2
    exit 1
  fi
  log "found ${NODE_NAME} = ${found}"
}

ensure_bucket() {
  log "ensuring private, versioned, SSE-S3 archive bucket ${BUCKET} in ${REGION}"
  if aws_ s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    local loc
    loc="$(aws_ s3api get-bucket-location --bucket "$BUCKET" --output text)"
    # get-bucket-location reports us-east-1 as "None".
    if [ "$loc" != "$REGION" ]; then
      echo "bucket ${BUCKET} exists in ${loc}, not ${REGION}; refusing to use it" >&2
      exit 1
    fi
    log "bucket already exists in the right region"
  else
    run aws --region "$REGION" s3api create-bucket --bucket "$BUCKET" \
      --create-bucket-configuration "LocationConstraint=${REGION}"
  fi

  # Versioning is the recoverability mechanism, since Object Lock cannot be
  # used (see header).
  run aws --region "$REGION" s3api put-bucket-versioning --bucket "$BUCKET" \
    --versioning-configuration Status=Enabled

  # There is no account-level public access block to inherit, so it is set here.
  run aws --region "$REGION" s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
    'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

  run aws --region "$REGION" s3api put-bucket-encryption --bucket "$BUCKET" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

  run aws --region "$REGION" s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
    --lifecycle-configuration "{\"Rules\":[
      {\"ID\":\"expire-archived-days\",\"Status\":\"Enabled\",\"Filter\":{\"Prefix\":\"\"},
       \"Expiration\":{\"Days\":${CURRENT_RETENTION_DAYS}}},
      {\"ID\":\"expire-noncurrent\",\"Status\":\"Enabled\",\"Filter\":{\"Prefix\":\"\"},
       \"NoncurrentVersionExpiration\":{\"NoncurrentDays\":${NONCURRENT_RETENTION_DAYS}}}
    ]}"
}

ensure_role() {
  log "ensuring dedicated node role ${ROLE}"
  if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
    run aws iam create-role --role-name "$ROLE" \
      --description "tr-eu-clickhouse node: analytics drain and Parquet archive" \
      --assume-role-policy-document \
      '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  fi

  # SSM is how code reaches this node at all -- the drain installer ships the
  # tree as chunked base64 over send-command.
  run aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

  # Carried over from the shared role, because the drain stops without it.
  run aws iam put-role-policy --role-name "$ROLE" --policy-name dsql-connect-drain \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":[\"dsql:DbConnect\"],
       \"Resource\":[\"arn:aws:dsql:${REGION}:${ACCOUNT}:cluster/${DSQL_CLUSTER}\"]}
    ]}"

  # Narrower than the shared role's quill/*: this node reads one secret.
  run aws iam put-role-policy --role-name "$ROLE" --policy-name read-clickhouse-password \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
      {\"Sid\":\"ReadClickHousePassword\",\"Effect\":\"Allow\",
       \"Action\":[\"secretsmanager:GetSecretValue\",\"secretsmanager:DescribeSecret\"],
       \"Resource\":\"arn:aws:secretsmanager:${REGION}:${ACCOUNT}:secret:${SECRET_ID}*\"},
      {\"Sid\":\"DecryptViaSecretsManager\",\"Effect\":\"Allow\",
       \"Action\":[\"kms:Decrypt\",\"kms:DescribeKey\"],
       \"Resource\":\"arn:aws:kms:*:${ACCOUNT}:key/*\",
       \"Condition\":{\"StringLike\":{\"kms:ViaService\":\"secretsmanager.*.amazonaws.com\"}}}
    ]}"

  # The archive grant itself. s3:ListBucket is deliberately absent:
  # S3ArchiveStore only ever calls put_object, get_object and head_object, and
  # head_object is authorized by s3:GetObject. Scope is the one bucket.
  run aws iam put-role-policy --role-name "$ROLE" --policy-name write-clickhouse-archive \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
      {\"Sid\":\"WriteArchiveObjects\",\"Effect\":\"Allow\",
       \"Action\":[\"s3:PutObject\",\"s3:GetObject\"],
       \"Resource\":\"arn:aws:s3:::${BUCKET}/*\"}
    ]}"
}

ensure_profile() {
  log "ensuring instance profile ${PROFILE}"
  if ! aws iam get-instance-profile --instance-profile-name "$PROFILE" >/dev/null 2>&1; then
    run aws iam create-instance-profile --instance-profile-name "$PROFILE"
  fi
  if ! aws iam get-instance-profile --instance-profile-name "$PROFILE" \
    --query 'InstanceProfile.Roles[].RoleName' --output text 2>/dev/null | grep -qw "$ROLE"; then
    run aws iam add-role-to-instance-profile \
      --instance-profile-name "$PROFILE" --role-name "$ROLE"
  fi
}

# Proving the new role can do the drain's job BEFORE the node depends on it.
# A missing dsql:DbConnect would not fail loudly; it would stop delivery, and a
# stalled drain is indistinguishable from an idle one from the outside.
preflight_profile_swap() {
  log "pre-flight: the new role must carry DSQL connect and the secret read"
  local missing=0
  for policy in dsql-connect-drain read-clickhouse-password write-clickhouse-archive; do
    if ! aws iam get-role-policy --role-name "$ROLE" --policy-name "$policy" \
      >/dev/null 2>&1; then
      echo "  MISSING inline policy ${policy} on ${ROLE}" >&2
      missing=1
    fi
  done
  if ! aws iam list-attached-role-policies --role-name "$ROLE" \
    --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null |
    grep -q AmazonSSMManagedInstanceCore; then
    echo "  MISSING AmazonSSMManagedInstanceCore on ${ROLE}: SSM is how code reaches the node" >&2
    missing=1
  fi
  if [ "$missing" -ne 0 ]; then
    echo "refusing to swap the instance profile: the replacement role is incomplete" >&2
    echo "run without --attach-profile first (with --apply) to provision it" >&2
    exit 1
  fi
  log "pre-flight passed"
}

attach_profile() {
  preflight_profile_swap
  local instance association
  instance="$(aws_ ec2 describe-instances \
    --filters "Name=tag:Name,Values=${NODE_NAME}" "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].InstanceId' --output text)"
  association="$(aws_ ec2 describe-iam-instance-profile-associations \
    --filters "Name=instance-id,Values=${instance}" \
    --query 'IamInstanceProfileAssociations[?State==`associated`].AssociationId' \
    --output text)"

  if [ -z "$association" ]; then
    run aws --region "$REGION" ec2 associate-iam-instance-profile --instance-id "$instance" \
      --iam-instance-profile "Name=${PROFILE}"
  else
    local current
    current="$(aws_ ec2 describe-iam-instance-profile-associations \
      --filters "Name=instance-id,Values=${instance}" \
      --query 'IamInstanceProfileAssociations[?State==`associated`].IamInstanceProfile.Arn' \
      --output text)"
    log "current association: ${current}"
    run aws --region "$REGION" ec2 replace-iam-instance-profile-association \
      --association-id "$association" --iam-instance-profile "Name=${PROFILE}"
    cat >&2 <<EOF

  Swapped ${NODE_NAME} onto ${PROFILE}.

  Credentials on the instance refresh within minutes, they are not instant. So
  VERIFY BEFORE WALKING AWAY, because the failure mode is silent:
    - the drain still delivers  (operational_analytics_outbox lag stops growing)
    - the archive can write     (a manual archive_daily run produces a pointer)

  To revert:
    aws --region ${REGION} ec2 replace-iam-instance-profile-association \\
      --association-id ${association} \\
      --iam-instance-profile Name=quill-enclave-instance-profile
EOF
  fi
}

require_node_region
ensure_bucket
ensure_role
ensure_profile
if [ "$ATTACH_PROFILE" -eq 1 ]; then
  attach_profile
else
  log "instance profile NOT attached; re-run with --attach-profile when ready"
fi

if [ "$APPLY" -eq 0 ]; then
  log "dry run only; nothing was changed. re-run with --apply"
fi
