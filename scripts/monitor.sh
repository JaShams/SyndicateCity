#!/usr/bin/env bash
# Watch the game bus with dbus-monitor (debugging only). Waits for the bus to
# come up, so start it before or after run_server.sh / demo.sh, and reattaches
# when the next game starts. Ctrl-C stops it.
#   sudo scripts/monitor.sh              # game traffic only (org.syndicate.City1)
#   sudo scripts/monitor.sh --all        # every message, bus housekeeping included
#   sudo scripts/monitor.sh --profile    # extra arguments go to dbus-monitor
# Requires `sudo scripts/setup.sh --debug-monitor`; the normal policy forbids it.
set -euo pipefail
source "$(dirname "$0")/common.sh"
need_root "monitoring the game bus"
command -v dbus-monitor >/dev/null || die "dbus-monitor missing: apt install dbus-bin (or dbus)"
grep -q '<policy user="root">' "$SC_BUS_CONF" 2>/dev/null \
    || die "monitoring is not enabled; run: sudo scripts/setup.sh --debug-monitor"

match=("interface='org.syndicate.City1'")
if [ "${1:-}" = "--all" ]; then
    match=(); shift
fi

while true; do
    if [ ! -S "$SC_SOCKET" ]; then
        echo "== waiting for the game bus ($SC_SOCKET)..." >&2
        until [ -S "$SC_SOCKET" ]; do sleep 0.1; done
    fi
    echo "== attached; Ctrl-C to stop" >&2
    dbus-monitor --address "unix:path=$SC_SOCKET" "$@" "${match[@]}" || true
    echo "== game bus closed" >&2
    sleep 1
done
