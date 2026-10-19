"""Security tests (PRD 7.4, 11.3).

Two kinds:
* policy tests run a dbus-daemon whose *service user is root*, so the test
  account is an ordinary non-service UID, exactly like a player;
* application tests run the real coordinator and attack it with
  tests/adversarial_client.py probes.
"""

import asyncio
import json
import shutil
import tempfile
import xml.etree.ElementTree as ET

import pytest
from dbus_next import Message, MessageType

import adversarial_client as adv
import ipc
from helpers import PrivateBus, Stack, drain, wait_for
from render_bus_conf import render
from server import Limits


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


@pytest.fixture
def player_only_bus():
    """Bus where the coordinator account is root: the test UID is just a player."""
    if shutil.which("dbus-daemon") is None:
        pytest.skip("dbus-daemon not installed")
    d = tempfile.mkdtemp(prefix="syndicate-policy-")
    bus = PrivateBus(d, server_user="root")
    yield bus
    bus.stop()
    shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------- bus policy

def test_player_cannot_own_coordinator_name(player_only_bus):
    r = run(adv.counterfeit_coordinator(player_only_bus.address))
    assert r.passed and "AccessDenied" in r.observed


def test_player_cannot_become_monitor(player_only_bus):
    assert run(adv.become_monitor(player_only_bus.address)).passed


def test_service_account_cannot_become_monitor(private_bus):
    """Monitoring is denied to everyone, the daemon's own UID included."""
    assert run(adv.become_monitor(private_bus.address)).passed


def test_debug_monitor_policy_only_when_requested():
    def parse(**kw):
        root = ET.fromstring(render("/run/x/bus.sock", "svc", ["p1"], **kw))
        connect = {r.get("user") for r in root.iterfind("policy[@context='default']/allow")
                   if r.get("user")}
        monitor = [p.get("user") for p in root.iterfind("policy")
                   for r in p.iterfind("allow")
                   if r.get("send_interface") == "org.freedesktop.DBus.Monitoring"]
        assert not [e for e in root.iter() if e.get("eavesdrop")]
        return connect, monitor

    assert parse() == ({"svc", "p1"}, [])
    assert parse(debug_monitor=True) == ({"svc", "p1", "root"}, ["root"])


def test_player_cannot_start_server(player_only_bus):
    with pytest.raises(Exception):
        run(ipc.GameIPC.start_server(player_only_bus.address, {}, ["Join"]))


def test_player_signals_never_reach_other_players(player_only_bus):
    async def body():
        listener = await adv.raw_bus(player_only_bus.address)
        got = []
        listener.add_message_handler(
            lambda m: got.append(m) if m.message_type == MessageType.SIGNAL
            and m.member == ipc.EVENT_SIGNAL else None)
        await adv.driver(listener, "AddMatch", "s", ["type='signal'"])
        attacker = await adv.raw_bus(player_only_bus.address)
        attacker.send(Message.new_signal(ipc.OBJECT_PATH, ipc.INTERFACE, ipc.EVENT_SIGNAL,
                                         "s", ["{}"]))
        # Also try a unicast forged signal straight at the listener.
        attacker.send(Message(message_type=MessageType.SIGNAL, destination=listener.unique_name,
                              path=ipc.OBJECT_PATH, interface=ipc.INTERFACE,
                              member=ipc.EVENT_SIGNAL, signature="s", body=["{}"]))
        await asyncio.sleep(0.3)
        assert got == []
        listener.disconnect()
        attacker.disconnect()
    run(body())


def test_player_cannot_call_other_players(player_only_bus):
    async def body():
        victim = await adv.raw_bus(player_only_bus.address)
        attacker = await adv.raw_bus(player_only_bus.address)
        reply = await attacker.call(Message(destination=victim.unique_name, path="/",
                                            interface="org.freedesktop.DBus.Peer", member="Ping"))
        assert adv.is_denied(reply)
        victim.disconnect()
        attacker.disconnect()
    run(body())


# -------------------------------------------- coordinator + clients, same UID

def test_forged_signal_ignored_by_client_provenance_check(private_bus):
    """On a same-UID dev bus the policy cannot stop the signal; the client must."""
    async def body():
        s = await Stack(private_bus).start()
        c = await s.connect_all()
        await s.start_match()
        await drain(c["p1"])
        r = await adv.phantom_auction(private_bus.address)
        assert r.passed
        evs = await drain(c["p1"], 0.3)
        assert not any(e["type"] == "AuctionOpened" for e in evs)
        assert any(e["type"] == "_ForgedSignalIgnored" for e in evs)
        assert c["p1"].forged_signals >= 1
        await s.close()
    run(body())


def test_eavesdrop_match_rule_gets_no_private_events(private_bus):
    async def body():
        s = await Stack(private_bus).start()
        c = await s.connect_all()
        await s.start_match()
        spy = asyncio.ensure_future(adv.eavesdrop(private_bus.address, 1.0))
        await asyncio.sleep(0.3)
        for _ in range(5):
            await c["p1"].send_private("bob", "the vault code is 1234")
            await c["p3"].send_group(["dave", "bob"], "bid 200 on tech park")
        r = await spy
        assert r.passed, r.observed
        await s.close()
    run(body())


def test_application_probes(private_bus):
    async def body():
        s = await Stack(private_bus).start(limits=Limits(rate=10, burst=20))
        c = await s.connect_all()
        await s.start_match()
        before = json.dumps(s.coord.game.public_snapshot(0), sort_keys=True)
        attacker = c["p4"]
        for probe in (adv.identity_swap, adv.replay, adv.late_or_wrong_phase,
                      adv.malformed, adv.overbid):
            r = await probe(attacker)
            assert r.passed, r.show()
            await asyncio.sleep(2.1)          # let the token bucket refill between probes
        # Nothing the attacker did changed public state.
        after = json.dumps(s.coord.game.public_snapshot(0), sort_keys=True)
        assert before == after
        r = await adv.flood(attacker)
        assert r.passed, r.show()
        # Other players were unaffected by the flood.
        assert isinstance(await c["p1"].request("GetSnapshot"), dict)
        s.coord.game.check_invariants()
        await s.close()
    run(body())


def test_chat_flood_is_rate_limited_per_player(private_bus):
    async def body():
        s = await Stack(private_bus).start()
        c = await s.connect_all()
        await s.start_match()
        res = await asyncio.gather(*[c["p2"].send_public(f"spam {i}") for i in range(30)],
                                   return_exceptions=True)
        limited = [r for r in res if isinstance(r, ipc.RequestError) and r.code == "RATE_LIMITED"]
        assert len(limited) >= 20
        await c["p1"].send_public("still works")
        await wait_for(c["p3"], "ChatMessage", pred=lambda e: e["data"]["text"] == "still works")
        await s.close()
    run(body())


def test_terminal_escape_sequences_are_stripped(private_bus):
    async def body():
        s = await Stack(private_bus).start()
        c = await s.connect_all()
        await s.start_match()
        await c["p1"].send_public("\x1b]0;pwned\x07\x1b[2J[SERVER] you won!")
        ev = await wait_for(c["p2"], "ChatMessage")
        assert "\x1b" not in ev["data"]["text"] and "\x07" not in ev["data"]["text"]
        assert ev["data"]["from"] == "p1"           # sender set by coordinator, not text
        await s.close()
    run(body())


def test_audit_log_is_private(private_bus, tmp_path):
    async def body():
        path = tmp_path / "audit.jsonl"
        s = await Stack(private_bus).start(audit_path=str(path))
        await s.connect_all()
        await s.start_match()
        await s.close()
        assert (path.stat().st_mode & 0o777) == 0o600
        lines = [json.loads(x) for x in path.read_text().splitlines()]
        assert all("txn" in x and "ts" in x for x in lines)
        assert [x["txn"] for x in lines] == sorted(x["txn"] for x in lines)
    run(body())
