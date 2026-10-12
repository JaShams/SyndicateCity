#!/usr/bin/env bash
# Validate deliverables and build dist/syndicate-city.tgz.
#   scripts/package.sh            # requires report.pdf at the project root
#   scripts/package.sh --draft    # allow a missing report (for review builds)
set -euo pipefail
cd "$(dirname "$0")/.."
DRAFT=0; [ "${1:-}" = "--draft" ] && DRAFT=1

# Package exactly what git considers part of the project: tracked files plus new
# files that .gitignore does not exclude. Edit .gitignore to change what ships.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "error: run from a git checkout" >&2; exit 1; }
FILES=()
while IFS= read -r -d '' f; do
    [ -f "$f" ] && FILES+=("$f")       # skip tracked files deleted in the working tree
done < <(git ls-files -z --cached --others --exclude-standard | sort -zu)
has() { printf '%s\n' "${FILES[@]}" | grep -qxF "$1"; }

# Sanity check: these must exist and must not be ignored.
REQUIRED=(ipc.py server.py client.py game.py board.py README.md USER_MANUAL.md requirements.txt
          config/bus.conf.in scripts/common.sh scripts/setup.sh scripts/run_server.sh
          scripts/run_client.sh scripts/stop.sh scripts/render_bus_conf.py)
for f in "${REQUIRED[@]}"; do has "$f" || { echo "error: missing or ignored: $f" >&2; exit 1; }; done
if [ -f report.pdf ]; then
    has report.pdf || FILES+=(report.pdf)      # the report ships even if .gitignore matches it
elif [ "$DRAFT" -eq 0 ]; then
    echo "error: report.pdf missing (use --draft for a review build)" >&2; exit 1
fi

# The communications library must be exactly one file and the only D-Bus user.
if grep -l "dbus_next" server.py client.py game.py board.py >/dev/null 2>&1; then
    echo "error: game modules must not import dbus_next directly" >&2; exit 1
fi
python3 -m py_compile ipc.py server.py client.py game.py board.py

mkdir -p dist
tar --owner=0 --group=0 --transform 's,^,syndicate-city/,' -czf dist/syndicate-city.tgz "${FILES[@]}"
echo "built dist/syndicate-city.tgz ($(du -h dist/syndicate-city.tgz | cut -f1)):"
tar -tzf dist/syndicate-city.tgz | sed 's/^/  /'
