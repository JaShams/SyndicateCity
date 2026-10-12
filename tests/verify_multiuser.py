#!/usr/bin/env python3
"""Check the logs written by scripts/integration_multiuser.sh."""

import json
import sys
from pathlib import Path


def load(path: Path):
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def main() -> int:
    args = sys.argv[1:]
    single_uid = "--single-uid" in args      # in-process dev test: UID checks skipped
    args = [a for a in args if a != "--single-uid"]
    out = Path(args[0])
    expected_uids = [int(x) for x in args[1:]]
    logs = {name: load(out / f"{name}.log") for name in ("p1", "p2", "p3", "p4", "p2-rejoin")}
    failures = []

    def check(cond, msg, uid_check=False):
        if uid_check and single_uid:
            print("SKIP " + msg + " (single-UID dev run)")
            return
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            failures.append(msg)

    joined = {n: next((r for r in rows if r["kind"] == "joined"), None) for n, rows in logs.items()}
    uids = {n: j["uid"] for n, j in joined.items() if j}
    check(len({uids[n] for n in ("p1", "p2", "p3", "p4") if n in uids}) == 4,
          f"four players joined with distinct UIDs {uids}", uid_check=True)
    check(set(uids.values()) <= set(expected_uids[1:]) and expected_uids[0] not in uids.values(),
          "player UIDs are the provisioned accounts, not the service UID", uid_check=True)
    check(joined["p2-rejoin"] and joined["p2-rejoin"]["player"] == "p2"
          and joined["p2-rejoin"]["uid"] == uids.get("p2"),
          "restarted process reclaimed slot p2 under the same UID")
    check(len({j["coordinator"] for j in joined.values() if j}) == 1,
          "every client verified the same coordinator unique name")

    ends = {}
    for n in ("p1", "p3", "p4", "p2-rejoin"):
        e = [r for r in logs[n] if r.get("type") == "MatchEnded"]
        ends[n] = e[-1]["data"] if e else None
    check(all(ends.values()), "all live clients received MatchEnded")
    if all(ends.values()):
        check(len({json.dumps(v, sort_keys=True) for v in ends.values()}) == 1,
              "all clients saw an identical final scoreboard")

    # Privacy: a private message "secret-<from>-to-<name>-rN" may appear only in the
    # sender's and the named recipient's logs.
    names = {"alice": "p1", "bob": "p2", "carol": "p3", "dave": "p4"}
    leaks = []
    for n, rows in logs.items():
        me = n.split("-")[0]
        for r in rows:
            if r.get("type") != "ChatMessage":
                continue
            d = r["data"]
            if d["scope"] in ("private", "group"):
                check_ok = me == d["from"] or me in d["to"]
                if not check_ok:
                    leaks.append((n, d["text"]))
                if r["visibility"] != "private":
                    leaks.append((n, "private chat arrived as broadcast"))
    check(not leaks, f"no private/group chat reached a non-recipient ({len(leaks)} leaks)")
    priv = sum(1 for rows in logs.values() for r in rows
               if r.get("type") == "ChatMessage" and r["data"]["scope"] == "private")
    check(priv > 0, f"private chats were delivered ({priv} deliveries)")

    bid_leaks = [(n, r) for n, rows in logs.items() for r in rows
                 if r.get("type") == "BidStatus" and r["visibility"] != "private"]
    check(not bid_leaks, "bid status only ever arrived as addressed (private) signals")

    forged = sum(1 for rows in logs.values() for r in rows
                 if r.get("type") == "AuctionOpened" and not r["data"].get("auctions"))
    check(forged == 0, "no forged events surfaced")

    perms = (out / "audit-perms.txt").read_text()
    check(perms.startswith("-rw-------") and "AUDIT_READABLE_BY_PLAYER" not in perms,
          "audit log is 0600 and unreadable by players")

    adv = (out / "adversarial.log").read_text()
    check("FAIL" not in adv and "probes behaved securely" in adv, "adversarial probes all passed")

    print(f"\n{'OK' if not failures else 'FAILED'}: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
