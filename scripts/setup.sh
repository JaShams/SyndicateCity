#!/usr/bin/env bash
# Provision Syndicate City on Ubuntu: group, service account, player accounts,
# install tree, dedicated-bus config and roster. Idempotent. Needs sudo.
#
#   sudo scripts/setup.sh [--players N] [--offline]     # N = 4..6, default 4
#   sudo scripts/setup.sh --debug-monitor                # also let root run dbus-monitor
#   sudo scripts/setup.sh --uninstall                    # remove everything we created
#
# Setup refuses to reuse an account or group that already exists unless it created
# it itself (see SC_ACCOUNTS_RECORD). --adopt-existing reuses them anyway; adopted
# accounts are never deleted by --uninstall.
set -euo pipefail
source "$(dirname "$0")/common.sh"

PLAYERS=4
OFFLINE=0
UNINSTALL=0
DEBUG_MONITOR=0
ADOPT=0
FORCE_LEGACY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --players) PLAYERS="$2"; shift 2 ;;
        --offline) OFFLINE=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --debug-monitor) DEBUG_MONITOR=1; shift ;;
        --adopt-existing) ADOPT=1; shift ;;
        --force-legacy) FORCE_LEGACY=1; shift ;;
        *) die "unknown option $1" ;;
    esac
done
need_root "creating accounts"

# ---- record of what we created: lines "<created|adopted> <user|group> <name> <id>"

# record <created|adopted> <user|group> <name> <id>   (replaces any older line for name)
record() {
    install -d -o root -g root -m 0755 "$SC_CONF_DIR"
    local tmp="$SC_ACCOUNTS_RECORD.tmp"
    if [ -f "$SC_ACCOUNTS_RECORD" ]; then
        awk -v k="$2" -v n="$3" '!($2 == k && $3 == n)' "$SC_ACCOUNTS_RECORD" > "$tmp"
    else
        : > "$tmp"
    fi
    printf '%s %s %s %s\n' "$1" "$2" "$3" "$4" >> "$tmp"
    chown root:root "$tmp"
    chmod 0600 "$tmp"
    mv "$tmp" "$SC_ACCOUNTS_RECORD"
}

# recorded <created|adopted> <user|group> <name>  -> prints the recorded id, fails if absent
recorded() {
    [ -f "$SC_ACCOUNTS_RECORD" ] || return 1
    awk -v h="$1" -v k="$2" -v n="$3" '$1 == h && $2 == k && $3 == n {id = $4; f = 1}
        END {if (f) print id; exit !f}' "$SC_ACCOUNTS_RECORD"
}

# claim_existing <user|group> <name> <current id>: an existing account may be reused
# only if we created it (same id) or the operator asked to adopt it.
claim_existing() {
    local kind="$1" name="$2" cur="$3" rec
    if rec="$(recorded created "$kind" "$name")"; then
        [ "$rec" = "$cur" ] && return 0
        die "$kind $name exists with id $cur, but setup created it with id $rec; it was" \
            "replaced by someone else. Remove it or rerun with --adopt-existing."
    fi
    recorded adopted "$kind" "$name" >/dev/null && return 0
    if [ "$ADOPT" -eq 1 ]; then
        echo "  adopting existing $kind $name (id $cur); --uninstall will leave it alone"
        record adopted "$kind" "$name" "$cur"
        return 0
    fi
    if [ ! -f "$SC_ACCOUNTS_RECORD" ] && [ -f "$SC_ROSTER" ]; then
        echo "note: this looks like an install from an older setup.sh, which kept no record" \
             "of its accounts. If $name is a Syndicate City account, clean up first with:" >&2
        echo "    sudo $0 --uninstall --force-legacy" >&2
    fi
    die "$kind $name already exists and was not created by this setup. Refusing to take" \
        "it over. Rename it, or rerun with --adopt-existing to use it anyway" \
        "(adopted accounts are never deleted by --uninstall)."
}

uninstall() {
    local kept_legacy=0
    "$SC_SRC_DIR/scripts/stop.sh" || true
    if [ -f "$SC_ACCOUNTS_RECORD" ]; then
        local how kind name id cur
        # Users first (groupdel refuses while the group is a user's primary group).
        while read -r how kind name id; do
            [ "$kind" = user ] || continue
            if [ "$how" = adopted ]; then
                gpasswd -d "$name" "$SC_GROUP" >/dev/null 2>&1 || true
                echo "  kept $name (existed before setup)"
                continue
            fi
            cur="$(id -u "$name" 2>/dev/null)" || { echo "  $name already gone"; continue; }
            if [ "$cur" != "$id" ]; then
                echo "  kept $name: its UID is $cur, but setup created it as $id"
                continue
            fi
            if [ "$name" = "$SC_SERVER_USER" ]; then
                userdel "$name"
            else
                userdel -r "$name" 2>/dev/null || userdel "$name"
            fi
            echo "  deleted $name"
        done < "$SC_ACCOUNTS_RECORD"
        while read -r how kind name id; do
            [ "$kind" = group ] || continue
            if [ "$how" = adopted ]; then
                echo "  kept group $name (existed before setup)"
                continue
            fi
            cur="$(getent group "$name" | cut -d: -f3)"
            if [ -z "$cur" ]; then
                echo "  group $name already gone"
            elif [ "$cur" != "$id" ]; then
                echo "  kept group $name: its GID is $cur, but setup created it as $id"
            else
                groupdel "$name"
                echo "  deleted group $name"
            fi
        done < "$SC_ACCOUNTS_RECORD"
    else
        # Installed before setup kept a record: we cannot tell our accounts from others.
        local found=()
        for u in "${SC_PLAYER_PREFIX}"{1..6} "$SC_SERVER_USER"; do
            id "$u" >/dev/null 2>&1 && found+=("$u")
        done
        if [ "${#found[@]}" -gt 0 ] && [ "$FORCE_LEGACY" -eq 0 ]; then
            echo "No record of which accounts setup created (installed by an older version)."
            echo "These accounts match the game's names and were NOT deleted:"
            printf '    %s\n' "${found[@]}"
            getent group "$SC_GROUP" >/dev/null && echo "    group $SC_GROUP"
            echo "If they are all Syndicate City accounts, delete them with:"
            echo "    sudo $0 --uninstall --force-legacy"
            kept_legacy=1
        elif [ "$FORCE_LEGACY" -eq 1 ]; then
            for u in "${found[@]}"; do
                if [ "$u" = "$SC_SERVER_USER" ]; then userdel "$u"; else userdel -r "$u" 2>/dev/null || userdel "$u"; fi
                echo "  deleted $u"
            done
            getent group "$SC_GROUP" >/dev/null && groupdel "$SC_GROUP" && echo "  deleted group $SC_GROUP"
        fi
    fi
    rm -rf "$SC_INSTALL_DIR" "$SC_CONF_DIR" "$SC_RUN_DIR" "$SC_LOG_DIR"
    if [ "$kept_legacy" -eq 1 ]; then
        echo "Syndicate City files removed; the accounts listed above were kept."
    else
        echo "Syndicate City removed."
    fi
}

if [ "$UNINSTALL" -eq 1 ]; then
    uninstall
    exit 0
fi

[[ "$PLAYERS" =~ ^[4-6]$ ]] || die "--players must be 4, 5 or 6"
command -v dbus-daemon >/dev/null || die "dbus-daemon missing: apt install dbus-daemon (or dbus)"
command -v python3 >/dev/null || die "python3 missing"

echo "== accounts"
# Check every name before creating anything, so a refusal leaves the system untouched.
if gid="$(getent group "$SC_GROUP" | cut -d: -f3)" && [ -n "$gid" ]; then
    claim_existing group "$SC_GROUP" "$gid"
    NEW_GROUP=0
else
    NEW_GROUP=1
fi
if uid="$(id -u "$SC_SERVER_USER" 2>/dev/null)"; then
    claim_existing user "$SC_SERVER_USER" "$uid"
fi
for i in $(seq 1 "$PLAYERS"); do
    u="${SC_PLAYER_PREFIX}$i"
    if uid="$(id -u "$u" 2>/dev/null)"; then
        claim_existing user "$u" "$uid"
    fi
done

if [ "$NEW_GROUP" -eq 1 ]; then
    groupadd --system "$SC_GROUP"
    record created group "$SC_GROUP" "$(getent group "$SC_GROUP" | cut -d: -f3)"
fi
if ! id "$SC_SERVER_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
        --gid "$SC_GROUP" "$SC_SERVER_USER"
    record created user "$SC_SERVER_USER" "$(id -u "$SC_SERVER_USER")"
fi
PLAYER_USERS=()
for i in $(seq 1 "$PLAYERS"); do
    u="${SC_PLAYER_PREFIX}$i"
    if ! id "$u" >/dev/null 2>&1; then
        # Dedicated test accounts with locked passwords; reach them via sudo -u.
        useradd --create-home --shell /bin/bash --user-group "$u"
        passwd -l "$u" >/dev/null
        record created user "$u" "$(id -u "$u")"
    fi
    usermod -a -G "$SC_GROUP" "$u"
    PLAYER_USERS+=("$u")
done

echo "== install tree $SC_INSTALL_DIR"
install -d -o root -g root -m 0755 "$SC_INSTALL_DIR" "$SC_INSTALL_DIR/tests" "$SC_INSTALL_DIR/scripts" "$SC_INSTALL_DIR/config" "$SC_INSTALL_DIR/demo"
install -o root -g root -m 0644 "$SC_SRC_DIR"/{ipc,cmdlog,server,client,tui,game,board}.py "$SC_SRC_DIR/requirements.txt" "$SC_INSTALL_DIR/"
install -o root -g root -m 0644 "$SC_SRC_DIR"/tests/*.py "$SC_INSTALL_DIR/tests/"
install -o root -g root -m 0755 "$SC_SRC_DIR"/scripts/*.py "$SC_INSTALL_DIR/scripts/"
install -o root -g root -m 0644 "$SC_SRC_DIR/config/bus.conf.in" "$SC_INSTALL_DIR/config/"
install -o root -g root -m 0644 "$SC_SRC_DIR"/demo/*.txt "$SC_INSTALL_DIR/demo/"
if [ ! -x "$SC_PYTHON" ]; then
    python3 -m venv "$SC_INSTALL_DIR/venv" || die "python3 -m venv failed: apt install python3-venv"
fi
if ! "$SC_PYTHON" -c "import dbus_next, websockets, pysyncobj, cryptography" 2>/dev/null; then
    if [ "$OFFLINE" -eq 1 ]; then
        die "dependencies (requirements.txt) not installed and --offline given"
    fi
    "$SC_INSTALL_DIR/venv/bin/pip" install --quiet -r "$SC_SRC_DIR/requirements.txt"
fi
chmod -R a+rX "$SC_INSTALL_DIR"

echo "== config $SC_CONF_DIR"
install -d -o root -g root -m 0755 "$SC_CONF_DIR"
args=(--socket "$SC_SOCKET" --server-user "$SC_SERVER_USER" --out "$SC_BUS_CONF")
for u in "${PLAYER_USERS[@]}"; do args+=(--player "$u"); done
# Debug only: root may watch every message (re-run setup without it to remove).
[ "$DEBUG_MONITOR" -eq 1 ] && args+=(--debug-monitor)
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
if [ "$DEBUG_MONITOR" -eq 1 ]; then
    echo
    echo "WARNING: --debug-monitor is on. root can read every message on the game bus,"
    echo "private ones included. Watch with:"
    echo "  sudo dbus-monitor --address \"\$(sudo cat $SC_ADDRESS_FILE)\""
    echo "Run setup again without --debug-monitor to remove it; restart the bus to apply."
fi
