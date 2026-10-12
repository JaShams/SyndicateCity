#!/usr/bin/env bash
# Start the dedicated D-Bus daemon and the coordinator, both as the service user.
# Extra arguments go to server.py, e.g. test-speed mode:
#   sudo scripts/run_server.sh --negotiation-seconds 2 --movement-seconds 2 --auction-seconds 2 --rounds 2
# Set KEEP_BUS=1 to leave the bus running after the coordinator exits.
set -euo pipefail
source "$(dirname "$0")/common.sh"
need_root "switching to $SC_SERVER_USER"
[ -f "$SC_BUS_CONF" ] && [ -f "$SC_ROSTER" ] || die "not set up; run sudo scripts/setup.sh"

sc_start_bus
trap '[ "${KEEP_BUS:-0}" = "1" ] || sc_stop_bus' EXIT

stamp="$(date +%Y%m%d-%H%M%S)"
cd "$SC_INSTALL_DIR"
runuser -u "$SC_SERVER_USER" -- "$SC_PYTHON" "$SC_INSTALL_DIR/server.py" \
    --roster "$SC_ROSTER" --bus-address-file "$SC_ADDRESS_FILE" \
    --audit-log "$SC_LOG_DIR/audit-$stamp.jsonl" "$@"
