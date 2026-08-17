#!/usr/bin/env bash
# ClickHouse for the AWS-EU cloud: analytics that belong to THIS cloud.
#
# Deliberately INDEPENDENT of the GCP cluster — no cross-cloud replication.
# Each cloud owning its own analytics is the same rule the rest of the
# separation architecture follows, and for the EU deployment it is also the
# thing that lets the EU-only claim survive contact with an auditor:
# operational rows about EU traffic never leave the EU.
#
# The GCP cluster (scripts/deploy/clickhouse_cluster.sh) is 3 GCE nodes with
# a Keeper quorum behind an internal LB. That script is gcloud-coupled at
# every provisioning step, so this is a port of the SHAPE, not a reuse: the
# ClickHouse config and the schema in clickhouse/*.sql are what actually
# carry over.
#
# This is the smallest USEFUL increment: one node, private, reachable only
# from inside the VPC, with the operational schema applied and the control
# plane wired to it. HA (3 nodes + Keeper, mirroring GCP) is the next rung
# and is deliberately not attempted here — a single node that demonstrably
# ingests beats three that are half-wired.
#
# Private on purpose. App Runner egress is DEFAULT (AWS-managed NAT with
# dynamic addresses), so a public ClickHouse could not be restricted by
# source IP and would be protected by a password alone. Instead the service
# gets a VPC connector and ClickHouse listens only inside the VPC.
set -euo pipefail

REGION="${REGION:-eu-west-3}"                  # Paris: same region as tr-eu.
ACCOUNT="${ACCOUNT:-330422590279}"
VPC_ID="${VPC_ID:-vpc-05b829b9cae6a9cd8}"
SUBNET_ID="${SUBNET_ID:-subnet-06e58bd9bca166a94}"
INSTANCE_TYPE="${INSTANCE_TYPE:-m5.large}"     # ClickHouse wants RAM more than cores.
VOLUME_GB="${VOLUME_GB:-100}"
NAME="${NAME:-tr-eu-clickhouse-1}"
SG_NAME="${SG_NAME:-tr-eu-clickhouse-sg}"
SECRET_ID="${SECRET_ID:-quill/tr-eu-clickhouse-password}"
ROLE_NAME="${ROLE_NAME:-tr-eu-clickhouse-role}"
INSTANCE_PROFILE="${INSTANCE_PROFILE:-tr-eu-clickhouse-instance-profile}"
CLUSTER_ID="${CLUSTER_ID:-tnt642i3ofzpn5z62msacutpuu}"   # DSQL, matches the drain installer.

log(){ printf '\n=== %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# 1. Password. Generated once and kept in Secrets Manager, never echoed.
# ---------------------------------------------------------------------------
if aws secretsmanager describe-secret --region "$REGION" --secret-id "$SECRET_ID" >/dev/null 2>&1; then
  log "reusing existing ClickHouse password secret"
else
  log "creating ClickHouse password secret"
  aws secretsmanager create-secret --region "$REGION" --name "$SECRET_ID" \
    --secret-string "$(openssl rand -base64 32 | tr -d '\n/+=' | head -c 40)" >/dev/null
fi
CH_PASSWORD="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" --query SecretString --output text)"

# ---------------------------------------------------------------------------
# 2. Security group: VPC-internal only. No 0.0.0.0/0 on 8123/9000 ever.
# ---------------------------------------------------------------------------
VPC_CIDR="$(aws ec2 describe-vpcs --region "$REGION" --vpc-ids "$VPC_ID" --query 'Vpcs[0].CidrBlock' --output text)"
SG_ID="$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
if [ -z "$SG_ID" ] || [ "$SG_ID" = "None" ]; then
  log "creating security group $SG_NAME (ingress from $VPC_CIDR only)"
  SG_ID="$(aws ec2 create-security-group --region "$REGION" --group-name "$SG_NAME" \
    --description "ClickHouse for the AWS-EU cloud; VPC-internal only" \
    --vpc-id "$VPC_ID" --query GroupId --output text)"
  for PORT in 8123 9000; do
    aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
      --protocol tcp --port "$PORT" --cidr "$VPC_CIDR" >/dev/null
  done
fi
log "security group: $SG_ID"

# ---------------------------------------------------------------------------
# 3. IAM: a least-privilege role for THIS node, created here so it exists.
# ---------------------------------------------------------------------------
# Until 2026-08-17 this script launched the node with
# quill-enclave-instance-profile — the profile the Nitro Enclave hosts use.
# That role grants secretsmanager:GetSecretValue on quill/* and kms:Decrypt on
# arn:aws:kms:*:ACCOUNT:key/*, so an analytics box held every provider API key
# (~40 of them), the Cloudflare token, the cross-cloud SA key, and the ability
# to decrypt with any key in the account including the CloudTrail CMK.
# Thirty days of CloudTrail showed this node using exactly one secret and one
# key. The role below is that observed set and nothing more.
#
# It is created HERE rather than by hand because a control that lives only in
# live cloud state dies with the resource: the role was split by CLI once, and
# the next run of this script would have handed the node the enclave profile
# straight back. Anything this script needs, this script creates.
#
# The KMS key is RESOLVED, not hardcoded — the secret uses the AWS-managed
# aws/secretsmanager key, whose id differs per account and region, so a
# literal arn would silently break the first time this runs anywhere else.
SM_KEY_ARN="$(aws kms describe-key --region "$REGION" --key-id alias/aws/secretsmanager \
  --query 'KeyMetadata.Arn' --output text)"
log "secretsmanager kms key: $SM_KEY_ARN"

ROLE_TRUST="$(cat <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole",
 "Condition":{"StringEqualsIfExists":{"aws:SourceAccount":"$ACCOUNT"}}}]}
JSON
)"
# StringEqualsIfExists, not StringEquals, and deliberately: EC2 instance-profile
# assumption does not populate aws:SourceAccount, so a hard equality fails
# CLOSED — and not at deploy time, but whenever cached IMDS credentials next
# expire, presenting as an unrelated outage. Every other role in this account
# uses hard StringEquals because every other one is a service principal
# (ecs-tasks, apprunner, events, cloudtrail) that does populate it.

ROLE_POLICY="$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"OwnPasswordSecretOnly","Effect":"Allow",
  "Action":["secretsmanager:GetSecretValue","secretsmanager:DescribeSecret"],
  "Resource":"arn:aws:secretsmanager:$REGION:$ACCOUNT:secret:$SECRET_ID-*"},
 {"Sid":"DecryptSecretsManagerKeyOnly","Effect":"Allow",
  "Action":["kms:Decrypt","kms:DescribeKey"],"Resource":"$SM_KEY_ARN"},
 {"Sid":"DsqlOutboxDrain","Effect":"Allow","Action":["dsql:DbConnect"],
  "Resource":"arn:aws:dsql:$REGION:$ACCOUNT:cluster/$CLUSTER_ID"},
 {"Sid":"EcrPullOnly","Effect":"Allow",
  "Action":["ecr:GetAuthorizationToken","ecr:BatchCheckLayerAvailability",
            "ecr:GetDownloadUrlForLayer","ecr:BatchGetImage"],"Resource":"*"},
 {"Sid":"QuillLogs","Effect":"Allow",
  "Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
  "Resource":"arn:aws:logs:$REGION:$ACCOUNT:log-group:/quill/*"}]}
JSON
)"

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  log "reusing role $ROLE_NAME"
else
  log "creating role $ROLE_NAME"
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$ROLE_TRUST" \
    --description "Least-privilege role for $NAME. Split from quill-enclave-role 2026-08-17." >/dev/null
fi
# Policy is put unconditionally: put-role-policy is idempotent and this is what
# makes the script the source of truth rather than a one-time bootstrap. Drift
# applied by hand is corrected on the next run.
aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name "${ROLE_NAME}-policy" --policy-document "$ROLE_POLICY"
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

if aws iam get-instance-profile --instance-profile-name "$INSTANCE_PROFILE" >/dev/null 2>&1; then
  log "reusing instance profile $INSTANCE_PROFILE"
else
  log "creating instance profile $INSTANCE_PROFILE"
  aws iam create-instance-profile --instance-profile-name "$INSTANCE_PROFILE" >/dev/null
fi
if ! aws iam get-instance-profile --instance-profile-name "$INSTANCE_PROFILE" \
     --query 'InstanceProfile.Roles[].RoleName' --output text | grep -qw "$ROLE_NAME"; then
  aws iam add-role-to-instance-profile \
    --instance-profile-name "$INSTANCE_PROFILE" --role-name "$ROLE_NAME"
fi
log "instance profile ready: $INSTANCE_PROFILE -> $ROLE_NAME"

# ---------------------------------------------------------------------------
# 4. The node. user-data installs ClickHouse and binds it to the private IP.
# ---------------------------------------------------------------------------
EXISTING="$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running,pending" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || true)"

if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  log "reusing running instance $EXISTING"
  INSTANCE_ID="$EXISTING"

  # Converge the profile on an instance that already exists, so this script is
  # the source of truth for a NODE and not merely for a launch. Without this,
  # every node provisioned before 2026-08-17 keeps the enclave profile forever
  # and the fix only applies to hosts nobody has built yet.
  #
  # Safe to do online: replace-iam-instance-profile-association needs no reboot,
  # and the instance keeps its CURRENT credentials until they expire (up to ~6h),
  # so nothing breaks at the moment of the swap. That same property is why a
  # BROKEN role here would fail late — hence the role above grants the set that
  # CloudTrail shows this node actually using, rather than a guess.
  CUR_ASSOC="$(aws ec2 describe-iam-instance-profile-associations --region "$REGION" \
    --filters "Name=instance-id,Values=$INSTANCE_ID" \
    --query 'IamInstanceProfileAssociations[?State==`associated`].[AssociationId,IamInstanceProfile.Arn]' \
    --output text 2>/dev/null || true)"
  CUR_ID="$(printf '%s\n' "$CUR_ASSOC" | awk 'NR==1{print $1}')"
  CUR_ARN="$(printf '%s\n' "$CUR_ASSOC" | awk 'NR==1{print $2}')"
  if [ -z "$CUR_ID" ]; then
    log "attaching instance profile $INSTANCE_PROFILE to $INSTANCE_ID"
    aws ec2 associate-iam-instance-profile --region "$REGION" \
      --instance-id "$INSTANCE_ID" \
      --iam-instance-profile Name="$INSTANCE_PROFILE" >/dev/null
  elif [ "${CUR_ARN##*/}" != "$INSTANCE_PROFILE" ]; then
    log "replacing instance profile ${CUR_ARN##*/} -> $INSTANCE_PROFILE on $INSTANCE_ID"
    aws ec2 replace-iam-instance-profile-association --region "$REGION" \
      --association-id "$CUR_ID" \
      --iam-instance-profile Name="$INSTANCE_PROFILE" >/dev/null
  else
    log "instance profile already $INSTANCE_PROFILE"
  fi
else
  AMI="$(aws ssm get-parameter --region "$REGION" \
    --name /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id \
    --query Parameter.Value --output text)"
  log "launching $NAME from $AMI"
  USER_DATA="$(cat <<USERDATA
#!/bin/bash
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y apt-transport-https ca-certificates dirmngr gnupg curl

GNUPGHOME=\$(mktemp -d)
GNUPGHOME=\$GNUPGHOME gpg --no-default-keyring --keyring /usr/share/keyrings/clickhouse-keyring.gpg \
  --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys 3a9ea1193a97b548be1457d48919f6bd2b48d754
chmod +r /usr/share/keyrings/clickhouse-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg] https://packages.clickhouse.com/deb stable main" \
  > /etc/apt/sources.list.d/clickhouse.list
apt-get update -qq

# Non-interactive install; the default user password is set below.
echo "clickhouse-server clickhouse-server/default-password password" | debconf-set-selections
apt-get install -y clickhouse-server clickhouse-client

PRIVATE_IP=\$(curl -s -H "X-aws-ec2-metadata-token: \$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/local-ipv4)

# Listen on the private IP only. 0.0.0.0 plus a permissive SG is how an
# analytics store ends up on the public internet.
cat > /etc/clickhouse-server/config.d/listen.xml <<XML
<clickhouse>
  <listen_host>\${PRIVATE_IP}</listen_host>
  <listen_host>127.0.0.1</listen_host>
</clickhouse>
XML

cat > /etc/clickhouse-server/users.d/default-password.xml <<XML
<clickhouse>
  <users>
    <default>
      <password>${CH_PASSWORD}</password>
      <networks><ip>${VPC_CIDR}</ip><ip>127.0.0.1</ip></networks>
    </default>
  </users>
</clickhouse>
XML
# Owned by clickhouse, not root: the server drops privileges to the
# clickhouse user, so a root-owned 600 file is unreadable to it and the
# process dies in UsersConfigAccessStorage::load with a stack trace that
# never names the permission. Cost one debugging cycle.
chown clickhouse:clickhouse /etc/clickhouse-server/users.d/default-password.xml
chmod 640 /etc/clickhouse-server/users.d/default-password.xml

systemctl enable clickhouse-server
systemctl restart clickhouse-server
USERDATA
)"
  INSTANCE_ID="$(aws ec2 run-instances --region "$REGION" \
    --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
    --subnet-id "$SUBNET_ID" --security-group-ids "$SG_ID" \
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$VOLUME_GB,VolumeType=gp3,DeleteOnTermination=true}" \
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
    --iam-instance-profile Name="$INSTANCE_PROFILE" \
    --user-data "$USER_DATA" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=Project,Value=tr-eu-analytics}]" \
    --query 'Instances[0].InstanceId' --output text)"
fi

aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"
PRIVATE_IP="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)"
log "instance $INSTANCE_ID at $PRIVATE_IP"

# ---------------------------------------------------------------------------
# 5. App Runner VPC connector, so tr-eu can reach a PRIVATE ClickHouse.
# ---------------------------------------------------------------------------
CONNECTOR_ARN="$(aws apprunner list-vpc-connectors --region "$REGION" \
  --query "VpcConnectors[?VpcConnectorName=='tr-eu-vpc' && Status=='ACTIVE'].VpcConnectorArn | [0]" \
  --output text 2>/dev/null || true)"
if [ -z "$CONNECTOR_ARN" ] || [ "$CONNECTOR_ARN" = "None" ]; then
  log "creating App Runner VPC connector"
  CONNECTOR_ARN="$(aws apprunner create-vpc-connector --region "$REGION" \
    --vpc-connector-name tr-eu-vpc \
    --subnets "$SUBNET_ID" --security-groups "$SG_ID" \
    --query 'VpcConnector.VpcConnectorArn' --output text)"
fi
log "vpc connector: $CONNECTOR_ARN"

echo
echo "CLICKHOUSE_PRIVATE_URL=http://${PRIVATE_IP}:8123"
echo "VPC_CONNECTOR_ARN=${CONNECTOR_ARN}"
echo "INSTANCE_ID=${INSTANCE_ID}"
echo
echo "Next: apply clickhouse/*.sql, then redeploy tr-eu with"
echo "  TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL=http://${PRIVATE_IP}:8123"
echo "  EgressConfiguration=VPC + the connector above."
