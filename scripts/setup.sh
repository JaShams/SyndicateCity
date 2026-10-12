#!/usr/bin/env bash
# Provision Syndicate City on Ubuntu: group, service account, player accounts,
# install tree, dedicated-bus config and roster. Idempotent. Needs sudo.
#
#   sudo scripts/setup.sh [--players N] [--offline]     # N = 4..6, default 4
#   sudo scripts/setup.sh --uninstall                    # remove everything we created
set -euo pipefail
source "$(dirname "$0")/common.sh"

PLAYERS=4
OFFLINE=0
UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --players) PLAYERS="$2"; shift 2 ;;
        --offline) OFFLINE=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        *) die "unknown option $1" ;;
    esac
done
need_root "creating accounts"

if [ "$UNINSTALL" -eq 1 ]; then
    "$SC_SRC_DIR/scripts/stop.sh" || true
    for i in 1 2 3 4 5 6; do
        id "${SC_PLAYER_PREFIX}$i" >/dev/null 2>&1 && userdel -r "${SC_PLAYER_PREFIX}$i" 2>/dev/null || true
    done
    id "$SC_SERVER_USER" >/dev/null 2>&1 && userdel "$SC_SERVER_USER" || true
    getent group "$SC_GROUP" >/dev/null && groupdel "$SC_GROUP" || true
    rm -rf "$SC_INSTALL_DIR" "$SC_CONF_DIR" "$SC_RUN_DIR" "$SC_LOG_DIR"
    echo "Syndicate City removed."
    exit 0
fi

[[ "$PLAYERS" =~ ^[4-6]$ ]] || die "--players must be 4, 5 or 6"
command -v dbus-daemon >/dev/null || die "dbus-daemon missing: apt install dbus-daemon (or dbus)"
command -v python3 >/dev/null || die "python3 missing"

echo "== accounts"
getent group "$SC_GROUP" >/dev/null || groupadd --system "$SC_GROUP"
if ! id "$SC_SERVER_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
        --gid "$SC_GROUP" "$SC_SERVER_USER"
fi
PLAYER_USERS=()
for i in $(seq 1 "$PLAYERS"); do
    u="${SC_PLAYER_PREFIX}$i"
    if ! id "$u" >/dev/null 2>&1; then
        # Dedicated test accounts with locked passwords; reach them via sudo -u.
        useradd --create-home --shell /bin/bash --user-group "$u"
        passwd -l "$u" >/dev/null
    fi
    usermod -a -G "$SC_GROUP" "$u"
    PLAYER_USERS+=("$u")
done

echo "== install tree $SC_INSTALL_DIR"
install -d -o root -g root -m 0755 "$SC_INSTALL_DIR" "$SC_INSTALL_DIR/tests" "$SC_INSTALL_DIR/scripts" "$SC_INSTALL_DIR/config" "$SC_INSTALL_DIR/demo"
install -o root -g root -m 0644 "$SC_SRC_DIR"/{ipc,server,client,game,board}.py "$SC_SRC_DIR/requirements.txt" "$SC_INSTALL_DIR/"
install -o root -g root -m 0644 "$SC_SRC_DIR"/tests/*.py "$SC_INSTALL_DIR/tests/"
install -o root -g root -m 0755 "$SC_SRC_DIR"/scripts/*.py "$SC_INSTALL_DIR/scripts/"
install -o root -g root -m 0644 "$SC_SRC_DIR/config/bus.conf.in" "$SC_INSTALL_DIR/config/"
install -o root -g root -m 0644 "$SC_SRC_DIR"/demo/*.txt "$SC_INSTALL_DIR/demo/"
if [ ! -x "$SC_PYTHON" ]; then
    python3 -m venv "$SC_INSTALL_DIR/venv" || die "python3 -m venv failed: apt install python3-venv"
fi
if ! "$SC_PYTHON" -c "import dbus_next" 2>/dev/null; then
    if [ "$OFFLINE" -eq 1 ]; then
        die "dbus-next not installed and --offline given"
    fi
    "$SC_INSTALL_DIR/venv/bin/pip" install --quiet -r "$SC_SRC_DIR/requirements.txt"
fi
chmod -R a+rX "$SC_INSTALL_DIR"

echo "== config $SC_CONF_DIR"
install -d -o root -g root -m 0755 "$SC_CONF_DIR"
args=(--socket "$SC_SOCKET" --server-user "$SC_SERVER_USER" --out "$SC_BUS_CONF")
for u in "${PLAYER_USERS[@]}"; do args+=(--player "$u"); done
"$SC_PYTHON" "$SC_INSTALL_DIR/scripts/render_bus_conf.py" "${args[@]}"
chmod 0644 "$SC_BUS_CONF"
{
    echo '{"players": ['
    for i in $(seq 1 "$PLAYERS"); do
        sep=","; [ "$i" -eq "$PLAYERS" ] && sep=""
        echo "  {\"id\": \"p$i\", \"user\": \"${SC_PLAYER_PREFIX}$i\", \"name\": \"${SC_NAMES[$((i-1))]}\"}$sep"
    done
    echo ']}'
} > "$SC_ROSTER"
chmod 0644 "$SC_ROSTER"

echo "== log dir $SC_LOG_DIR"
install -d -o "$SC_SERVER_USER" -g "$SC_GROUP" -m 0700 "$SC_LOG_DIR"

echo "== verifying distinct UIDs"
uids=$(for u in "$SC_SERVER_USER" "${PLAYER_USERS[@]}"; do id -u "$u"; done)
[ "$(echo "$uids" | sort -u | wc -l)" -eq $((PLAYERS + 1)) ] || die "UIDs are not distinct"
for u in "$SC_SERVER_USER" "${PLAYER_USERS[@]}"; do printf '  %-20s uid=%s\n' "$u" "$(id -u "$u")"; done

echo
echo "Setup complete. Next: sudo scripts/run_server.sh   then, in 4 terminals: scripts/run_client.sh 1..$PLAYERS"
echo "Or watch a self-playing match: sudo scripts/demo.sh"
