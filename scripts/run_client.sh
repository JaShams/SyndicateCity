#!/usr/bin/env bash
# Run one player's client as that player's Linux account.
#   scripts/run_client.sh 1          # becomes syndicate-p1 via sudo
#   scripts/run_client.sh            # already logged in as a player account
set -euo pipefail
source "$(dirname "$0")/common.sh"
if [ $# -ge 1 ] && [[ "$1" =~ ^[1-6]$ ]]; then
    user="${SC_PLAYER_PREFIX}$1"; shift
    exec sudo -u "$user" -- "$SC_PYTHON" "$SC_INSTALL_DIR/client.py" \
        --bus-address-file "$SC_ADDRESS_FILE" "$@"
fi
exec "$SC_PYTHON" "$SC_INSTALL_DIR/client.py" --bus-address-file "$SC_ADDRESS_FILE" "$@"
