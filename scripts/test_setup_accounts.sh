#!/usr/bin/env bash
# Account-safety checks for setup.sh --uninstall, plus a full multi-UID match.
#
# DESTRUCTIVE: creates and deletes users and groups. Run it only in a throwaway
# container, with the source tree mounted read-only at /src:
#
#   docker run --rm -v "$PWD":/src:ro debian:bookworm-slim bash /src/scripts/test_setup_accounts.sh
set -uo pipefail
if [ ! -f /.dockerenv ] && [ ! -f /run/.containerenv ] && [ "${SC_DISPOSABLE:-0}" != 1 ]; then
    echo "refusing to run: this creates and deletes accounts; use a throwaway container" >&2
    echo "(or set SC_DISPOSABLE=1 on a machine you are happy to wreck)" >&2
    exit 2
fi
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
if command -v apt-get >/dev/null; then
    apt-get update -qq >/dev/null
    apt-get install -y -qq dbus python3 python3-venv passwd procps sudo >/dev/null 2>&1
fi
SRC="${SRC:-/src}"
rm -rf /root/sc && cp -a "$SRC" /root/sc && cd /root/sc
pass=0; fail=0
ok()   { echo "PASS: $*"; pass=$((pass+1)); }
bad()  { echo "FAIL: $*"; fail=$((fail+1)); }
check() { local d="$1"; shift; if "$@"; then ok "$d"; else bad "$d"; fi; }
exists() { id "$1" >/dev/null 2>&1; }

echo "=== 1. a real user already named syndicate-p3"
useradd -m syndicate-p3 && echo "precious" > /home/syndicate-p3/notes.txt
out="$(scripts/setup.sh 2>&1)"; rc=$?
check "setup refuses (rc=$rc)" [ "$rc" -ne 0 ]
check "error names the account" grep -q "syndicate-p3 already exists" <<<"$out"
check "nothing created before refusing" bash -c '! id syndicate-p1 >/dev/null 2>&1 && ! getent group syndicate >/dev/null'

echo "=== 2. --adopt-existing"
out="$(scripts/setup.sh --adopt-existing 2>&1)"; rc=$?
check "setup succeeds (rc=$rc)" [ "$rc" -eq 0 ]
[ "$rc" -eq 0 ] || echo "$out" | tail -5
check "record lists p3 as adopted" grep -q "^adopted user syndicate-p3 " /etc/syndicate-city/accounts.record
check "record lists p1 as created" grep -q "^created user syndicate-p1 $(id -u syndicate-p1)$" /etc/syndicate-city/accounts.record
check "record is root-only" [ "$(stat -c '%U %a' /etc/syndicate-city/accounts.record)" = "root 600" ]
cat /etc/syndicate-city/accounts.record

echo "=== 3. re-run is idempotent"
out="$(scripts/setup.sh 2>&1)"; rc=$?
check "second setup succeeds (rc=$rc)" [ "$rc" -eq 0 ]
check "no duplicate record lines" [ "$(wc -l < /etc/syndicate-city/accounts.record)" -eq 6 ]

echo "=== 4. uninstall keeps the adopted account"
out="$(scripts/setup.sh --uninstall 2>&1)"; rc=$?
echo "$out"
check "uninstall succeeds (rc=$rc)" [ "$rc" -eq 0 ]
check "p3 still exists" exists syndicate-p3
check "p3's files intact" grep -q precious /home/syndicate-p3/notes.txt
check "created accounts deleted" bash -c '! id syndicate-p1 >/dev/null 2>&1 && ! id syndicate-p4 >/dev/null 2>&1 && ! id syndicate-server >/dev/null 2>&1'
check "created group deleted" bash -c '! getent group syndicate >/dev/null'
userdel -r syndicate-p3

echo "=== 5. an account replaced behind setup's back (different UID)"
scripts/setup.sh >/dev/null 2>&1 || bad "fresh setup"
userdel -r syndicate-p2 && useradd -m -u 4242 syndicate-p2
out="$(scripts/setup.sh 2>&1)"; rc=$?
check "setup refuses the impostor (rc=$rc)" [ "$rc" -ne 0 ]
check "error explains the UID change" grep -q "replaced by someone else" <<<"$out"
out="$(scripts/setup.sh --uninstall 2>&1)"
echo "$out"
check "uninstall keeps the replaced account" exists syndicate-p2
check "uninstall still removes the rest" bash -c '! id syndicate-p1 >/dev/null 2>&1'
userdel -r syndicate-p2

echo "=== 6. an install from the old setup.sh (no record)"
groupadd --system syndicate
useradd --system --no-create-home --shell /usr/sbin/nologin --gid syndicate syndicate-server
for i in 1 2 3 4; do useradd -m --user-group syndicate-p$i; done
mkdir -p /etc/syndicate-city && echo '{"players": []}' > /etc/syndicate-city/roster.json
out="$(scripts/setup.sh 2>&1)"; rc=$?
check "new setup will not silently take over legacy accounts (rc=$rc)" [ "$rc" -ne 0 ]
check "it points at --force-legacy" grep -q -- "--force-legacy" <<<"$out"
mkdir -p /etc/syndicate-city && echo '{"players": []}' > /etc/syndicate-city/roster.json
out="$(scripts/setup.sh --uninstall 2>&1)"
echo "$out"
check "plain uninstall deletes nothing it cannot prove it made" bash -c 'id syndicate-p1 >/dev/null && id syndicate-server >/dev/null'
out="$(scripts/setup.sh --uninstall --force-legacy 2>&1)"
echo "$out"
check "--force-legacy removes them" bash -c '! id syndicate-p1 >/dev/null 2>&1 && ! id syndicate-server >/dev/null 2>&1 && ! getent group syndicate >/dev/null'

echo "=== 7. clean install, then the game actually runs"
scripts/setup.sh >/dev/null 2>&1 && ok "clean setup" || bad "clean setup"
out="$(scripts/integration_multiuser.sh 2>&1)"; rc=$?
echo "$out" | tail -25
check "multi-UID integration match passes (rc=$rc)" [ "$rc" -eq 0 ]
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
