#!/usr/bin/env bash
set -euo pipefail

# Uso: sudo ./iniciar_ryu.sh [ospf|ecmp]
ROUTING="${1:-ospf}"
if [[ "$ROUTING" != "ospf" && "$ROUTING" != "ecmp" ]]; then
  echo "Uso: $0 [ospf|ecmp]"
  exit 2
fi

cd "$(dirname "$0")"
export SDN_ROUTING="$ROUTING"
exec ryu-manager --observe-links --ofp-tcp-listen-port 6633 ryu_controller.py
