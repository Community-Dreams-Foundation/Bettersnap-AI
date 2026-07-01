#!/usr/bin/env bash
# Emergency GPU spend brake: flip GPU_DISPATCH_ENABLED on the Function app.
# This is the action a budget Action Group / runbook should run at the 100%
# (or daily) threshold — budget alerts only NOTIFY; this actually stops dispatch.
#
# Usage:
#   ./disable_dispatch.sh off        # stop all A100 dispatch
#   ./disable_dispatch.sh on         # resume
#   DRY_RUN=1 ./disable_dispatch.sh off   # print the command only
set -euo pipefail

RG="${RESOURCE_GROUP:-bettersnap-ai-rg}"
APP="${FUNCTION_APP_NAME:?set FUNCTION_APP_NAME to the Function app name}"

case "${1:-}" in
  off) VALUE=false ;;
  on)  VALUE=true ;;
  *)   echo "usage: $0 {on|off}" >&2; exit 2 ;;
esac

CMD=(az functionapp config appsettings set
     --resource-group "$RG" --name "$APP"
     --settings "GPU_DISPATCH_ENABLED=$VALUE")

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "[dry-run] ${CMD[*]}"
else
  "${CMD[@]}"
  echo "GPU_DISPATCH_ENABLED=$VALUE set on $APP"
fi
