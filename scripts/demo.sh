#!/usr/bin/env bash
# Self-playing demo: the coordinator plus four clients, each playing a script from
# demo/ as its own player account, side by side in one tmux window.
#   sudo scripts/demo.sh                 # ~3 minute, 4-round match
#   sudo scripts/demo.sh --rounds 2      # extra arguments go to server.py
# Requires `sudo scripts/setup.sh` (4 players) and tmux. Ctrl-b d detaches;
# `sudo tmux kill-session -t syndicate-demo` closes it.
set -euo pipefail
source "$(dirname "$0")/common.sh"
need_root "running each player as its own account"
command -v tmux >/dev/null || die "tmux missing: apt install tmux"
[ -f "$SC_ROSTER" ] || die "not set up; run sudo scripts/setup.sh"
for i in 1 2 3 4; do
    [ -f "$SC_INSTALL_DIR/demo/p$i.txt" ] || die "$SC_INSTALL_DIR/demo/p$i.txt missing; re-run sudo scripts/setup.sh"
done

SESSION=syndicate-demo
tmux kill-session -t "$SESSION" 2>/dev/null || true
our_bus_pid >/dev/null && die "a game is already running; stop it first (sudo scripts/stop.sh)"

# The scripts' @wait timings fit these phase lengths; later arguments override them.
server="$SC_SRC_DIR/scripts/run_server.sh --rounds 4 --negotiation-seconds 25 \
--movement-seconds 6 --auction-seconds 12 --linger 10 $*"
tmux new-session -d -s "$SESSION" -x 240 -y 64 -n demo "$server"
tmux set-option -t "$SESSION" remain-on-exit on >/dev/null
tmux select-pane -t "$SESSION:0.0" -T server

for _ in $(seq 50); do
    tmux capture-pane -p -t "$SESSION:0.0" | grep -q "Waiting for players" && break
    sleep 0.2
done
tmux capture-pane -p -t "$SESSION:0.0" | grep -q "Waiting for players" \
    || die "server did not start; see: sudo tmux attach -t $SESSION"

for i in 1 2 3 4; do
    tmux split-window -t "$SESSION:0" \
        "$SC_SRC_DIR/scripts/run_client.sh $i --script $SC_INSTALL_DIR/demo/p$i.txt"
    tmux select-pane -T "${SC_NAMES[$((i-1))]}"
    tmux select-layout -t "$SESSION:0" tiled >/dev/null
done
# Server in a short strip on top, the four players in a 2x2 grid below.
tmux select-layout -t "$SESSION:0" main-horizontal >/dev/null
tmux resize-pane -t "$SESSION:0.0" -y 12
tmux set-option -t "$SESSION" pane-border-status top >/dev/null

joined=0
for _ in $(seq 75); do
    joined=$(tmux capture-pane -p -S - -t "$SESSION:0.0" | grep -c "^PlayerJoined" || true)
    [ "$joined" -ge 4 ] && break
    sleep 0.2
done
[ "$joined" -ge 4 ] || echo "warning: only $joined/4 players joined; starting anyway" >&2
tmux send-keys -t "$SESSION:0.0" start Enter

if [ -t 1 ]; then
    exec tmux attach -t "$SESSION"
fi
echo "demo running; watch it with: sudo tmux attach -t $SESSION"
