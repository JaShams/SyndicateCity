"""Concurrent requests over the real bus: no overspending, no double settlement."""

import asyncio
import statistics
import time

import ipc
from helpers import Stack, wait_for


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


async def to_auction_on(s, c, tiles):
    """Arrange positions so the given tiles are discovered, then reach AUCTION."""
    g = s.coord.game
    await wait_for(c["p1"], "PhaseChanged", pred=lambda e: e["data"]["phase"] == "MOVEMENT")
    discoverers = list(c)[:len(tiles)]
    for pid, tile in zip(discoverers, tiles):
        g.players[pid].position = tile - 1        # test setup: place pawns directly
    for pid in c:
        if pid not in discoverers:
            g.players[pid].position = 8
    await wait_for(c["p1"], "AuctionOpened")


def test_concurrent_bids_cannot_exceed_cash(private_bus):
    async def body():
        s = await Stack(private_bus).start(neg=0.2, mov=0.4, auc=3)
        c = await s.connect_all()
        await s.start_match()
        await to_auction_on(s, c, [1, 4])
        attacker = c["p4"]                      # 630 cash after Supply Drop
        reqs = [attacker.request("SubmitBid", {"property_id": t, "amount": 400})
                for t in (1, 4) for _ in range(10)]
        results = await asyncio.gather(*reqs, return_exceptions=True)
        ok = [r for r in results if isinstance(r, dict)]
        errs = [r for r in results if isinstance(r, ipc.RequestError)]
        assert ok and errs and all(e.code == "INSUFFICIENT_FUNDS" for e in errs)
        g = s.coord.game
        assert g.reserved("p4") <= g.players["p4"].cash
        bids = {t: a.active_bids.get("p4") for t, a in g.auctions.items()}
        assert sum(v for v in bids.values() if v) == 400
        g.check_invariants()
        await s.close()
    run(body())


def test_concurrent_accepts_settle_once(private_bus):
    async def body():
        s = await Stack(private_bus).start(neg=10)
        c = await s.connect_all()
        await s.start_match()
        g = s.coord.game
        g._transfer_property(2, "p2")
        offer = await c["p2"].request("CreateTradeOffer",
                                      {"buyer": "alice", "property_id": 2, "price": 200})
        oid = offer["offer_id"]
        results = await asyncio.gather(
            *[c["p1"].request("AcceptTradeOffer", {"offer_id": oid}) for _ in range(10)],
            return_exceptions=True)
        assert sum(isinstance(r, dict) for r in results) == 1
        assert g.players["p1"].cash == 400 and g.players["p2"].cash == 800
        # Same request id replayed concurrently: one effect, identical responses.
        g._transfer_property(5, "p3")
        o2 = await c["p3"].request("CreateTradeOffer", {"buyer": "bob", "property_id": 5, "price": 50})
        rid = "replay-1"
        replies = await asyncio.gather(
            *[c["p2"].request("AcceptTradeOffer", {"offer_id": o2["offer_id"]}, request_id=rid)
              for _ in range(5)], return_exceptions=True)
        assert all(isinstance(r, dict) and r == replies[0] for r in replies)
        assert g.players["p2"].cash == 750 and g.players["p3"].cash == 650
        g.check_invariants()
        await s.close()
    run(body())


def test_simultaneous_movement_and_latency(private_bus):
    async def body():
        s = await Stack(private_bus).start(neg=0.2, mov=1.5)
        c = await s.connect_all()
        await s.start_match()
        await wait_for(c["p1"], "PhaseChanged", pred=lambda e: e["data"]["phase"] == "MOVEMENT")
        lat = []

        async def timed(x, steps):
            t0 = time.perf_counter()
            await x.request("SubmitMovement", {"steps": steps})
            lat.append(time.perf_counter() - t0)

        await asyncio.gather(*[timed(x, (i % 3) + 1) for i, x in enumerate(c.values()) for _ in range(3)])
        reveal = await wait_for(c["p2"], "MovementRevealed")
        steps = {m["player"]: m["steps"] for m in reveal["data"]["moves"]}
        assert steps == {"p1": 1, "p2": 2, "p3": 3, "p4": 1}
        assert statistics.median(lat) < 0.25
        await s.close()
    run(body())
