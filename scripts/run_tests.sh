#!/usr/bin/env bash
# Run the unit, integration (private bus, current account) and security suites.
# No root needed. Creates .venv on first use.
set -euo pipefail
cd "$(dirname "$0")/.."
if ! .venv/bin/python -c "import dbus_next, pytest" 2>/dev/null; then
    rm -rf .venv
    python3 -m venv .venv || { echo "python3 -m venv failed: sudo apt install python3-venv" >&2; exit 1; }
    .venv/bin/pip install --quiet -r requirements.txt -r requirements-dev.txt
fi
exec .venv/bin/python -m pytest -q tests "$@"
