"""Integration tests over a real private dbus-daemon (PRD 11.2, single-UID variant)."""

import asyncio

import pytest

import ipc
from helpers import Stack, drain, wait_for


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


def test_join_snapshot_and_lobby_events(private_bus):
    async def body():
        s = await Stack(private_bus).start()
        a = await s.connect("p1")
        b = await ipc.GameIPC.connect_client(private_bus.address)
        s.fake_uids[b.unique_name] = 50001
        snap = await b.join()
        assert snap["private"]["you"] == "p2" and snap["public"]["phase"] == "LOBBY"
        assert [p["name"] for p in snap["public"]["players"]] == ["alice", "bob", "carol", "dave"]
        ev = await wait_for(a, "PlayerJoined", pred=lambda e: e["data"]["player"] == "p2")
        assert ev["visibility"] == "public"
        s.clients["p2"] = b
        await s.close()
    run(body())


def test_unknown_uid_and_unjoined_calls_rejected(private_bus):
    async def body():
        s = await Stack(private_bus).start()
        stranger = await ipc.GameIPC.connect_client(private_bus.address)
        s.fake_uids[stranger.unique_name] = 4242
        with pytest.raises(ipc.RequestError) as e:
            await stranger.join()
        assert e.value.code == "UNKNOWN_IDENTITY"
        with pytest.raises(ipc.RequestError) as e:
            await stranger.request("SubmitMovement", {"steps": 1})
        assert e.value.code == "NOT_JOINED"
        await stranger.close()
        await s.close()
    run(body())


def test_duplicate_session_rejected_and_start_needs_everyone(private_bus):
    async def body():
        s = await Stack(private_bus).start()
        await s.connect("p1")
        with pytest.raises(ipc.RequestError) as e:
            await s.connect("p1")
        assert e.value.code == "DUPLICATE_SESSION"
        assert not await s.coord.start_match()            # only one player present
        assert s.coord.game.phase.value == "LOBBY"
        await s.close()
    run(body())


def test_public_private_and_group_routing(private_bus):
    async def body():
        s = await Stack(private_bus).start()
        c = await s.connect_all()
        await s.start_match()
        for x in c.values():
            await drain(x)
        await c["p1"].send_public("hello all")
        await c["p1"].send_private("bob", "secret for bob")
        await c["p1"].send_group(["bob", "carol"], "group plan")
        got = {pid: [e for e in await drain(x) if e["type"] == "ChatMessage"]
               for pid, x in c.items()}
        texts = {pid: sorted(e["data"]["text"] for e in evs) for pid, evs in got.items()}
        assert texts["p1"] == ["group plan", "hello all", "secret for bob"]
        assert texts["p2"] == ["group plan", "hello all", "secret for bob"]
        assert texts["p3"] == ["group plan", "hello all"]
        assert texts["p4"] == ["hello all"]
        for pid, evs in got.items():
            for e in evs:
                expected = "public" if e["data"]["scope"] == "public" else "private"
                assert e["visibility"] == expected      # derived from D-Bus destination
        await s.close()
    run(body())


def test_public_sequence_numbers_are_contiguous(private_bus):
    async def body():
        s = await Stack(private_bus).start()
        c = await s.connect_all()
        await s.start_match()
        for _ in range(5):
            await c["p2"].send_public("x")
        evs = [e for e in await drain(c["p4"]) if e["visibility"] == "public"]
        pseqs = [e["pseq"] for e in evs]
        assert pseqs == list(range(pseqs[0], pseqs[0] + len(pseqs)))
        assert not any("missed_public" in e for e in evs)
        await s.close()
    run(body())


def test_full_short_match_end_to_end(private_bus):
    async def body():
        s = await Stack(private_bus).start(rounds=2, neg=0.4, mov=0.4, auc=0.4)
        c = await s.connect_all()
        await s.start_match()
        await wait_for(c["p1"], "PhaseChanged", pred=lambda e: e["data"]["phase"] == "MOVEMENT")
        for pid, x in c.items():
            await x.request("SubmitMovement", {"steps": 1 if pid in ("p1", "p2") else 3})
        opened = await wait_for(c["p1"], "AuctionOpened")
        assert [a["property_id"] for a in opened["data"]["auctions"]] == [1]
        await c["p1"].request("SubmitBid", {"property_id": 1, "amount": 150})
        await c["p2"].request("SubmitBid", {"property_id": 1, "amount": 140})
        closed = await wait_for(c["p3"], "AuctionClosed")
        assert closed["data"] == {"property_id": 1, "name": "Dockyard", "winner": "p1",
                                  "paid": 135, "discount": 15}
        assert "140" not in str(closed)
        ended = await wait_for(c["p4"], "MatchEnded", timeout=10)
        assert ended["data"]["winners"]
        snap = await c["p1"].request("GetSnapshot")
        assert snap["public"]["phase"] == "ENDED"
        s.coord.game.check_invariants()
        await s.close()
    run(body())


def test_private_bid_status_only_to_bidder(private_bus):
    async def body():
        s = await Stack(private_bus).start(neg=0.2, mov=0.3, auc=5)
        c = await s.connect_all()
        await s.start_match()
        await wait_for(c["p1"], "PhaseChanged", pred=lambda e: e["data"]["phase"] == "MOVEMENT")
        await wait_for(c["p1"], "AuctionOpened")            # everyone defaults to Dockyard
        for x in c.values():
            await drain(x, 0.1)
        await c["p3"].request("SubmitBid", {"property_id": 1, "amount": 199})
        seen = {pid: await drain(x) for pid, x in c.items()}
        assert any(e["type"] == "BidStatus" for e in seen["p3"])
        for pid in ("p1", "p2", "p4"):
            assert all("199" not in str(e) for e in seen[pid])
        await s.close()
    run(body())


def test_crash_during_auction_then_rejoin_keeps_bid(private_bus):
    async def body():
        s = await Stack(private_bus).start(neg=0.2, mov=0.3, auc=1.5)
        c = await s.connect_all()
        await s.start_match()
        await wait_for(c["p2"], "AuctionOpened", timeout=5)
        await c["p2"].request("SubmitBid", {"property_id": 1, "amount": 170})
        await c["p2"].close()                               # simulated crash
        await wait_for(c["p1"], "PlayerDisconnected")
        assert not s.coord.game.players["p2"].connected
        fresh = await s.connect("p2")                       # same (fake) UID, new process
        snap = await fresh.request("GetSnapshot")
        assert snap["private"]["bids"] == {"1": 170}
        closed = await wait_for(c["p1"], "AuctionClosed", timeout=5)
        assert closed["data"]["winner"] == "p2"
        await wait_for(c["p1"], "PhaseChanged", pred=lambda e: e["data"]["phase"] == "NEGOTIATION")
        snap = await fresh.request("GetSnapshot")
        p2 = next(p for p in snap["public"]["players"] if p["id"] == "p2")
        assert p2["props"] == [1] and p2["cash"] == 600 - 155 and p2["connected"]
        await s.close()
    run(body())


def test_second_coordinator_cannot_take_name(private_bus):
    async def body():
        s = await Stack(private_bus).start()
        with pytest.raises(ipc.IPCError):
            await ipc.GameIPC.start_server(private_bus.address, {}, ["Join"])
        await s.close()
    run(body())


def test_client_refuses_non_dedicated_bus():
    async def body():
        with pytest.raises(ipc.IPCError):
            await ipc.GameIPC.connect_client("tcp:host=127.0.0.1,port=1")
        with pytest.raises(ipc.IPCError):
            await ipc.GameIPC.connect_client("")
    run(body())


def test_game_modules_do_not_import_dbus_directly():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("server.py", "client.py", "tui.py", "game.py", "board.py"):
        text = (root / name).read_text()
        assert "dbus_next" not in text, name
    assert "dbus_next" in (root / "ipc.py").read_text()


def test_rejoin_before_disconnect_notice_is_processed(private_bus):
    """A restarted client must not be locked out by its dead predecessor's binding."""
    async def body():
        s = await Stack(private_bus).start()
        old = await s.connect("p3")
        old_name = old.unique_name
        # Simulate the race: the server misses NameOwnerChanged for the old connection.
        s.server._bus.remove_message_handler(s.server._on_message)
        await old.close()
        await asyncio.sleep(0.2)
        s.server._bus.add_message_handler(s.server._on_message)
        assert s.server.player_for(old_name) == "p3"          # stale binding still present
        fresh = await s.connect("p3")
        snap = await fresh.request("GetSnapshot")
        assert snap["private"]["you"] == "p3"
        assert s.server.player_for(old_name) is None
        await s.close()
    run(body())
