#!/usr/bin/env bash
# Distributed-mode demo on one machine: three coordinator replicas (Raft) and four
# players over wss://, each player as its own Linux account, in one tmux window.
#
#   sudo scripts/cluster_demo.sh              # 3 replicas + 4 players; you type the moves
#   sudo scripts/cluster_demo.sh --auto       # players play demo/p1..p4.txt by themselves
#   sudo scripts/cluster_demo.sh --tui        # players use the full-screen client
#   sudo scripts/cluster_demo.sh --single     # one coordinator with a crash-proof log, no Raft
#   sudo scripts/cluster_demo.sh kill 2       # SIGKILL replica 2 (kill the LEADER for fail-over)
#   sudo scripts/cluster_demo.sh restart 2    # start replica 2 again from its saved state
#   sudo scripts/cluster_demo.sh stop         # end the demo
#
# Other arguments after the options go to server.py (e.g. --rounds 3). Needs
# `sudo scripts/setup.sh` (4 players) and tmux. Ctrl-b d detaches; reattach with
# `sudo tmux attach -t syndicate-cluster`. Everything listens on 127.0.0.1 only.
set -euo pipefail
source "$(dirname "$0")/common.sh"

SESSION=syndicate-cluster
DEMO_DIR="${SC_DEMO_DIR:-/var/lib/syndicate-city-demo}"
KEYS="$DEMO_DIR/keys"
PLAYERS_DIR="$DEMO_DIR/players"

node_pane() { echo "$SESSION:0.$(($1 - 1))"; }

kill_node() {
    local n="$1"
    [[ "$n" =~ ^[1-3]$ ]] || die "replica number must be 1, 2 or 3"
    if pkill -KILL -u "$SC_SERVER_USER" -f -- "--node-id node$n( |$)"; then
        echo "replica $n killed with SIGKILL"
    else
        echo "replica $n is not running"
    fi
}

case "${1:-}" in
    kill)
        need_root "signalling the service account"
        kill_node "${2:-}"
        exit 0 ;;
    restart)
        need_root "running the coordinator as its service account"
        n="${2:-}"
        [[ "$n" =~ ^[1-3]$ ]] || die "replica number must be 1, 2 or 3"
        [ -f "$DEMO_DIR/node$n.cmd" ] || die "no demo running (start one first)"
        pgrep -u "$SC_SERVER_USER" -f -- "--node-id node$n( |$)" >/dev/null && die "replica $n is still running"
        tmux respawn-pane -t "$(node_pane "$n")" "$(cat "$DEMO_DIR/node$n.cmd")"
        echo "replica $n restarted from $DEMO_DIR/state$n"
        exit 0 ;;
    stop)
        need_root "stopping the demo"
        tmux kill-session -t "$SESSION" 2>/dev/null || true
        for n in 1 2 3; do pkill -TERM -u "$SC_SERVER_USER" -f -- "--node-id node$n( |$)" || true; done
        echo "demo stopped (state kept in $DEMO_DIR until the next start)"
        exit 0 ;;
esac

need_root "running each player and the coordinators as their own accounts"
command -v tmux >/dev/null || die "tmux missing: apt install tmux"
[ -f "$SC_ROSTER" ] || die "not set up; run sudo scripts/setup.sh"
"$SC_PYTHON" -c "import websockets, pysyncobj, cryptography" 2>/dev/null \
    || die "dependencies missing; re-run sudo scripts/setup.sh"

AUTO=0 TUI=0 NODES=3
while [ $# -gt 0 ]; do
    case "$1" in
        --auto) AUTO=1; shift ;;
        --tui) TUI=1; shift ;;
        --single) NODES=1; shift ;;
        *) break ;;
    esac
done

tmux kill-session -t "$SESSION" 2>/dev/null || true
for n in 1 2 3; do pkill -TERM -u "$SC_SERVER_USER" -f -- "--node-id node$n( |$)" || true; done
sleep 0.5

# -- fresh key material and state (owned by the service account) --------------------
rm -rf "$DEMO_DIR"
install -d -o root -g root -m 0755 "$DEMO_DIR" "$PLAYERS_DIR"
install -d -o "$SC_SERVER_USER" -g "$SC_GROUP" -m 0700 "$KEYS"
as_server() { runuser -u "$SC_SERVER_USER" -- "$@"; }
as_server "$SC_PYTHON" "$SC_INSTALL_DIR/scripts/net_keys.py" init --dir "$KEYS" \
    --host 127.0.0.1 --host localhost >/dev/null
install -o root -g root -m 0644 "$KEYS/server.crt" "$PLAYERS_DIR/server.crt"
for i in 1 2 3 4; do
    user="${SC_PLAYER_PREFIX}$i"
    as_server "$SC_PYTHON" "$SC_INSTALL_DIR/scripts/net_keys.py" token --dir "$KEYS" \
        --player "p$i" --hours 12 --out "$KEYS/p$i.token" >/dev/null
    # Each player's token is readable by that player only.
    install -o "$user" -g "$user" -m 0600 "$KEYS/p$i.token" "$PLAYERS_DIR/p$i.token"
done

ADDRESSES=""
for n in $(seq "$NODES"); do
    ADDRESSES+="${ADDRESSES:+,}wss://127.0.0.1:744$n"
done

# -- one command per replica (kept so `restart N` can rerun it) ---------------------
# Same pacing as demo.sh so the demo/ scripts fit; later arguments override it.
timing="--rounds 4 --negotiation-seconds 25 --movement-seconds 6 --auction-seconds 12 --linger 20"
for n in $(seq "$NODES"); do
    install -d -o "$SC_SERVER_USER" -g "$SC_GROUP" -m 0700 "$DEMO_DIR/state$n"
    cmd="runuser -u $SC_SERVER_USER -- $SC_PYTHON $SC_INSTALL_DIR/server.py --roster $SC_ROSTER"
    cmd+=" $timing --listen wss://127.0.0.1:744$n --tls-cert $KEYS/server.crt"
    cmd+=" --tls-key $KEYS/server.key --token-secret-file $KEYS/token.secret"
    cmd+=" --state-dir $DEMO_DIR/state$n --node-id node$n"
    if [ "$NODES" -gt 1 ]; then
        peers=""
        for m in 1 2 3; do [ "$m" -ne "$n" ] && peers+="${peers:+,}127.0.0.1:760$m"; done
        cmd+=" --raft-self 127.0.0.1:760$n --raft-peers $peers"
        cmd+=" --cluster-secret-file $KEYS/cluster.secret"
    fi
    cmd+=" $*"
    echo "$cmd" > "$DEMO_DIR/node$n.cmd"
done

tmux new-session -d -s "$SESSION" -x 240 -y 64 -n cluster "$(cat "$DEMO_DIR/node1.cmd")"
tmux set-option -t "$SESSION" remain-on-exit on >/dev/null
tmux set-option -t "$SESSION" pane-border-status top >/dev/null
tmux select-pane -t "$SESSION:0.0" -T "node1 (wss :7441)"
for n in $(seq 2 "$NODES"); do
    tmux split-window -h -t "$SESSION:0" "$(cat "$DEMO_DIR/node$n.cmd")"
    tmux select-pane -T "node$n (wss :744$n)"
done

# Wait for a replica to serve (Raft) or for the single coordinator to come up.
ready_pat="is the LEADER"
[ "$NODES" -eq 1 ] && ready_pat="Waiting for players"
up=0
for _ in $(seq 100); do
    for n in $(seq "$NODES"); do
        tmux capture-pane -p -S - -t "$(node_pane "$n")" | grep -q "$ready_pat" && up=1
    done
    [ "$up" -eq 1 ] && break
    sleep 0.2
done
[ "$up" -eq 1 ] || die "no coordinator came up; see: sudo tmux attach -t $SESSION"

for i in 1 2 3 4; do
    client="sudo -u ${SC_PLAYER_PREFIX}$i -- $SC_PYTHON $SC_INSTALL_DIR/client.py"
    client+=" --server $ADDRESSES --token-file $PLAYERS_DIR/p$i.token"
    client+=" --ca-file $PLAYERS_DIR/server.crt"
    [ "$AUTO" -eq 1 ] && client+=" --script $SC_INSTALL_DIR/demo/p$i.txt"
    [ "$TUI" -eq 1 ] && [ "$AUTO" -eq 0 ] && client+=" --tui"
    tmux split-window -t "$SESSION:0" "$client"
    tmux select-pane -T "${SC_NAMES[$((i-1))]} (syndicate-p$i)"
    tmux select-layout -t "$SESSION:0" tiled >/dev/null
done
# Replicas in a strip across the top, the four players in a grid below.
tmux select-layout -t "$SESSION:0" tiled >/dev/null

joined=0
for _ in $(seq 100); do
    for n in $(seq "$NODES"); do
        c=$(tmux capture-pane -p -S - -t "$(node_pane "$n")" | grep -c "^PlayerJoined" || true)
        [ "$c" -gt "$joined" ] && joined=$c
    done
    [ "$joined" -ge 4 ] && break
    sleep 0.2
done
[ "$joined" -ge 4 ] || echo "warning: only $joined/4 players joined; starting anyway" >&2
for n in $(seq "$NODES"); do        # only the leader accepts; the others say why not
    tmux send-keys -t "$(node_pane "$n")" start Enter
done

cat <<EOF
Demo running in tmux session $SESSION ($NODES coordinator(s), 4 players).
  Fail-over:  sudo scripts/cluster_demo.sh kill <n of the LEADER pane>
  Recovery:   sudo scripts/cluster_demo.sh restart <n>
  Status:     type 'status' in any node pane
  Stop:       sudo scripts/cluster_demo.sh stop
EOF
if [ -t 1 ]; then
    exec tmux attach -t "$SESSION"
fi
echo "watch it with: sudo tmux attach -t $SESSION"
