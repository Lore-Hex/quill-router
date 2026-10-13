# Sourced by both command doubles; each line is one JSON array, in execution order.
record_event() {
  {
    printf '['
    separator=''
    for arg in "$@"; do
      arg=${arg//\\/\\\\}
      arg=${arg//\"/\\\"}
      arg=${arg//$'\n'/\\n}
      arg=${arg//$'\r'/\\r}
      arg=${arg//$'\t'/\\t}
      printf '%s"%s"' "${separator}" "${arg}"
      separator=,
    done
    printf ']\n'
  } >> "${TMPDIR}/events.jsonl"
}
