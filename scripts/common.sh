# Shared settings for Syndicate City scripts. Override via environment.
# shellcheck shell=bash
SC_GROUP="${SC_GROUP:-syndicate}"
SC_SERVER_USER="${SC_SERVER_USER:-syndicate-server}"
SC_PLAYER_PREFIX="${SC_PLAYER_PREFIX:-syndicate-p}"
SC_INSTALL_DIR="${SC_INSTALL_DIR:-/opt/syndicate-city}"
SC_CONF_DIR="${SC_CONF_DIR:-/etc/syndicate-city}"
SC_RUN_DIR="${SC_RUN_DIR:-/run/syndicate-city}"
SC_LOG_DIR="${SC_LOG_DIR:-/var/log/syndicate-city}"
SC_PYTHON="${SC_INSTALL_DIR}/venv/bin/python"
SC_BUS_CONF="${SC_CONF_DIR}/bus.conf"
SC_ROSTER="${SC_CONF_DIR}/roster.json"
SC_SOCKET="${SC_RUN_DIR}/bus.sock"
SC_ADDRESS_FILE="${SC_RUN_DIR}/bus.address"
SC_PID_FILE="${SC_RUN_DIR}/bus.pid"
SC_SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SC_NAMES=(alice bob carol dave erin frank)

die() { echo "error: $*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "run with sudo (needs root for $1)"; }

# Echo the PID in the pid file only if it is *our* dbus-daemon (owned by the
# service user and started with our config file). Never touch anything else.
our_bus_pid() {
    [ -f "$SC_PID_FILE" ] || return 1
    local pid
    pid="$(tr -dc '0-9' < "$SC_PID_FILE")"
    [ -n "$pid" ] && [ -d "/proc/$pid" ] || return 1
    local owner
    owner="$(stat -c %U "/proc/$pid")"
    [ "$owner" = "$SC_SERVER_USER" ] || return 1
    tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q -- "dbus-daemon.*--config-file=$SC_BUS_CONF" || return 1
    echo "$pid"
}

# Start the dedicated bus as the service user (no-op if ours is running).
sc_start_bus() {
    install -d -o "$SC_SERVER_USER" -g "$SC_GROUP" -m 0750 "$SC_RUN_DIR"
    [ "$(stat -c '%U:%G %a' "$SC_RUN_DIR")" = "$SC_SERVER_USER:$SC_GROUP 750" ] \
        || die "$SC_RUN_DIR has wrong ownership/mode"
    local pid out addr
    if pid="$(our_bus_pid)"; then
        echo "game bus already running (pid $pid)"
        return 0
    fi
    # Stale files from a previous run: remove only our own files.
    rm -f "$SC_SOCKET" "$SC_ADDRESS_FILE" "$SC_PID_FILE"
    out="$(runuser -u "$SC_SERVER_USER" -- dbus-daemon --config-file="$SC_BUS_CONF" \
            --fork --print-address=1 --print-pid=1)"
    addr="$(echo "$out" | sed -n 1p)"
    pid="$(echo "$out" | sed -n 2p)"
    [[ "$addr" == unix:path=* ]] || die "dbus-daemon did not report an address: $out"
    printf '%s\n' "$addr" > "$SC_ADDRESS_FILE"
    printf '%s\n' "$pid" > "$SC_PID_FILE"
    chown "$SC_SERVER_USER:$SC_GROUP" "$SC_ADDRESS_FILE" "$SC_PID_FILE"
    chmod 0640 "$SC_ADDRESS_FILE" "$SC_PID_FILE"
    echo "game bus started (pid $pid) at $addr"
}

sc_stop_bus() {
    local pid
    if pid="$(our_bus_pid)"; then
        kill "$pid" 2>/dev/null || true
        echo "game bus (pid $pid) stopped"
    fi
    rm -f "$SC_SOCKET" "$SC_ADDRESS_FILE" "$SC_PID_FILE"
}
