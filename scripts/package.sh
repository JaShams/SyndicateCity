#!/usr/bin/env bash
# Validate deliverables and build dist/syndicate-city.tgz.
#   scripts/package.sh            # requires report.pdf at the project root
#   scripts/package.sh --draft    # allow a missing report (for review builds)
set -euo pipefail
cd "$(dirname "$0")/.."
DRAFT=0; [ "${1:-}" = "--draft" ] && DRAFT=1

FILES=(ipc.py server.py client.py game.py board.py README.md requirements.txt requirements-dev.txt
       config/bus.conf.in config/roster.example.json
       scripts/common.sh scripts/setup.sh scripts/run_server.sh scripts/run_client.sh
       scripts/stop.sh scripts/package.sh scripts/run_tests.sh scripts/integration_multiuser.sh
       scripts/render_bus_conf.py scripts/demo.sh demo/p1.txt demo/p2.txt demo/p3.txt demo/p4.txt
       tests/conftest.py tests/helpers.py tests/test_game.py tests/test_ipc.py
       tests/test_concurrency.py tests/test_security.py tests/test_processes.py
       tests/adversarial_client.py tests/scripted_client.py tests/verify_multiuser.py
       tests/test_script_client.py
       docs/report_outline.md docs/USER_MANUAL.md)
if [ -f report.pdf ]; then
    FILES+=(report.pdf)
elif [ "$DRAFT" -eq 0 ]; then
    echo "error: report.pdf missing (use --draft for a review build)" >&2; exit 1
fi
for f in "${FILES[@]}"; do [ -f "$f" ] || { echo "error: missing $f" >&2; exit 1; }; done

# The communications library must be exactly one file and the only D-Bus user.
if grep -l "dbus_next" server.py client.py game.py board.py >/dev/null 2>&1; then
    echo "error: game modules must not import dbus_next directly" >&2; exit 1
fi
python3 -m py_compile ipc.py server.py client.py game.py board.py

mkdir -p dist
tar --owner=0 --group=0 --transform 's,^,syndicate-city/,' -czf dist/syndicate-city.tgz "${FILES[@]}"
echo "built dist/syndicate-city.tgz ($(du -h dist/syndicate-city.tgz | cut -f1)):"
tar -tzf dist/syndicate-city.tgz | sed 's/^/  /'
