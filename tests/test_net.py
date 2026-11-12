"""Network transport (wss://): token identity, TLS, routing, fail-over to a restarted server."""

import asyncio
import json
import ssl

import pytest
from websockets.asyncio.client import connect as ws_connect

import ipc
from helpers import NetStack, drain, wait_for


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


def test_token_roundtrip_and_tampering():
    secret = b"k" * 32
    tok = ipc.mint_token(secret, "p3", 60)
    assert ipc.verify_token(secret, tok) == "p3"
    claims, sig = tok.split(".")
    for bad in (tok + "x", claims + "." + sig[::-1], ipc.mint_token(b"z" * 32, "p3", 60),
                ipc.mint_token(secret, "p3", -1), "", None, "a.b.c"):
        with pytest.raises(ipc.RequestError) as e:
            ipc.verify_token(secret, bad)
        assert e.value.code == "AUTH_FAILED"
    # Swapping the claims to another slot invalidates the signature.
    other = ipc.mint_token(secret, "p1", 60).split(".")[0]
    with pytest.raises(ipc.RequestError):
        ipc.verify_token(secret, other + "." + sig)


def test_join_and_routing_over_wss(net_keys):
    async def body():
        s = await NetStack(net_keys).start()
        c = await s.connect_all()
        assert all(x.transport == "wss" for x in c.values())
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
        for evs in got.values():
            for e in evs:
                assert e["visibility"] == ("public" if e["data"]["scope"] == "public" else "private")
        await s.close()
    run(body())


def test_identity_comes_from_token_not_payload(net_keys):
    async def body():
        s = await NetStack(net_keys).start()
        c = await s.connect("p2")
        snap = await c.request("GetSnapshot")
        assert snap["private"]["you"] == "p2"
        with pytest.raises(ipc.RequestError) as e:
            await c.request("SubmitMovement", {"steps": 1, "player_id": "p1"})
        assert e.value.code == "BAD_PAYLOAD"
        await s.close()
    run(body())


def test_bad_tokens_and_strangers_refused(net_keys):
    async def body():
        s = await NetStack(net_keys).start()
        cases = [ipc.mint_token(b"x" * 32, "p1", 60),          # wrong secret
                 ipc.mint_token(net_keys.secret, "p1", -5),     # expired
                 ipc.mint_token(net_keys.secret, "p9", 60),     # not on the roster
                 "garbage"]
        for tok in cases:
            with pytest.raises(ipc.IPCError) as e:
                await ipc.GameIPC.connect_client(s.address, token=tok, ca_file=net_keys.cert)
            assert "AUTH_FAILED" in str(e.value)
        await s.close()
    run(body())


def test_duplicate_session_refused(net_keys):
    async def body():
        s = await NetStack(net_keys).start()
        await s.connect("p1")
        with pytest.raises(ipc.RequestError) as e:
            await s.connect("p1")
        assert e.value.code == "DUPLICATE_SESSION"
        await s.close()
    run(body())


def test_tls_is_mandatory_and_verified(net_keys):
    async def body():
        s = await NetStack(net_keys).start()
        plain = s.address.replace("wss://", "ws://")
        with pytest.raises(ipc.IPCError):
            await ipc.GameIPC.connect_client(f"{s.address},{plain}", token=net_keys.token("p1"))
        with pytest.raises(ipc.IPCError):
            await ipc.GameIPC.start_server("wss://127.0.0.1:0", {}, ["Join"])   # no cert/secret
        # Without the pinned certificate the self-signed server is not trusted.
        with pytest.raises(ipc.IPCError) as e:
            await ipc.GameIPC.connect_client(s.address, token=net_keys.token("p1"), timeout=2)
        assert "CERTIFICATE_VERIFY_FAILED" in str(e.value)
        await s.close()
    run(body())


def test_raw_socket_cannot_skip_hello_or_send_junk(net_keys):
    async def body():
        s = await NetStack(net_keys).start()
        ctx = ssl.create_default_context(cafile=net_keys.cert)
        async with ws_connect(s.address, ssl=ctx) as ws:        # request before hello
            await ws.send(json.dumps({"t": "req", "c": 1, "body": "{}"}))
            reply = json.loads(await ws.recv())
            assert reply["t"] == "refused" and reply["code"] == "BAD_HELLO"
        async with ws_connect(s.address, ssl=ctx) as ws:        # junk after a valid hello
            await ws.send(json.dumps({"t": "hello", "v": 1, "token": net_keys.token("p1")}))
            assert json.loads(await ws.recv())["t"] == "welcome"
            await ws.send("not json")
            with pytest.raises(Exception):
                for _ in range(3):
                    await asyncio.wait_for(ws.recv(), 3)
        await s.close()
    run(body())


def test_private_events_never_reach_other_sockets(net_keys):
    async def body():
        s = await NetStack(net_keys).start(neg=0.2, mov=0.3, auc=5)
        c = await s.connect_all()
        await s.start_match()
        await wait_for(c["p1"], "AuctionOpened", timeout=5)   # everyone defaults to Dockyard
        for x in c.values():
            await drain(x, 0.1)
        await c["p3"].request("SubmitBid", {"property_id": 1, "amount": 199})
        seen = {pid: await drain(x) for pid, x in c.items()}
        assert any(e["type"] == "BidStatus" for e in seen["p3"])
        for pid in ("p1", "p2", "p4"):
            assert all("199" not in str(e) for e in seen[pid])
        await s.close()
    run(body())


def test_full_short_match_over_wss(net_keys):
    async def body():
        s = await NetStack(net_keys).start(rounds=2, neg=0.4, mov=0.4, auc=0.4)
        c = await s.connect_all()
        await s.start_match()
        await wait_for(c["p1"], "PhaseChanged", pred=lambda e: e["data"]["phase"] == "MOVEMENT")
        for pid, x in c.items():
            await x.request("SubmitMovement", {"steps": 1 if pid in ("p1", "p2") else 3})
        await wait_for(c["p1"], "AuctionOpened")
        await c["p1"].request("SubmitBid", {"property_id": 1, "amount": 150})
        await c["p2"].request("SubmitBid", {"property_id": 1, "amount": 140})
        closed = await wait_for(c["p3"], "AuctionClosed")
        assert closed["data"]["winner"] == "p1" and "140" not in str(closed)
        ended = await wait_for(c["p4"], "MatchEnded", timeout=10)
        assert ended["data"]["winners"]
        s.coord.game.check_invariants()
        await s.close()
    run(body())


def test_client_reconnects_and_rejoins_after_drop(net_keys):
    async def body():
        s = await NetStack(net_keys).start(neg=0.2, mov=0.3, auc=10)
        c = await s.connect_all()
        await s.start_match()
        await wait_for(c["p2"], "AuctionOpened", timeout=5)
        await c["p2"].request("SubmitBid", {"property_id": 1, "amount": 170})
        await s.server.drop_all("test drop")                 # every socket closes
        ev = await wait_for(c["p2"], "_Reconnected", timeout=10)
        assert ev["data"]["snapshot"]["private"]["bids"] == {"1": 170}
        assert c["p2"].reconnects == 1
        snap = await c["p2"].request("GetSnapshot")
        assert snap["private"]["you"] == "p2"
        await s.close()
    run(body())
