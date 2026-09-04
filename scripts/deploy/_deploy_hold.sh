#!/usr/bin/env bash
# Shared parser for the deploy-time regional traffic hold control.

deploy_region_is_held() {
  local target="$1"
  local raw="${TR_DEPLOY_HOLD_REGIONS:-}"
  local entry

  while true; do
    entry="${raw%%,*}"
    entry="${entry#"${entry%%[![:space:]]*}"}"
    entry="${entry%"${entry##*[![:space:]]}"}"
    if [ "$entry" = "all" ] || [ "$entry" = "$target" ]; then
      return 0
    fi
    if [[ "$raw" != *,* ]]; then
      break
    fi
    raw="${raw#*,}"
  done
  return 1
}

deploy_warn_region_held() {
  echo "::warning::$1 held by TR_DEPLOY_HOLD_REGIONS; traffic untouched"
}
