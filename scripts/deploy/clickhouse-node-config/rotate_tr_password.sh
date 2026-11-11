#!/usr/bin/env bash
# Change the password of the `tr` ClickHouse user on THIS node to the one on
# standard input.
#
# Runs on a cluster node as root; scripts/deploy/clickhouse_rotate_password.sh
# ships it and pipes the new value in, so the value is never an argument, never
# instance metadata and never in a staging file. Without --apply it only
# reports whether the node already uses that password.
#
# Nothing else on a provisioned node can do this. clickhouse_startup.sh writes
# users.d/tr-user.xml once and never runs again, and the scripts that write
# /etc/tr-clickhouse-ingest.env do not touch the server's own copy.
#
#   --apply   install users.d/tr-user.xml holding only the SHA-256 of the new
#             password, wait until the server accepts it (users.d is reread
#             without a restart), require that the previous password is now
#             refused, write the new value to /etc/tr-clickhouse-ingest.env,
#             and restart the running services that read that file. If the
#             server does not accept the new password, the previous user file
#             goes back and nothing else changes.
#
# It can be run again after a failure: whatever is already in place is checked
# and left alone, and the rest is brought in line. Run with the password the
# node already has, it replaces a plain-text user file with the hashed one and
# changes nothing else.
set -euo pipefail

ROOT="${TR_NODE_ROOT:-}"
USERS_D="${ROOT}/etc/clickhouse-server/users.d"
USER_FILE="${USERS_D}/tr-user.xml"
ENV_FILE="${ROOT}/etc/tr-clickhouse-ingest.env"
UNIT_D="${ROOT}/etc/systemd/system"
WAIT_TRIES="${TR_WAIT_TRIES:-30}"
WAIT_SECONDS="${TR_WAIT_SECONDS:-2}"

APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    *) echo "usage: $0 [--apply] < new-password" >&2; exit 2 ;;
  esac
done

node="$(hostname)"

IFS= read -r NEW_PASSWORD || true
# The env file is read both by systemd and by `.` in a shell, so the value must
# mean the same thing to both. That and a length floor are the whole rule.
if ! [[ "$NEW_PASSWORD" =~ ^[A-Za-z0-9_-]{24,}$ ]]; then
  echo "refusing: the new password must be 24 or more characters from A-Z a-z 0-9 _ -" >&2
  exit 1
fi

if [ ! -r "$ENV_FILE" ]; then
  echo "refusing: ${ENV_FILE} is missing on ${node}" >&2
  exit 1
fi
OLD_PASSWORD="$(
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  printf '%s' "${CH_PASSWORD:-}"
)"

# The password travels in the environment: an argument is readable by every
# user on the node for as long as the client runs.
signs_in() {
  CLICKHOUSE_PASSWORD="$1" clickhouse-client --user tr --query 'SELECT 1' >/dev/null 2>&1
}

# Refused for being the wrong password, and for no other reason: a server that
# is down also fails a sign-in, and that must not read as "the old one is dead".
refused() {
  local output
  if output="$(CLICKHOUSE_PASSWORD="$1" clickhouse-client --user tr --query 'SELECT 1' 2>&1)"; then
    return 1
  fi
  grep -q 'AUTHENTICATION_FAILED' <<<"$output"
}

wait_for() {
  local tries=0
  until "$@"; do
    tries=$((tries + 1))
    if [ "$tries" -ge "$WAIT_TRIES" ]; then
      return 1
    fi
    sleep "$WAIT_SECONDS"
  done
}

render_user_file() {
  local hash
  hash="$(printf '%s' "$NEW_PASSWORD" | sha256sum | cut -d' ' -f1)"
  # The same user clickhouse_startup.sh creates on a new node.
  cat <<XML
<clickhouse>
    <users>
        <tr>
            <password_sha256_hex>${hash}</password_sha256_hex>
            <networks>
                <ip>10.0.0.0/8</ip>
                <!-- Loopback is required, not optional: the ingester runs on
                     this host and the readiness check below connects over
                     127.0.0.1, which is NOT inside the VPC range. Omitting it
                     leaves the server healthy but the user unable to log in. -->
                <ip>127.0.0.1</ip>
                <ip>::1</ip>
            </networks>
            <profile>default</profile>
            <quota>default</quota>
            <access_management>1</access_management>
        </tr>
    </users>
</clickhouse>
XML
}

# Written beside its destination and renamed into place, so the server never
# reads half a file. The name does not end in .xml, so it is not read at all.
replace_file() {
  local destination="$1" owner="$2" group="$3" mode="$4" staged
  staged="$(mktemp "${destination}.XXXXXX")"
  cat >"$staged"
  chown "${owner}:${group}" "$staged"
  chmod "$mode" "$staged"
  mv -f "$staged" "$destination"
}

if signs_in "$NEW_PASSWORD"; then
  accepted=1
elif [ -n "$OLD_PASSWORD" ] && signs_in "$OLD_PASSWORD"; then
  accepted=0
else
  echo "refusing: ClickHouse on ${node} accepts neither the password in ${ENV_FILE} nor the new one" >&2
  exit 1
fi

# The file is compared as well as the sign-in: a node provisioned before the
# hash was introduced accepts its password from a plain-text copy, and that
# copy goes even when the value stays.
current=1
render_user_file | cmp -s - "$USER_FILE" || current=0

if [ "$APPLY" -eq 0 ]; then
  if [ "$accepted" -eq 0 ]; then
    echo "${node}: --apply would change the tr password"
  elif [ "$current" -eq 0 ]; then
    echo "${node}: the server accepts this password; --apply would rewrite ${USER_FILE} to hold only its hash"
  elif [ "$OLD_PASSWORD" != "$NEW_PASSWORD" ]; then
    echo "${node}: the server accepts this password but ${ENV_FILE} holds another; --apply would rewrite it"
  else
    echo "${node}: already uses this password"
  fi
  exit 0
fi

installed=0
if [ "$current" -eq 0 ]; then
  previous="$(mktemp "${USERS_D}/tr-user.previous.XXXXXX")"
  trap 'rm -f "$previous"' EXIT
  cat "$USER_FILE" >"$previous"
  render_user_file | replace_file "$USER_FILE" \
    "${TR_FILE_OWNER:-clickhouse}" "${TR_FILE_GROUP:-clickhouse}" 0640
  installed=1
  echo "${node}: installed ${USER_FILE}"
fi
if [ "$installed" -eq 1 ] || [ "$accepted" -eq 0 ]; then
  # The server rereads users.d on its own every few seconds; asking saves the
  # wait. Whichever password it holds at this instant is the one that asks.
  for password in "$OLD_PASSWORD" "$NEW_PASSWORD"; do
    CLICKHOUSE_PASSWORD="$password" clickhouse-client --user tr \
      --query 'SYSTEM RELOAD CONFIG' >/dev/null 2>&1 || true
  done
  if ! wait_for signs_in "$NEW_PASSWORD"; then
    if [ "$installed" -eq 1 ]; then
      replace_file "$USER_FILE" \
        "${TR_FILE_OWNER:-clickhouse}" "${TR_FILE_GROUP:-clickhouse}" 0640 <"$previous"
      echo "refusing to continue: ClickHouse on ${node} did not accept the new password after ${WAIT_TRIES} tries; the previous user file is back" >&2
    else
      echo "refusing to continue: ${USER_FILE} on ${node} holds the new hash but ClickHouse does not accept the new password" >&2
    fi
    exit 1
  fi
fi

if [ "$OLD_PASSWORD" != "$NEW_PASSWORD" ]; then
  # The point of a rotation: the old value no longer opens anything.
  if [ -n "$OLD_PASSWORD" ] && ! wait_for refused "$OLD_PASSWORD"; then
    echo "refusing to report success: ClickHouse on ${node} was not seen to refuse the previous password" >&2
    exit 1
  fi
  {
    grep -v '^CH_PASSWORD=' "$ENV_FILE" || true
    printf 'CH_PASSWORD=%s\n' "$NEW_PASSWORD"
  } | replace_file "$ENV_FILE" "$(id -un)" "$(id -gn)" 0600
  echo "${node}: wrote ${ENV_FILE}"
  # Timer-driven units read the file at their next start. Only a unit that is
  # running now still holds the old value.
  for unit_file in "${UNIT_D}"/*.service; do
    [ -e "$unit_file" ] || continue
    grep -q '^EnvironmentFile=-\{0,1\}/etc/tr-clickhouse-ingest.env' "$unit_file" || continue
    unit="$(basename "$unit_file")"
    systemctl is-active --quiet "$unit" || continue
    # A service that cannot sign in exits; give it the chance to. One that
    # reports its own readiness fails the restart itself.
    if systemctl restart "$unit"; then
      sleep "$WAIT_SECONDS"
    fi
    if ! systemctl is-active --quiet "$unit"; then
      echo "refusing to report success: ${unit} on ${node} is not running after its restart" >&2
      exit 1
    fi
    echo "${node}: restarted ${unit}"
  done
fi

echo "${node}: tr signs in with this password; ${USER_FILE} holds only its hash"
