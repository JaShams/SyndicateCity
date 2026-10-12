"""client.py --script: parser rules and a scripted match played by real client processes."""

import asyncio
import os
import subprocess
import sys
import time

import pytest

import ipc
from client import parse_script
from helpers import ROOT, Stack

PY = sys.executable
CLIENT = os.path.join(ROOT, "client.py")


def test_parse_script_directives_and_commands():
    steps = parse_script("""
        # comment
        @round 2
        @round 3 Auction
        @wait 1.5
        @accept Bob
        @reject
        hello there
        /bid Dockyard 150
        @end
    """)
    assert [(k, a) for _, k, a in steps] == [
        ("round", [2, "NEGOTIATION"]), ("round", [3, "AUCTION"]), ("wait", [1.5]),
        ("accept", ["bob"]), ("reject", [None]), ("cmd", ["hello there"]),
        ("cmd", ["/bid Dockyard 150"]), ("end", [])]


@pytest.mark.parametrize("line", ["@round", "@round x", "@round 1 settlement", "@wait",
                                  "@accept a b", "@end now", "@dance"])
def test_parse_script_rejects_bad_directives(line):
    with pytest.raises(ValueError, match="line 2"):
        parse_script("/say hi\n" + line)


SCRIPTS = {
    "p1": """
@round 1
/say hello from alice
@round 1 movement
/move 1
@round 1 auction
/bid Dockyard 150
@round 2
@accept dave
@round 2 movement
/move 2
@round 2 auction
/say round 2 has no auction: every move lands on a special tile
@end
/say never sent
""",
    "p2": """
@round 1 movement
/move 2
@round 2
@wait 0.3
/tell carol psst
@round 2 movement
/move 1
""",
    "p3": """
@round 1 movement
/move 3
@round 2 movement
/move 3
""",
    "p4": """
@round 1 movement
/move 1
@round 1 auction
/bid Dockyard 165
@round 2
@wait 0.3
/offer alice Dockyard 200
@round 2 movement
/move 2
""",
}


def test_scripted_clients_play_a_match(private_bus, tmp_path):
    async def body():
        s = Stack(private_bus)
        pid_to_uid: dict[int, int] = {}

        async def by_pid(name):
            reply = await ipc._driver_call(s.server._bus, "GetConnectionUnixProcessID", "s", [name])
            return pid_to_uid[int(reply[0])]

        await s.start(rounds=3, neg=2.0, mov=1.0, auc=1.5)
        s.server._resolve_uid = by_pid
        uid_of = {pid: uid for pid, uid, _ in s.roster}
        procs = {}
        for slot, text in SCRIPTS.items():
            (tmp_path / f"{slot}.txt").write_text(text)
            log = open(tmp_path / f"{slot}.log", "w")
            procs[slot] = subprocess.Popen(
                [PY, CLIENT, "--bus-address", private_bus.address, "--no-color",
                 "--script", str(tmp_path / f"{slot}.txt")],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            pid_to_uid[procs[slot].pid] = uid_of[slot]
        for _ in range(100):
            if all(s.coord.game.players[p].connected for p in procs):
                break
            await asyncio.sleep(0.05)
        await s.start_match()

        deadline = time.time() + 30
        while time.time() < deadline and any(p.poll() is None for p in procs.values()):
            await asyncio.sleep(0.1)
        logs = {slot: (tmp_path / f"{slot}.log").read_text() for slot in procs}
        for slot, p in procs.items():
            assert p.returncode == 0, (slot, logs[slot][-2000:])
        g = s.coord.game
        await s.close()
        return g, logs

    g, logs = asyncio.run(asyncio.wait_for(body(), 60))
    # Round 1: dave outbids alice for Dockyard; round 2: dave sells it to alice by trade.
    assert g.properties[1].owner_id == "p1"
    assert "[SCRIPT R1 NEGOTIATION] /say hello from alice" in logs["p1"]
    assert "alice: hello from alice" in logs["p3"]
    assert "bob -> you: psst" in logs["p3"] and "psst" not in logs["p1"]
    assert "/accept " in logs["p1"] and "ACCEPTED" in logs["p4"]
    assert "skipping round 2 auction" in logs["p1"]
    assert "never sent" not in logs["p3"]
    assert all("MATCH OVER" in text for text in logs.values())
    errors = [(slot, line) for slot, text in logs.items() for line in text.splitlines()
              if line.startswith("[ERROR")]
    assert not errors, errors
