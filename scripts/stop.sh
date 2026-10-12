#!/usr/bin/env bash
# Stop the coordinator and the dedicated bus, removing only our runtime files.
# Never touches the system bus or anyone's session bus.
set -euo pipefail
source "$(dirname "$0")/common.sh"
need_root "stopping service-user processes"
pkill -u "$SC_SERVER_USER" -f "$SC_INSTALL_DIR/server.py" 2>/dev/null && echo "coordinator stopped" || true
sc_stop_bus
