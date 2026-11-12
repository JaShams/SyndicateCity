"""Command log: deterministic replay and crash recovery from the file log."""

import asyncio
import json
import os

import pytest

import cmdlog
from game import Game
from helpers import NetStack, drain, wait_for


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


def state(game: Game) -> dict:
    """Everything that matters about a game, independent of the clock."""
    snap = game.public_snapshot(0.0)
    snap.pop("remaining", None)
    return {"public": snap, "private": {pid: game.private_view(pid) for pid in game.players},
            "deadline": round(game.phase_deadline_monotonic, 6)}


def header_for(s) -> dict:
    return {"k": "boot", "v": 1, "match": "m1", "roster": [list(r) for r in s.roster],
            "settings": vars(s.settings)}


def test_replay_rebuilds_identical_state_and_ledger(net_keys, tmp_path):
    async def body():
        path = str(tmp_path / "commands.jsonl")
        s = NetStack(net_keys)
        flog = cmdlog.FileLog(path)
        s.roster = [(f"p{i+1}", 50000 + i, n) for i, n in enumerate(["alice", "bob", "carol", "dave"])]
        from game import Settings
        s.settings = Settings(rounds=2, negotiation_seconds=0.3, movement_seconds=0.3,
                              auction_seconds=30, skip_empty_negotiation=False)
        flog.open(header_for(s))
        await s.start(rounds=2, neg=0.3, mov=0.3, auc=30, log=flog)
        c = await s.connect_all()
        await s.start_match()
        await c["p1"].send_private("bob", "psst")
        await wait_for(c["p1"], "AuctionOpened", timeout=5)
        bid = await c["p3"].request("SubmitBid", {"property_id": 1, "amount": 150},
                                    request_id="bid-1")
        before = state(s.coord.game)
        ledger = {pid: dict(v) for pid, v in s.coord.dedup.items()}
        await s.close()
        await flog.close()

        # A brand-new coordinator rebuilds the same game from the file alone.
        s2 = NetStack(net_keys)
        flog2 = cmdlog.FileLog(path)
        s2.roster, s2.settings = s.roster, s.settings
        header, history = flog2.open(header_for(s))
        assert header["match"] == "m1" and len(history) == flog.count
        await s2.start(rounds=2, neg=0.3, mov=0.3, auc=30, log=flog2, history=history)
        after = state(s2.coord.game)
        for pid in after["public"]["players"]:
            pid["connected"] = True           # replay marks sessions stale; compare the rest
        for pid in before["public"]["players"]:
            pid["connected"] = True
        assert after == before
        assert {pid: dict(v) for pid, v in s2.coord.dedup.items()} == ledger
        # The idempotency ledger survived: the same request id replays its outcome.
        c3 = await s2.connect("p3")
        again = await c3.request("SubmitBid", {"property_id": 1, "amount": 150},
                                 request_id="bid-1")
        assert again == bid
        with pytest.raises(Exception) as e:
            await c3.request("SubmitBid", {"property_id": 1, "amount": 160}, request_id="bid-1")
        assert "ID_REUSED" in repr(e.value) or getattr(e.value, "code", "") == "ID_REUSED"
        await s2.close()
        await flog2.close()
    run(body())


def test_torn_tail_is_discarded_and_wrong_match_refused(tmp_path):
    path = str(tmp_path / "commands.jsonl")
    header = {"k": "boot", "v": 1, "match": "m1", "roster": [["p1", 1, "a"]], "settings": {"r": 1}}
    flog = cmdlog.FileLog(path)
    flog.bind(lambda cmd, deliver: ("ok", {}))
    flog.open(header)
    asyncio.run(flog.submit({"k": "start", "now": 1.0}))
    asyncio.run(flog.close())
    with open(path, "ab") as fh:
        fh.write(b'{"k":"advance","no')            # crash in the middle of a write
    flog = cmdlog.FileLog(path)
    got, history = flog.open(header)
    assert history == [{"k": "start", "now": 1.0}]
    asyncio.run(flog.close())
    with open(path, "rb") as fh:
        assert fh.read().endswith(b"}\n")           # the torn bytes were truncated away
    other = dict(header, roster=[["p1", 2, "a"]])
    with pytest.raises(ValueError):
        cmdlog.FileLog(path).open(other)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_game_clock_freezes_and_resumes():
    t = [100.0]
    clock = cmdlog.GameClock(lambda: t[0])
    t[0] = 105.0
    assert clock.now() == 5.0
    clock.observe(5.0)
    clock.freeze()
    t[0] = 200.0
    assert clock.now() == 5.0                     # no coordinator: time stands still
    clock.resume()
    t[0] = 202.0
    assert clock.now() == 7.0
    clock.observe(50.0)                           # never behind a committed command
    assert clock.now() == 50.0
