#!/usr/bin/env bash
# SECOND ClickHouse node for the AWS cloud: Stockholm (eu-north-1).
#
# Sibling of scripts/deploy/aws_eu_clickhouse.sh, which built the Paris node.
# Read that one first — every hard-won detail in it is repeated here on
# purpose (VPC-only security group, private listen address, IMDSv2, and the
# chown clickhouse:clickhouse on the users.d file that cost a debugging cycle).
#
# WHY THIS EXISTS -- durability, not availability
# ----------------------------------------------
# The gateway never touches ClickHouse, and the analytics write is a durable
# outbox row inside the same DSQL transaction as the generation. So a
# ClickHouse outage costs nothing but freshness. What a single node DOES risk
# is everything: one EBS volume in one AZ currently holds every operational
# row this cloud has ever produced, and losing it loses the history. This node
# is a second, independent copy of that history.
#
# WHY A SEPARATE VPC, IN A SEPARATE REGION
# ----------------------------------------
# A second node inside the Paris VPC would share its route tables, its NAT, its
# security-group blast radius and its region -- reintroducing the single
# failure domain the second copy exists to escape. So this is its own VPC, its
# own CIDR, its own subnet and its own security group in eu-north-1, connected
# to Paris by an inter-region Transit Gateway peering rather than by being part
# of the same network. Both stay inside the EU, which the EU-only claim needs.
#
# WHY NOT CLICKHOUSE KEEPER / ReplicatedReplacingMergeTree
# --------------------------------------------------------
# That is what the GCP cluster runs (scripts/deploy/clickhouse_cluster.sh),
# on a THREE-node Keeper quorum. Across exactly two regions a quorum is worse
# than useless: two Keeper members cannot form a majority when either one dies,
# so losing a region would freeze writes on the survivor and turn a durability
# improvement into an availability regression. Both AWS nodes therefore run
# plain ReplacingMergeTree and know nothing about each other.
#
# They are kept in step by the drain instead
# (clickhouse/ingest_operational_outbox_postgres.py), which extends the
# at-least-once property it already had:
#
#     SELECT batch -> write Paris -> write Stockholm -> DELETE outbox rows
#
# with the DELETE gated on BOTH writes. If either node is unreachable the rows
# stay queued and redeliver; ReplacingMergeTree on ingest_version collapses the
# duplicate that redelivery creates. The failure mode is "the outbox grows",
# not "data is lost".
#
# DO NOT instead run a second drain process on this node against the same
# outbox. Two drains would each DELETE rows the other had not yet written, and
# every row would land on exactly one node -- which looks like replication and
# is the precise opposite of it.
set -euo pipefail

REGION="${REGION:-eu-north-1}"                     # Stockholm.
PARIS_REGION="${PARIS_REGION:-eu-west-3}"
PARIS_VPC_ID="${PARIS_VPC_ID:-vpc-05b829b9cae6a9cd8}"
PARIS_NODE_NAME="${PARIS_NODE_NAME:-tr-eu-clickhouse-1}"   # Where the drain runs.
ACCOUNT="${ACCOUNT:-330422590279}"
VPC_CIDR="${VPC_CIDR:-10.60.0.0/16}"               # Must not overlap Paris.
SUBNET_CIDR="${SUBNET_CIDR:-10.60.1.0/24}"
INSTANCE_TYPE="${INSTANCE_TYPE:-m5.large}"         # Match Paris.
VOLUME_GB="${VOLUME_GB:-100}"
NAME="${NAME:-tr-eu-north-clickhouse-1}"
VPC_NAME="${VPC_NAME:-tr-eu-north-analytics}"
SG_NAME="${SG_NAME:-tr-eu-north-clickhouse-sg}"
SECRET_ID="${SECRET_ID:-quill/tr-eu-north-clickhouse-password}"
INSTANCE_PROFILE="${INSTANCE_PROFILE:-quill-enclave-instance-profile}"
PEER_WITH_PARIS="${PEER_WITH_PARIS:-1}"            # 0 to bring your own path.
SCHEMA_FILE="${SCHEMA_FILE:-$(dirname "$0")/../../clickhouse/006_operational_analytics_single_node.sql}"
CLIENT_SCHEMA_FILE="${CLIENT_SCHEMA_FILE:-$(dirname "$0")/../../clickhouse/009_client_events_single_node.sql}"

log(){ printf '\n=== %s\n' "$*" >&2; }

# Creating a route that is already there is success, not an error -- this
# script is re-run. Every OTHER failure is fatal, deliberately: a `|| true` on
# route creation converts "the route was never installed" into a silent
# blackhole, which is the single most likely way this network ends up built,
# reported healthy, and unable to pass a packet.
route_exists_or_created(){  # region route-table-id cidr flag value
  local out
  if out="$(aws ec2 create-route --region "$1" --route-table-id "$2" \
      --destination-cidr-block "$3" "$4" "$5" 2>&1)"; then
    return 0
  fi
  case "$out" in
    *RouteAlreadyExists*) return 0 ;;
    *) echo "$out" >&2; return 1 ;;
  esac
}

tgw_route_exists_or_created(){  # region route-table-id cidr attachment-id
  local out
  if out="$(aws ec2 create-transit-gateway-route --region "$1" \
      --transit-gateway-route-table-id "$2" --destination-cidr-block "$3" \
      --transit-gateway-attachment-id "$4" 2>&1)"; then
    return 0
  fi
  case "$out" in
    *RouteAlreadyExists*|*DuplicateTransitGatewayRoute*) return 0 ;;
    *) echo "$out" >&2; return 1 ;;
  esac
}

# There is no `aws ec2 wait transit-gateway-available` and no waiter for any
# transit-gateway resource -- checked against the CLI's own waiter model, not
# assumed. So the polling is written out.
await_state(){  # description attempts command...
  local what="$1" attempts="$2" state=""
  shift 2
  for _ in $(seq 1 "$attempts"); do
    state="$("$@" 2>/dev/null || echo pending)"
    [ "$state" = "available" ] && { log "$what is available"; return 0; }
    sleep 10
  done
  echo "$what did not become available (state=$state)" >&2
  return 1
}

# ---------------------------------------------------------------------------
# 0. Preflight. Two mistakes here are unrecoverable later, so both are checked
#    before anything is created.
# ---------------------------------------------------------------------------
if [ "$REGION" = "$PARIS_REGION" ]; then
  echo "REGION and PARIS_REGION are both $REGION; a second copy in the same region is not a second failure domain" >&2
  exit 1
fi
[ -r "$SCHEMA_FILE" ] || { echo "schema file not readable: $SCHEMA_FILE" >&2; exit 1; }
[ -r "$CLIENT_SCHEMA_FILE" ] || { echo "schema file not readable: $CLIENT_SCHEMA_FILE" >&2; exit 1; }

PARIS_VPC_CIDR="$(aws ec2 describe-vpcs --region "$PARIS_REGION" --vpc-ids "$PARIS_VPC_ID" \
  --query 'Vpcs[0].CidrBlock' --output text)"
log "Paris VPC $PARIS_VPC_ID is $PARIS_VPC_CIDR; this VPC will be $VPC_CIDR"

# Overlapping CIDRs cannot be routed to each other across a Transit Gateway,
# and the failure is a silent blackhole route rather than an error at creation
# time. Refuse now, while VPC_CIDR is still just a variable.
python3 - "$PARIS_VPC_CIDR" "$VPC_CIDR" <<'PY'
import ipaddress
import sys

paris, stockholm = (ipaddress.ip_network(arg) for arg in sys.argv[1:3])
if paris.overlaps(stockholm):
    sys.exit(
        f"VPC_CIDR {stockholm} overlaps the Paris VPC {paris}; "
        "a Transit Gateway cannot route between overlapping CIDRs. "
        "Set VPC_CIDR to a disjoint range."
    )
PY

# ---------------------------------------------------------------------------
# 1. Password. Its OWN secret, in THIS region. Secrets Manager is regional, so
#    reusing the Paris secret would make an eu-west-3 outage the thing that
#    stops the Stockholm node from being provisioned or rebuilt -- a shared
#    dependency inside a design whose whole point is not sharing one.
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
# 2. The VPC, subnet, and its route to the internet.
#
#    The instance gets a public IP for one reason: apt must reach
#    packages.clickhouse.com to install ClickHouse at all, and a NAT gateway is
#    a recurring cost and an extra provisioning dependency for a node that
#    receives one TCP stream a second from one peer. Nothing is exposed by it,
#    and that rests on TWO independent controls, not one:
#      (a) ClickHouse binds the PRIVATE address only (see user-data below), so
#          the public interface has nothing listening on 8123/9000; and
#      (b) the security group admits only the Paris VPC CIDR, on those two
#          ports, with no 0.0.0.0/0 rule anywhere -- including no SSH.
# ---------------------------------------------------------------------------
VPC_ID="$(aws ec2 describe-vpcs --region "$REGION" \
  --filters "Name=tag:Name,Values=$VPC_NAME" "Name=state,Values=available" \
  --query 'Vpcs[0].VpcId' --output text 2>/dev/null || true)"
if [ -z "$VPC_ID" ] || [ "$VPC_ID" = "None" ]; then
  log "creating VPC $VPC_NAME ($VPC_CIDR)"
  VPC_ID="$(aws ec2 create-vpc --region "$REGION" --cidr-block "$VPC_CIDR" \
    --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=$VPC_NAME},{Key=Project,Value=tr-eu-analytics}]" \
    --query 'Vpc.VpcId' --output text)"
  aws ec2 wait vpc-available --region "$REGION" --vpc-ids "$VPC_ID"
  aws ec2 modify-vpc-attribute --region "$REGION" --vpc-id "$VPC_ID" --enable-dns-hostnames >/dev/null
fi
log "vpc: $VPC_ID"

SUBNET_ID="$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:Name,Values=$VPC_NAME-a" \
  --query 'Subnets[0].SubnetId' --output text 2>/dev/null || true)"
if [ -z "$SUBNET_ID" ] || [ "$SUBNET_ID" = "None" ]; then
  log "creating subnet $SUBNET_CIDR"
  SUBNET_ID="$(aws ec2 create-subnet --region "$REGION" --vpc-id "$VPC_ID" \
    --cidr-block "$SUBNET_CIDR" \
    --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=$VPC_NAME-a}]" \
    --query 'Subnet.SubnetId' --output text)"
fi
# Public address assigned by the SUBNET rather than by run-instances'
# --associate-public-ip-address: that flag makes the CLI synthesise a
# NetworkInterfaces block, which then conflicts with the top-level --subnet-id
# and --security-group-ids this script passes. Setting it here keeps the launch
# call plain.
aws ec2 modify-subnet-attribute --region "$REGION" --subnet-id "$SUBNET_ID" \
  --map-public-ip-on-launch >/dev/null
log "subnet: $SUBNET_ID"

IGW_ID="$(aws ec2 describe-internet-gateways --region "$REGION" \
  --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
  --query 'InternetGateways[0].InternetGatewayId' --output text 2>/dev/null || true)"
if [ -z "$IGW_ID" ] || [ "$IGW_ID" = "None" ]; then
  log "creating internet gateway"
  IGW_ID="$(aws ec2 create-internet-gateway --region "$REGION" \
    --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=$VPC_NAME}]" \
    --query 'InternetGateway.InternetGatewayId' --output text)"
  aws ec2 attach-internet-gateway --region "$REGION" --vpc-id "$VPC_ID" --internet-gateway-id "$IGW_ID" >/dev/null
fi

ROUTE_TABLE_ID="$(aws ec2 describe-route-tables --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=association.main,Values=true" \
  --query 'RouteTables[0].RouteTableId' --output text)"
route_exists_or_created "$REGION" "$ROUTE_TABLE_ID" 0.0.0.0/0 --gateway-id "$IGW_ID" >/dev/null
log "route table: $ROUTE_TABLE_ID (igw $IGW_ID)"

# ---------------------------------------------------------------------------
# 3. Security group. Ingress from the PARIS VPC only -- the drain host is the
#    single legitimate client. Same rule as the Paris node's "VPC-internal
#    only", generalised to the one remote network that has business here.
#    No 0.0.0.0/0 on 8123/9000. Ever. And no SSH: use SSM Session Manager.
# ---------------------------------------------------------------------------
SG_ID="$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
if [ -z "$SG_ID" ] || [ "$SG_ID" = "None" ]; then
  log "creating security group $SG_NAME (ingress from $PARIS_VPC_CIDR only)"
  SG_ID="$(aws ec2 create-security-group --region "$REGION" --group-name "$SG_NAME" \
    --description "ClickHouse durability replica for the AWS-EU cloud; Paris VPC only" \
    --vpc-id "$VPC_ID" --query GroupId --output text)"
  for PORT in 8123 9000; do
    aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
      --protocol tcp --port "$PORT" --cidr "$PARIS_VPC_CIDR" >/dev/null
  done
fi
log "security group: $SG_ID"

# ---------------------------------------------------------------------------
# 4. The node. user-data installs ClickHouse, binds it to the private IP, and
#    applies the standalone operational schema so the node is COMPLETE when it
#    finishes booting -- the drain can be pointed at it without a second
#    manual step that someone forgets.
# ---------------------------------------------------------------------------
OPERATIONAL_SCHEMA="$(cat "$SCHEMA_FILE")"
CLIENT_SCHEMA="$(cat "$CLIENT_SCHEMA_FILE")"

EXISTING="$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running,pending" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || true)"

if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  log "reusing running instance $EXISTING"
  INSTANCE_ID="$EXISTING"
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

# IMDSv2: the token PUT is required, so a stolen SSRF primitive cannot read
# instance metadata with a bare GET.
PRIVATE_IP=\$(curl -s -H "X-aws-ec2-metadata-token: \$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/local-ipv4)

# Listen on the private IP only. This instance HAS a public address (it needs
# egress to install ClickHouse), so binding 0.0.0.0 here would put an analytics
# store on the public internet behind nothing but a password.
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
      <networks><ip>${PARIS_VPC_CIDR}</ip><ip>${VPC_CIDR}</ip><ip>127.0.0.1</ip></networks>
    </default>
  </users>
</clickhouse>
XML
# Owned by clickhouse, not root: the server drops privileges to the
# clickhouse user, so a root-owned 600 file is unreadable to it and the
# process dies in UsersConfigAccessStorage::load with a stack trace that
# never names the permission. Cost one debugging cycle on the Paris node;
# it is repeated here rather than rediscovered.
chown clickhouse:clickhouse /etc/clickhouse-server/users.d/default-password.xml
chmod 640 /etc/clickhouse-server/users.d/default-password.xml

systemctl enable clickhouse-server
systemctl restart clickhouse-server

# Apply the standalone operational schema. Plain ReplacingMergeTree, no
# Keeper: this node does not replicate with Paris, the drain writes both.
cat > /root/operational_schema.sql <<'SQLEOF'
${OPERATIONAL_SCHEMA}
${CLIENT_SCHEMA}
SQLEOF
for attempt in \$(seq 1 60); do
  if CLICKHOUSE_PASSWORD='${CH_PASSWORD}' clickhouse-client --user default --database default --query 'SELECT 1' >/dev/null 2>&1; then
    break
  fi
  sleep 5
done
CLICKHOUSE_PASSWORD='${CH_PASSWORD}' clickhouse-client --user default --database default \
  --multiquery < /root/operational_schema.sql
rm -f /root/operational_schema.sql
USERDATA
)"
  INSTANCE_ID="$(aws ec2 run-instances --region "$REGION" \
    --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
    --subnet-id "$SUBNET_ID" --security-group-ids "$SG_ID" \
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$VOLUME_GB,VolumeType=gp3,DeleteOnTermination=false}" \
    --disable-api-termination \
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
    --iam-instance-profile "Name=$INSTANCE_PROFILE" \
    --user-data "$USER_DATA" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=Project,Value=tr-eu-analytics}]" \
    --query 'Instances[0].InstanceId' --output text)"
fi

# DeleteOnTermination=false and --disable-api-termination above are the whole
# point of this node stated as API flags: the second copy of the history must
# not be destroyed by terminating an instance. Mirrors the GCP node's
# --no-boot-disk-auto-delete + --deletion-protection.

aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"
PRIVATE_IP="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)"
log "instance $INSTANCE_ID at $PRIVATE_IP"

# ---------------------------------------------------------------------------
# 5. Inter-region path, so the drain running on the PARIS node can reach this
#    one. Transit Gateway peering, not VPC peering and not a shared VPC: each
#    side keeps its own VPC, CIDR, route table and security group, and the
#    traffic stays on the AWS backbone instead of traversing the internet.
#
#    A TGW outage stops the WRITE to this node. That is the designed-for
#    failure: the drain does not delete, the outbox grows, and delivery
#    resumes. It is not data loss.
#
#    NOTE: TGW route tables propagate VPC attachments automatically but NOT
#    peering attachments, which need static routes. That asymmetry is the
#    usual reason a freshly built peering blackholes.
# ---------------------------------------------------------------------------
if [ "$PEER_WITH_PARIS" = "1" ]; then
  ensure_tgw(){  # region, name -> transit gateway id on stdout
    local rgn="$1" name="$2" id
    id="$(aws ec2 describe-transit-gateways --region "$rgn" \
      --filters "Name=tag:Name,Values=$name" "Name=state,Values=available,pending" \
      --query 'TransitGateways[0].TransitGatewayId' --output text 2>/dev/null || true)"
    if [ -z "$id" ] || [ "$id" = "None" ]; then
      id="$(aws ec2 create-transit-gateway --region "$rgn" \
        --description "TrustedRouter analytics inter-region path" \
        --tag-specifications "ResourceType=transit-gateway,Tags=[{Key=Name,Value=$name}]" \
        --query 'TransitGateway.TransitGatewayId' --output text)"
    fi
    printf '%s' "$id"
  }
  ensure_attachment(){  # region, tgw, vpc, subnet -> attachment id on stdout
    local rgn="$1" tgw="$2" vpc="$3" subnet="$4" id
    id="$(aws ec2 describe-transit-gateway-vpc-attachments --region "$rgn" \
      --filters "Name=transit-gateway-id,Values=$tgw" "Name=vpc-id,Values=$vpc" \
                "Name=state,Values=available,pending" \
      --query 'TransitGatewayVpcAttachments[0].TransitGatewayAttachmentId' --output text 2>/dev/null || true)"
    if [ -z "$id" ] || [ "$id" = "None" ]; then
      id="$(aws ec2 create-transit-gateway-vpc-attachment --region "$rgn" \
        --transit-gateway-id "$tgw" --vpc-id "$vpc" --subnet-ids "$subnet" \
        --query 'TransitGatewayVpcAttachment.TransitGatewayAttachmentId' --output text)"
    fi
    printf '%s' "$id"
  }

  log "ensuring transit gateways in $PARIS_REGION and $REGION"
  PARIS_TGW="$(ensure_tgw "$PARIS_REGION" tr-analytics-tgw)"
  NORTH_TGW="$(ensure_tgw "$REGION" tr-analytics-tgw)"
  await_state "transit gateway $PARIS_TGW" 60 \
    aws ec2 describe-transit-gateways --region "$PARIS_REGION" --transit-gateway-ids "$PARIS_TGW" \
    --query 'TransitGateways[0].State' --output text
  await_state "transit gateway $NORTH_TGW" 60 \
    aws ec2 describe-transit-gateways --region "$REGION" --transit-gateway-ids "$NORTH_TGW" \
    --query 'TransitGateways[0].State' --output text

  # The subnet the PARIS CLICKHOUSE NODE is actually in -- not `Subnets[0]`.
  # This one value picks both the TGW VPC attachment and (below) the route
  # table that receives the return route, and `describe-subnets` has no defined
  # ordering while a VPC has one subnet per AZ. Two silent blackholes follow
  # from getting it wrong, and the script would print "inter-region path ready"
  # for both:
  #   * a TGW VPC attachment only carries traffic for AZs in which it has a
  #     subnet/ENI, so an attachment in the wrong AZ drops the drain's packets;
  #   * the 10.60.0.0/16 route lands in whichever route table that subnet is
  #     associated with, which need not be the ClickHouse subnet's.
  # So it is derived from the instance that runs the drain, by tag, and a
  # failure to find it stops the script instead of guessing.
  if [ -z "${PARIS_SUBNET_ID:-}" ]; then
    PARIS_SUBNET_ID="$(aws ec2 describe-instances --region "$PARIS_REGION" \
      --filters "Name=tag:Name,Values=$PARIS_NODE_NAME" \
                "Name=instance-state-name,Values=running,pending" \
      --query 'Reservations[0].Instances[0].SubnetId' --output text 2>/dev/null || true)"
  fi
  [ -n "$PARIS_SUBNET_ID" ] && [ "$PARIS_SUBNET_ID" != "None" ] || {
    echo "could not find the subnet of the Paris ClickHouse node (tag Name=$PARIS_NODE_NAME) in $PARIS_REGION." >&2
    echo "The inter-region path must be built against the subnet the DRAIN runs in:" >&2
    echo "an attachment or route in any other subnet is a blackhole that reports healthy." >&2
    echo "Set PARIS_SUBNET_ID explicitly if the node is tagged differently." >&2
    exit 1; }
  PARIS_SUBNET_VPC="$(aws ec2 describe-subnets --region "$PARIS_REGION" \
    --subnet-ids "$PARIS_SUBNET_ID" --query 'Subnets[0].VpcId' --output text)"
  [ "$PARIS_SUBNET_VPC" = "$PARIS_VPC_ID" ] || {
    echo "PARIS_SUBNET_ID $PARIS_SUBNET_ID is in $PARIS_SUBNET_VPC, not $PARIS_VPC_ID" >&2; exit 1; }
  log "attaching VPCs (paris subnet $PARIS_SUBNET_ID)"
  PARIS_ATTACHMENT="$(ensure_attachment "$PARIS_REGION" "$PARIS_TGW" "$PARIS_VPC_ID" "$PARIS_SUBNET_ID")"
  NORTH_ATTACHMENT="$(ensure_attachment "$REGION" "$NORTH_TGW" "$VPC_ID" "$SUBNET_ID")"

  # Selected by JMESPath over both TGW ids rather than by a describe --filters
  # name. The peering-attachment filter vocabulary is the one thing in this
  # section that could not be checked against the CLI's own model offline, so
  # it is simply not depended on.
  PEERING_ID="$(aws ec2 describe-transit-gateway-peering-attachments --region "$PARIS_REGION" \
    --query "TransitGatewayPeeringAttachments[?RequesterTgwInfo.TransitGatewayId=='$PARIS_TGW' \
             && AccepterTgwInfo.TransitGatewayId=='$NORTH_TGW' \
             && State!='deleted' && State!='failed' && State!='rejected'] \
             | [0].TransitGatewayAttachmentId" --output text 2>/dev/null || true)"
  if [ -z "$PEERING_ID" ] || [ "$PEERING_ID" = "None" ]; then
    log "creating transit gateway peering $PARIS_REGION -> $REGION"
    PEERING_ID="$(aws ec2 create-transit-gateway-peering-attachment --region "$PARIS_REGION" \
      --transit-gateway-id "$PARIS_TGW" --peer-transit-gateway-id "$NORTH_TGW" \
      --peer-account-id "$ACCOUNT" --peer-region "$REGION" \
      --query 'TransitGatewayPeeringAttachment.TransitGatewayAttachmentId' --output text)"
  fi
  # Same account on both sides, so we accept our own request; the attachment id
  # is the same on both ends. Already-accepted is not an error worth stopping
  # for, which is why this one call tolerates failure -- unlike the routes.
  aws ec2 accept-transit-gateway-peering-attachment --region "$REGION" \
    --transit-gateway-attachment-id "$PEERING_ID" >/dev/null 2>&1 || true
  await_state "peering attachment $PEERING_ID" 60 \
    aws ec2 describe-transit-gateway-peering-attachments --region "$REGION" \
    --transit-gateway-attachment-ids "$PEERING_ID" \
    --query 'TransitGatewayPeeringAttachments[0].State' --output text

  # A VPC attachment takes minutes to become available, and a route created
  # against a pending attachment fails. Wait for both before routing anything.
  await_state "paris vpc attachment $PARIS_ATTACHMENT" 60 \
    aws ec2 describe-transit-gateway-vpc-attachments --region "$PARIS_REGION" \
    --transit-gateway-attachment-ids "$PARIS_ATTACHMENT" \
    --query 'TransitGatewayVpcAttachments[0].State' --output text
  await_state "stockholm vpc attachment $NORTH_ATTACHMENT" 60 \
    aws ec2 describe-transit-gateway-vpc-attachments --region "$REGION" \
    --transit-gateway-attachment-ids "$NORTH_ATTACHMENT" \
    --query 'TransitGatewayVpcAttachments[0].State' --output text

  # Static routes across the peering, in BOTH TGW route tables. A TGW route
  # table propagates VPC attachments automatically but NOT peering attachments,
  # so without these two the peering is available and every packet is dropped.
  for SIDE in paris north; do
    if [ "$SIDE" = paris ]; then
      RGN="$PARIS_REGION"; TGW="$PARIS_TGW"; DEST="$VPC_CIDR"
    else
      RGN="$REGION"; TGW="$NORTH_TGW"; DEST="$PARIS_VPC_CIDR"
    fi
    TGW_RT="$(aws ec2 describe-transit-gateways --region "$RGN" --transit-gateway-ids "$TGW" \
      --query 'TransitGateways[0].Options.AssociationDefaultRouteTableId' --output text)"
    tgw_route_exists_or_created "$RGN" "$TGW_RT" "$DEST" "$PEERING_ID" >/dev/null
  done

  # And the VPC subnet route tables, which know nothing about the TGW yet.
  route_exists_or_created "$REGION" "$ROUTE_TABLE_ID" "$PARIS_VPC_CIDR" \
    --transit-gateway-id "$NORTH_TGW" >/dev/null
  PARIS_RT="$(aws ec2 describe-route-tables --region "$PARIS_REGION" \
    --filters "Name=vpc-id,Values=$PARIS_VPC_ID" "Name=association.subnet-id,Values=$PARIS_SUBNET_ID" \
    --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null || true)"
  if [ -z "$PARIS_RT" ] || [ "$PARIS_RT" = "None" ]; then
    PARIS_RT="$(aws ec2 describe-route-tables --region "$PARIS_REGION" \
      --filters "Name=vpc-id,Values=$PARIS_VPC_ID" "Name=association.main,Values=true" \
      --query 'RouteTables[0].RouteTableId' --output text)"
  fi
  route_exists_or_created "$PARIS_REGION" "$PARIS_RT" "$VPC_CIDR" \
    --transit-gateway-id "$PARIS_TGW" >/dev/null
  log "inter-region path ready ($PARIS_VPC_CIDR <-> $VPC_CIDR)"
else
  log "PEER_WITH_PARIS=0: skipping the inter-region path. The drain cannot reach this node until one exists."
fi

# ---------------------------------------------------------------------------
# 6. What the operator does next. The password is never printed; it is fetched
#    from Secrets Manager into the drain's environment file, which is 0600 and
#    the only place it belongs.
# ---------------------------------------------------------------------------
echo
echo "INSTANCE_ID=${INSTANCE_ID}"
echo "PRIVATE_IP=${PRIVATE_IP}"
echo "VPC_ID=${VPC_ID}"
echo "SECRET_ID=${SECRET_ID} (region ${REGION})"
echo
echo "Next, on the PARIS ClickHouse node (where the drain runs), append these"
echo "five literals to /etc/tr-clickhouse-ingest-postgres.env:"
echo
echo "  TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_NAME=stockholm"
echo "  TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_HOST=${PRIVATE_IP}"
echo "  TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_PORT=9000"
echo "  TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_USER=default"
echo "  TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_REPLICA_DATABASE=default"
echo
echo "and add the password by RUNNING this, rather than by pasting it:"
echo
echo "  umask 077 && printf 'CH_REPLICA_PASSWORD=%s\\n' \\"
echo "    \"\$(aws secretsmanager get-secret-value --region ${REGION} \\"
echo "        --secret-id ${SECRET_ID} --query SecretString --output text)\" \\"
echo "    >> /etc/tr-clickhouse-ingest-postgres.env"
echo
echo "That file is a systemd EnvironmentFile, which performs NO command or"
echo "variable substitution: a literal \$(aws ...) written into it becomes the"
echo "password, is non-empty so every startup check passes, and then fails"
echo "authentication on every insert forever while the outbox grows."
echo
echo "then: systemctl restart tr-clickhouse-operational-ingest-postgres.service"
echo
echo "Verify BEFORE trusting the second copy -- from the Paris node:"
echo "  clickhouse-client --host ${PRIVATE_IP} --user default --database default --query 'SELECT count() FROM activity_generations'"
echo "and watch the drain log for 'copies=2 degraded_targets=-'. A nonempty"
echo "degraded_targets means rows are accumulating in the outbox, not lost."
echo
echo "This node starts EMPTY. It holds only rows drained after it was wired in;"
echo "history already deleted from the outbox is not backfilled by adding it."
echo
echo "To copy the existing history across, run this ON THE PARIS NODE -- it"
echo "PUSHES to Stockholm. The reverse (a pull from Stockholm) cannot connect:"
echo "the Paris security group admits only the Paris VPC CIDR, and the Paris"
echo "ClickHouse 'default' user is pinned to <networks>Paris-VPC + 127.0.0.1</networks>,"
echo "so Stockholm is refused at both layers. Only Paris->Stockholm is open."
echo
echo "  clickhouse-client --user default --database default --query \\"
echo "    \"INSERT INTO FUNCTION remote('${PRIVATE_IP}:9000','default','activity_generations','default','<stockholm password>') \\"
echo "     SELECT * FROM activity_generations\""
echo
echo "and again for synthetic_probe_samples, client_request_events, and"
echo "client_minute_counters. ingest_version is carried through,"
echo "so re-running it collapses instead of double-counting."
echo
echo "COVERAGE: the drain replicates activity_generations,"
echo "synthetic_probe_samples, client_request_events, client_minute_counters,"
echo "and operational_outbox_quarantine. It does NOT write"
echo "synthetic_status_rollups, client_availability_rollups, or"
echo "public_analytics_snapshots; on this cloud nothing does today"
echo "(those are GCP timers), so both nodes hold them empty."
echo "If a rollup or snapshot job is ever run against Paris, its output is NOT"
echo "on this node and this node is not a complete copy until it is."

# ---------------------------------------------------------------------------
# The exit code, which is the only part of the above a pipeline can read.
#
# Everything printed so far is a HUMAN step: it needs a shell on the Paris node
# and the Stockholm password. Printing it and returning 0 is exactly how the
# Paris drain came to not exist for fifteen days — a script said its piece,
# exited successfully, and nothing anywhere disagreed. So this ends by
# checking the cloud, and then by refusing to claim the replica is wired until
# somebody says it is.
#
# TR_STOCKHOLM_REPLICA_WIRED=1 is that attestation. It is deliberately a
# statement an operator makes AFTER watching the drain log 'copies=2
# degraded_targets=-', not something this script can infer: from here, a
# second node that is provisioned and a second node that is receiving rows
# look identical.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/cloud_complete_gate.sh
. "${SCRIPT_DIR}/cloud_complete_gate.sh"

COMPLETE=0
require_cloud_complete aws || COMPLETE=$?

if [ "${TR_STOCKHOLM_REPLICA_WIRED:-0}" != "1" ]; then
  cat >&2 <<NEXT

STOCKHOLM NOT WIRED. The node exists; the drain does not know about it, so this
is one copy of the history, not two. Do the steps printed above on the PARIS
node, watch for 'copies=2 degraded_targets=-' in

  journalctl -u tr-clickhouse-operational-ingest-postgres -f | grep outbox.metrics

and then record it by re-running:

  TR_STOCKHOLM_REPLICA_WIRED=1 bash scripts/deploy/aws_eu_north_clickhouse.sh

NEXT
  exit 3
fi

exit "$COMPLETE"
