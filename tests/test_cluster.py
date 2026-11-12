"""Coordinators as real OS processes: crash recovery from the file log, and Raft fail-over.

Each coordinator is ``server.py`` in its own process, killed with SIGKILL (no
chance to clean up). Players are network clients that list every replica.
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

import ipc
from helpers import ROOT, drain, wait_for

ROSTER = [("p1", 50000, "alice"), ("p2", 50001, "bob"), ("p3", 50002, "carol"),
          ("p4", 50003, "dave")]


def run(coro, limit=120):
    return asyncio.run(asyncio.wait_for(coro, limit))


def free_ports(n):
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


class Node:
    """One server.py process with its console on a pipe and its output collected."""

    def __init__(self, args):
        self.args = args
        self.lines: list[str] = []
        self.proc = None
        self.start()

    def start(self):
        self.lines.clear()
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")] + self.args,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, cwd=ROOT)
        threading.Thread(target=self._collect, args=(self.proc,), daemon=True).start()

    def _collect(self, proc):
        for line in proc.stdout:
            self.lines.append(line.rstrip())

    def console(self, line):
        if self.proc.poll() is None:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def output(self):
        return "\n".join(self.lines)

    def kill(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(5)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def common_args(tmp, keys, rounds=3, neg=1.0, mov=1.0, auc=4.0):
    roster = os.path.join(tmp, "roster.json")
    with open(roster, "w") as fh:
        json.dump({"players": [{"id": p, "uid": u, "name": n} for p, u, n in ROSTER]}, fh)
    secret = os.path.join(keys.dir, "token.secret")
    return ["--roster", roster, "--rounds", str(rounds), "--negotiation-seconds", str(neg),
            "--movement-seconds", str(mov), "--auction-seconds", str(auc), "--linger", "5",
            "--tls-cert", keys.cert, "--tls-key", keys.key, "--token-secret-file", secret]


async def connect_all(keys, address, window=20):
    clients = {}
    for pid, _, _ in ROSTER:
        c = await ipc.GameIPC.connect_client(address, token=keys.token(pid), ca_file=keys.cert,
                                             timeout=15, reconnect_window=window)
        await c.join()
        clients[pid] = c
    return clients


async def until(pred, timeout=15.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


def test_coordinator_killed_and_restarted_recovers_from_file_log(net_keys, tmp_path):
    async def body():
        (port,) = free_ports(1)
        state = str(tmp_path / "state")
        args = common_args(str(tmp_path), net_keys, auc=6.0) + [
            "--listen", f"wss://127.0.0.1:{port}", "--state-dir", state]
        node = Node(args)
        try:
            assert await until(lambda: "Waiting for players" in node.output())
            c = await connect_all(net_keys, f"wss://127.0.0.1:{port}")
            node.console("start")
            await wait_for(c["p2"], "AuctionOpened", timeout=10)     # all default to Dockyard
            await c["p2"].request("SubmitBid", {"property_id": 1, "amount": 170},
                                  request_id="p2-bid")
            node.kill()                                               # crash: no cleanup at all
            await asyncio.sleep(1.0)
            node.start()                                              # same state dir
            ev = await wait_for(c["p2"], "_Reconnected", timeout=20)
            assert ev["data"]["snapshot"]["private"]["bids"] == {"1": 170}
            assert "Recovered" in node.output()
            # The idempotency ledger came back too: a retry is answered, not re-applied.
            again = await c["p2"].request("SubmitBid", {"property_id": 1, "amount": 170},
                                          request_id="p2-bid")
            assert again["property_id"] == 1
            closed = await wait_for(c["p1"], "AuctionClosed", timeout=20)
            assert closed["data"]["winner"] == "p2" and closed["data"]["paid"] == 170
            for x in c.values():
                await x.close()
        finally:
            node.stop()
    run(body())


@pytest.fixture
def cluster(net_keys, tmp_path):
    wss = free_ports(3)
    raft = free_ports(3)
    base = common_args(str(tmp_path), net_keys, rounds=3, neg=1.5, mov=1.5, auc=6.0)
    nodes = []
    for i in range(3):
        peers = ",".join(f"127.0.0.1:{raft[j]}" for j in range(3) if j != i)
        nodes.append(Node(base + [
            "--listen", f"wss://127.0.0.1:{wss[i]}", "--node-id", f"node{i+1}",
            "--raft-self", f"127.0.0.1:{raft[i]}", "--raft-peers", peers,
            "--cluster-secret-file", os.path.join(net_keys.dir, "cluster.secret"),
            "--state-dir", str(tmp_path / f"state{i+1}")]))
    address = ",".join(f"wss://127.0.0.1:{p}" for p in wss)
    yield nodes, address
    for n in nodes:
        n.stop()


def leader_of(nodes):
    for n in nodes:
        if n.proc.poll() is None and "is the LEADER" in n.output():
            return n
    return None


def test_raft_cluster_survives_leader_crash_mid_auction(net_keys, cluster):
    nodes, address = cluster

    async def body():
        assert await until(lambda: leader_of(nodes) is not None, 20), \
            "\n---\n".join(n.output() for n in nodes)
        first = leader_of(nodes)
        c = await connect_all(net_keys, address)
        for n in nodes:
            n.console("start")                 # only the leader can; the others refuse
        await wait_for(c["p1"], "AuctionOpened", timeout=15)
        assert all(x.coordinator == c["p1"].coordinator for x in c.values())
        await c["p2"].request("SubmitBid", {"property_id": 1, "amount": 170},
                              request_id="p2-bid")
        await c["p3"].send_private("alice", "before the crash")

        first.kill()                            # SIGKILL the leader mid-auction
        evs = [await wait_for(x, "_Reconnected", timeout=25) for x in c.values()]
        assert evs[1]["data"]["snapshot"]["private"]["bids"] == {"1": 170}
        second = leader_of([n for n in nodes if n is not first])
        assert second is not None and second is not first
        assert all(x.coordinator == c["p1"].coordinator for x in c.values())

        # The replicated idempotency ledger: same id + same payload is answered,
        # same id + different payload is refused -- on a replica that never saw it.
        await c["p2"].request("SubmitBid", {"property_id": 1, "amount": 170},
                              request_id="p2-bid")
        with pytest.raises(ipc.RequestError) as e:
            await c["p2"].request("SubmitBid", {"property_id": 1, "amount": 175},
                                  request_id="p2-bid")
        assert e.value.code == "ID_REUSED"

        closed = await wait_for(c["p4"], "AuctionClosed", timeout=20)
        assert closed["data"]["winner"] == "p2" and closed["data"]["paid"] == 170
        ended = await wait_for(c["p4"], "MatchEnded", timeout=40)
        assert ended["data"]["winners"]
        # The surviving follower applied the same commands and printed the same result.
        follower = next(n for n in nodes if n not in (first, second))
        assert await until(lambda: "MATCH ENDED" in follower.output(), 10)
        assert follower.output().split("MATCH ENDED")[1].splitlines()[0] == \
            second.output().split("MATCH ENDED")[1].splitlines()[0]
        for x in c.values():
            await x.close()

    run(body(), 150)


def test_cluster_rejects_peers_without_the_password(net_keys, cluster, tmp_path):
    """A process that knows the peer addresses but not the secret cannot join the cluster."""
    nodes, address = cluster

    async def body():
        assert await until(lambda: leader_of(nodes) is not None, 20)
        # Raw bytes at a Raft port must not be unpickled; the replicas carry on.
        raft_port = int(nodes[0].args[nodes[0].args.index("--raft-self") + 1].split(":")[1])
        with socket.create_connection(("127.0.0.1", raft_port)) as s:
            s.sendall(b"\x00\x00\x00\x10" + b"cos\nsystem\n(S'id'\ntR." * 4)
        await asyncio.sleep(1.0)
        assert all(n.proc.poll() is None for n in nodes)
        c = await connect_all(net_keys, address)
        snap = await c["p1"].request("GetSnapshot")
        assert snap["public"]["phase"] == "LOBBY"
        for x in c.values():
            await x.close()

    run(body(), 60)
