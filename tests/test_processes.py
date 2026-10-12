"""Separate-process match: scripted players as real OS processes, crash + rejoin.

This mirrors scripts/integration_multiuser.sh on a single account. Each client
process is mapped to a roster slot by its PID (test-only resolver using
GetConnectionUnixProcessID); the multi-UID script uses real UIDs instead.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time

import ipc
from helpers import ROOT, Stack

PY = sys.executable
SCRIPTED = os.path.join(ROOT, "tests", "scripted_client.py")
ADVERSARIAL = os.path.join(ROOT, "tests", "adversarial_client.py")
VERIFY = os.path.join(ROOT, "tests", "verify_multiuser.py")


def test_scripted_processes_full_match_with_crash(private_bus, tmp_path):
    async def body():
        s = Stack(private_bus)
        pid_to_uid: dict[int, int] = {}

        async def by_pid(name):
            reply = await ipc._driver_call(s.server._bus, "GetConnectionUnixProcessID", "s", [name])
            return pid_to_uid[int(reply[0])]

        await s.start(rounds=3, neg=1.0, mov=1.0, auc=1.5,
                      audit_path=str(tmp_path / "audit.jsonl"))
        s.server._resolve_uid = by_pid
        uid_of = {pid: uid for pid, uid, _ in s.roster}

        def spawn(slot, log_name, extra=()):
            log = open(tmp_path / f"{log_name}.log", "w")
            proc = subprocess.Popen([PY, SCRIPTED, "--bus-address", private_bus.address, *extra],
                                    stdout=log, stderr=subprocess.STDOUT)
            pid_to_uid[proc.pid] = uid_of[slot]
            return proc

        # Adversarial probes from "player 4" in the lobby (subset valid on a same-UID bus).
        adv = subprocess.Popen([PY, ADVERSARIAL, "--bus-address", private_bus.address,
                                "--eavesdrop-seconds", "1", "phantom", "direct", "eavesdrop",
                                "identity", "replay", "late", "malformed"],
                               stdout=open(tmp_path / "adversarial.log", "w"),
                               stderr=subprocess.STDOUT)
        pid_to_uid[adv.pid] = uid_of["p4"]
        while adv.poll() is None:
            await asyncio.sleep(0.1)
        assert adv.returncode == 0, (tmp_path / "adversarial.log").read_text()

        procs = {slot: spawn(slot, slot) for slot in ("p1", "p2", "p3", "p4")}
        for _ in range(100):
            if all(s.coord.game.players[p].connected for p in procs):
                break
            await asyncio.sleep(0.05)
        await s.start_match()

        while not s.coord.game.auctions:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        procs["p2"].send_signal(signal.SIGKILL)               # crash during the auction
        procs["p2"].wait()
        for _ in range(100):
            if not s.coord.game.players["p2"].connected:
                break
            await asyncio.sleep(0.05)
        procs["p2-rejoin"] = spawn("p2", "p2-rejoin")

        deadline = time.time() + 40
        live = ("p1", "p3", "p4", "p2-rejoin")
        while time.time() < deadline and any(procs[n].poll() is None for n in live):
            await asyncio.sleep(0.1)
        for n in live:
            assert procs[n].returncode == 0, (n, (tmp_path / f"{n}.log").read_text()[-2000:])

        mode = oct((tmp_path / "audit.jsonl").stat().st_mode)
        (tmp_path / "audit-perms.txt").write_text(
            ("-rw-------" if mode.endswith("600") else mode) + " audit.jsonl\n")
        s.coord.game.check_invariants()
        await s.close()

    asyncio.run(asyncio.wait_for(body(), 90))
    res = subprocess.run([PY, VERIFY, str(tmp_path), "--single-uid"], capture_output=True, text=True)
    print(res.stdout)
    assert res.returncode == 0, res.stdout + res.stderr
