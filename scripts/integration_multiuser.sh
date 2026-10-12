#!/usr/bin/env bash
# Full multi-UID integration run (PRD 11.2). Requires `sudo scripts/setup.sh` first.
#
#   sudo scripts/integration_multiuser.sh
#
# 1. starts the dedicated bus and asserts server + 4 players have distinct UIDs;
# 2. runs every adversarial probe as player 4 in the lobby;
# 3. runs a fast 3-round match with 4 scripted players, each as its own UID;
# 4. kills player 2 during the first auction and restarts it under the same UID;
# 5. verifies routing privacy, consistent results, rejoin state and log permissions.
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd /
need_root "running processes as several accounts"
[ -f "$SC_ROSTER" ] || die "run sudo scripts/setup.sh first"

OUT="$(mktemp -d /tmp/syndicate-integration.XXXX)"
chmod 0755 "$OUT"
echo "logs: $OUT"
PIDS=()
cleanup() {
    for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
    pkill -u "$SC_SERVER_USER" -f "$SC_INSTALL_DIR/server.py" 2>/dev/null || true
    sc_stop_bus >/dev/null
    exec 7>&- 2>/dev/null || true
}
trap cleanup EXIT

P=(); for i in 1 2 3 4; do P+=("${SC_PLAYER_PREFIX}$i"); done
uids=$(for u in "$SC_SERVER_USER" "${P[@]}"; do id -u "$u"; done)
[ "$(echo "$uids" | sort -u | wc -l)" -eq 5 ] || die "accounts do not have distinct UIDs"
echo "== distinct UIDs:" $uids

sc_start_bus

echo "== coordinator (fast timers)"
mkfifo "$OUT/console"
chown "$SC_SERVER_USER" "$OUT/console"
runuser -u "$SC_SERVER_USER" -- "$SC_PYTHON" "$SC_INSTALL_DIR/server.py" \
    --roster "$SC_ROSTER" --bus-address-file "$SC_ADDRESS_FILE" \
    --audit-log "$SC_LOG_DIR/audit-integration.jsonl" \
    --negotiation-seconds 2 --movement-seconds 2 --auction-seconds 3 --rounds 3 --linger 8 \
    < "$OUT/console" > "$OUT/server.log" 2>&1 &
PIDS+=($!)
exec 7>"$OUT/console"          # keep the console pipe open
sleep 1

echo "== adversarial probes as ${P[3]} (lobby)"
if sudo -u "${P[3]}" -- "$SC_PYTHON" "$SC_INSTALL_DIR/tests/adversarial_client.py" \
        --bus-address-file "$SC_ADDRESS_FILE" --eavesdrop-seconds 2 all > "$OUT/adversarial.log" 2>&1; then
    echo "   all probes behaved securely"
else
    echo "   SOME PROBES FAILED (see $OUT/adversarial.log)"; ADV_FAIL=1
fi
sleep 2.5    # flood probe: let token bucket refill; p4 session already gone

echo "== players"
for i in 1 2 3 4; do
    sudo -u "${P[$((i-1))]}" -- "$SC_PYTHON" "$SC_INSTALL_DIR/tests/scripted_client.py" \
        --bus-address-file "$SC_ADDRESS_FILE" > "$OUT/p$i.log" 2>&1 &
    PIDS+=($!)
    disown $!     # player 2 is SIGKILLed on purpose; no job-control noise
done
for _ in $(seq 50); do
    [ "$(grep -c '^PlayerJoined\|^PlayerRejoined' "$OUT/server.log" || true)" -ge 5 ] && break
    sleep 0.2
done
echo start >&7

echo "== kill player 2 during the first auction, then restart it"
for _ in $(seq 150); do grep -q '"AuctionOpened"' "$OUT/p1.log" && break; sleep 0.1; done
grep -q '"AuctionOpened"' "$OUT/p1.log" || die "no auction happened"
sleep 0.5
# SIGKILL the python process itself (sudo cannot relay SIGKILL to its child).
pkill -9 -u "${P[1]}" -f scripted_client.py || die "player 2 process not found"
sleep 0.7
sudo -u "${P[1]}" -- "$SC_PYTHON" "$SC_INSTALL_DIR/tests/scripted_client.py" \
    --bus-address-file "$SC_ADDRESS_FILE" > "$OUT/p2-rejoin.log" 2>&1 &
PIDS+=($!)

echo "== bus-level probes as ${P[3]} during the match (other players connected)"
sleep 1
if sudo -u "${P[3]}" -- "$SC_PYTHON" "$SC_INSTALL_DIR/tests/adversarial_client.py" \
        --bus-address-file "$SC_ADDRESS_FILE" --eavesdrop-seconds 2 \
        coordinator phantom monitor direct eavesdrop > "$OUT/adversarial-live.log" 2>&1 \
        && grep -q "4/4 denied" "$OUT/adversarial-live.log"; then
    echo "   all live probes behaved securely (4/4 direct calls denied)"
else
    echo "   LIVE PROBES FAILED (see $OUT/adversarial-live.log)"; ADV_FAIL=1
fi

echo "== waiting for match end"
for _ in $(seq 300); do
    n=0; for f in p1 p3 p4 p2-rejoin; do grep -q '"MatchEnded"' "$OUT/$f.log" && n=$((n+1)); done
    [ "$n" -eq 4 ] && break
    sleep 0.2
done

echo "== audit log permissions"
ls -l "$SC_LOG_DIR/audit-integration.jsonl" | tee "$OUT/audit-perms.txt"
if sudo -u "${P[0]}" cat "$SC_LOG_DIR/audit-integration.jsonl" >/dev/null 2>&1; then
    echo "AUDIT_READABLE_BY_PLAYER" >> "$OUT/audit-perms.txt"
fi

echo "== verify"
if "$SC_PYTHON" "$SC_INSTALL_DIR/tests/verify_multiuser.py" "$OUT" $uids; then rc=0; else rc=1; fi
[ "${ADV_FAIL:-0}" -eq 0 ] || rc=1
echo "logs kept in $OUT"
exit $rc
