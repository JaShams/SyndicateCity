"""Shared test helpers: launch a private dbus-daemon from the real policy template."""

import os
import pwd
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from render_bus_conf import render  # noqa: E402


def current_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


class PrivateBus:
    """A dedicated dbus-daemon running as the current user in a temp directory."""

    def __init__(self, directory: str, server_user: str = None, players=(), max_conn: int = 64):
        self.dir = directory
        sock = os.path.join(directory, "bus.sock")
        conf = os.path.join(directory, "bus.conf")
        me = current_user()
        with open(conf, "w") as fh:
            fh.write(render(sock, server_user or me, list(players) or [me], max_conn))
        self.proc = subprocess.Popen(
            ["dbus-daemon", "--config-file", conf, "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline().strip()
        if not line:
            err = self.proc.stderr.read()
            raise RuntimeError(f"dbus-daemon failed: {err}")
        self.address = line
        deadline = time.time() + 5
        while not os.path.exists(sock) and time.time() < deadline:
            time.sleep(0.02)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# ------------------------------------------------------------ in-process stack

import asyncio  # noqa: E402

import ipc  # noqa: E402
from game import Game, Settings  # noqa: E402
from server import ALLOWED_ACTIONS, Coordinator, Limits  # noqa: E402

NAMES = ["alice", "bob", "carol", "dave", "erin", "frank"]


class Stack:
    """Coordinator + N clients on a private bus, all under the current UID.

    Real multi-UID identity is exercised by scripts/integration_multiuser.sh.
    Here, a *test-only* resolver maps each client's unique name to a fake UID
    so several slots can be filled from one account; everything else (policy,
    routing, sender checks) is the production code path.
    """

    def __init__(self, bus: PrivateBus):
        self.bus = bus
        self.fake_uids: dict[str, int] = {}
        self.server = None
        self.coord = None
        self.clients: dict[str, ipc.ClientIPC] = {}
        self.lines: list[str] = []

    async def start(self, n=4, rounds=7, neg=30.0, mov=30.0, auc=30.0, limits=None,
                    audit_path=None):
        roster = [(f"p{i+1}", 50000 + i, NAMES[i]) for i in range(n)]
        self.roster = roster
        game = Game(roster, Settings(rounds=rounds, negotiation_seconds=neg,
                                     movement_seconds=mov, auction_seconds=auc))

        async def resolver(name):
            return self.fake_uids[name]

        self.server = await ipc.GameIPC.start_server(
            self.bus.address, {uid: pid for pid, uid, _ in roster}, ALLOWED_ACTIONS,
            uid_resolver=resolver)
        self.coord = Coordinator(game, self.server, audit_path, limits=limits or Limits(),
                                 echo=self.lines.append)
        self.clock_task = asyncio.ensure_future(self.coord.run_clock())
        return self

    async def connect(self, pid: str, join=True) -> ipc.ClientIPC:
        c = await ipc.GameIPC.connect_client(self.bus.address)
        uid = next(u for p, u, _ in self.roster if p == pid)
        self.fake_uids[c.unique_name] = uid
        if join:
            await c.join()
        self.clients[pid] = c
        return c

    async def connect_all(self):
        for pid, _, _ in self.roster:
            await self.connect(pid)
        return self.clients

    async def start_match(self):
        assert await self.coord.start_match()

    async def close(self):
        self.clock_task.cancel()
        for c in self.clients.values():
            await c.close()
        await self.server.close()
        self.coord.close()


async def drain(client: ipc.ClientIPC, settle: float = 0.15) -> list[dict]:
    """Collect every event that arrives within ``settle`` seconds of quiet."""
    out = []
    while True:
        try:
            out.append(await client.next_event(settle))
        except asyncio.TimeoutError:
            return out


async def wait_for(client: ipc.ClientIPC, etype: str, timeout: float = 5.0, pred=None) -> dict:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        ev = await client.next_event(max(0.01, end - loop.time()))
        if ev["type"] == etype and (pred is None or pred(ev)):
            return ev
