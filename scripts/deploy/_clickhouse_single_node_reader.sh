#!/usr/bin/env bash
# Emit the node-side install shared by standalone bootstrap and reruns.
# The caller supplies reader_hash on the node; no password enters the script.
single_node_reader_setup() {
  cat <<READER
umask 077
reader_config=\$(mktemp)
cat > "\$reader_config" <<XML
<clickhouse>
  <profiles>
    <tr_control_readonly>
      <readonly>1</readonly>
      <max_execution_time>30</max_execution_time>
      <max_memory_usage>1073741824</max_memory_usage>
      <max_result_bytes>1073741824</max_result_bytes>
    </tr_control_readonly>
  </profiles>
  <users>
    <tr_control_read>
      <password_sha256_hex>\${reader_hash}</password_sha256_hex>
      <networks><ip>$1</ip><ip>127.0.0.1</ip></networks>
      <profile>tr_control_readonly</profile>
      <quota>default</quota>
      <grants><query>GRANT SELECT ON default.*</query></grants>
    </tr_control_read>
  </users>
</clickhouse>
XML
if ! cmp -s "\$reader_config" /etc/clickhouse-server/users.d/tr-control-reader.xml; then
  install -o clickhouse -g clickhouse -m 0640 "\$reader_config" /etc/clickhouse-server/users.d/tr-control-reader.xml
  systemctl restart clickhouse-server
fi
rm -f "\$reader_config"
unset reader_hash
READER
}
