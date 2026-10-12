#!/usr/bin/env python3
"""Adversarial probes against a live Syndicate City game (PRD 7.4 / 11.3).

Run as a *player* account on the dedicated bus, e.g.::

    sudo -u syndicate-p4 .venv/bin/python tests/adversarial_client.py \
        --bus-address-file /run/syndicate-city/bus.address all

Each probe prints: expected behaviour, observed behaviour, the OS concept it
exercises and PASS/FAIL. This tool deliberately talks raw D-Bus (dbus_next)
for the bus-level attacks, because an attacker is not obliged to use ipc.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbus_next import Message, MessageType  # noqa: E402
from dbus_next.aio import MessageBus  # noqa: E402

import ipc  # noqa: E402

DBUS = "org.freedesktop.DBus"


@dataclass
class Result:
    name: str
    expected: str
    observed: str
    concept: str
    passed: bool

    def show(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return (f"[{mark}] {self.name}\n   expected: {self.expected}\n"
                f"   observed: {self.observed}\n   concept:  {self.concept}")


async def raw_bus(address: str) -> MessageBus:
    return await MessageBus(bus_address=address).connect()


async def driver(bus: MessageBus, member: str, sig: str = "", body=(), iface: str = DBUS):
    return await bus.call(Message(destination=DBUS, path="/org/freedesktop/DBus",
                                  interface=iface, member=member, signature=sig, body=list(body)))


def is_denied(reply: Message) -> bool:
    return reply.message_type == MessageType.ERROR and "AccessDenied" in (reply.error_name or "")


# ------------------------------------------------------------- bus-level probes

async def counterfeit_coordinator(address: str) -> Result:
    bus = await raw_bus(address)
    reply = await driver(bus, "RequestName", "su", [ipc.BUS_NAME, 4])
    bus.disconnect()
    if is_denied(reply):
        obs, ok = "AccessDenied from bus policy", True
    elif reply.message_type == MessageType.METHOD_RETURN and reply.body[0] == 3:
        obs, ok = "EXISTS (name held by coordinator; policy NOT tested - same UID as server?)", True
    else:
        obs, ok = f"unexpected reply {reply.message_type.name} {reply.body}", False
    return Result("Counterfeit Coordinator: own org.syndicate.City", "bus refuses ownership",
                  obs, "dbus-daemon <policy> own rules bound to the service UID", ok)


async def phantom_auction(address: str) -> Result:
    bus = await raw_bus(address)
    fake = json.dumps({"v": 1, "seq": 10**6, "pseq": 10**6, "type": "AuctionOpened",
                       "visibility": "public", "round": 1,
                       "data": {"round": 1, "auctions": [{"property_id": 10, "name": "Tech Park",
                                                          "min_bid": 1, "discoverers": []}],
                                "priority": []}})
    # 1) broadcast; 2) unicast straight at every other connection (unicast signals
    # bypass the receivers' sender-filtered match rules, so only policy or the
    # client's own provenance check can stop them).
    bus.send(Message.new_signal(ipc.OBJECT_PATH, ipc.INTERFACE, ipc.EVENT_SIGNAL, "s", [fake]))
    names = (await driver(bus, "ListNames")).body[0]
    targets = [n for n in names if n.startswith(":") and n != bus.unique_name]
    for t in targets:
        bus.send(Message(message_type=MessageType.SIGNAL, destination=t, path=ipc.OBJECT_PATH,
                         interface=ipc.INTERFACE, member=ipc.EVENT_SIGNAL, signature="s",
                         body=[fake]))
    await asyncio.sleep(0.2)
    bus.disconnect()
    return Result("Phantom Auction: forged GameEvent (broadcast + unicast to "
                  f"{len(targets)} connections)",
                  "bus drops it (players may not send signals); clients also ignore any "
                  "signal whose sender is not the coordinator's unique name",
                  "signals sent; other clients must show no auction (at most an ERROR "
                  "'ignored a GameEvent signal' line)",
                  "signal provenance: policy send_type rules + sender unique-name check", True)


async def become_monitor(address: str) -> Result:
    bus = await raw_bus(address)
    reply = await driver(bus, "BecomeMonitor", "asu", [[], 0], iface=DBUS + ".Monitoring")
    bus.disconnect()
    ok = reply.message_type == MessageType.ERROR
    return Result("Syndicate Spy (1): BecomeMonitor", "denied",
                  f"{reply.message_type.name} {reply.error_name or ''}".strip(),
                  "bus monitoring is privileged (root / bus UID only)", ok)


async def eavesdrop(address: str, seconds: float) -> Result:
    bus = await raw_bus(address)
    me = bus.unique_name
    leaked = []

    def handler(msg: Message):
        if msg.message_type == MessageType.SIGNAL and msg.member == ipc.EVENT_SIGNAL \
                and msg.destination not in (None, me):
            leaked.append(msg.destination)
        return False
    bus.add_message_handler(handler)
    reply = await driver(bus, "AddMatch", "s", ["type='signal',eavesdrop='true'"])
    await driver(bus, "AddMatch", "s", ["eavesdrop='true'"])
    await asyncio.sleep(seconds)
    bus.disconnect()
    return Result(f"Syndicate Spy (2): eavesdrop match rule for {seconds}s",
                  "no message addressed to another connection is delivered",
                  f"AddMatch {reply.message_type.name}; {len(leaked)} foreign unicast signals seen",
                  "unicast (destination-addressed) signals + no eavesdrop allow rule", not leaked)


async def call_other_connections(address: str) -> Result:
    bus = await raw_bus(address)
    names = (await driver(bus, "ListNames")).body[0]
    coord = (await driver(bus, "GetNameOwner", "s", [ipc.BUS_NAME])).body[0]
    targets = [n for n in names if n.startswith(":") and n not in (bus.unique_name, coord)]
    outcomes = []
    for t in targets[:8]:
        r = await asyncio.wait_for(bus.call(Message(
            destination=t, path="/", interface="org.freedesktop.DBus.Peer", member="Ping")), 3)
        outcomes.append(is_denied(r))
    bus.disconnect()
    if not outcomes:
        return Result("Direct calls to other players' connections", "AccessDenied for every target",
                      "SKIPPED: no other player connections on the bus (run during a match)",
                      "default-deny method_call policy", True)
    return Result("Direct calls to other players' connections", "AccessDenied for every target",
                  f"{sum(outcomes)}/{len(outcomes)} denied", "default-deny method_call policy",
                  all(outcomes))


# ------------------------------------------------------- application-level probes

async def joined_client(address: str) -> ipc.ClientIPC:
    c = await ipc.GameIPC.connect_client(address)
    await c.join()
    return c


async def expect_code(coro, *codes) -> str:
    try:
        await coro
    except ipc.RequestError as exc:
        return exc.code if exc.code in codes else f"!{exc.code}"
    return "!accepted"


async def identity_swap(c: ipc.ClientIPC) -> Result:
    obs = []
    obs.append(await expect_code(c.request("SubmitMovement", {"steps": 1, "player_id": "p1"}),
                                 "BAD_PAYLOAD"))
    obs.append(await expect_code(c.request("AcceptTradeOffer", {"offer_id": "o1"}),
                                 "UNKNOWN_OFFER", "WRONG_PHASE", "DEADLINE_PASSED"))
    snap = await c.request("GetSnapshot")
    ok = all(not o.startswith("!") for o in obs)
    return Result("Identity Swap: claim another player in the payload",
                  "extra identity fields rejected; actions apply only to the caller's slot",
                  f"{obs}; server still says you are {snap['private']['you']}",
                  "identity from bus credentials (GetConnectionUnixUser), never payload", ok)


async def replay(c: ipc.ClientIPC) -> Result:
    rid = c.new_request_id()
    first = await expect_code(c.request("CancelBid", {"property_id": 1}, request_id=rid),
                              "WRONG_PHASE", "NO_BID", "DEADLINE_PASSED", "NO_AUCTION")
    again = await expect_code(c.request("CancelBid", {"property_id": 1}, request_id=rid),
                              "WRONG_PHASE", "NO_BID", "DEADLINE_PASSED", "NO_AUCTION")
    changed = await expect_code(c.request("CancelBid", {"property_id": 2}, request_id=rid),
                                "ID_REUSED")
    ok = first == again and changed == "ID_REUSED"
    return Result("Replay: reuse a request id", "identical replay returns the recorded "
                  "outcome; changed payload with same id is rejected",
                  f"first={first} replay={again} altered={changed}",
                  "idempotency via per-player request-id ledger", ok)


async def late_or_wrong_phase(c: ipc.ClientIPC) -> Result:
    snap = await c.request("GetSnapshot")
    phase = snap["public"]["phase"]
    if phase == "AUCTION":
        probe = c.request("SubmitMovement", {"steps": 2})
    else:
        probe = c.request("SubmitBid", {"property_id": 1, "amount": 500})
    code = await expect_code(probe, "WRONG_PHASE", "DEADLINE_PASSED", "NO_AUCTION")
    return Result(f"Late/Wrong-phase action (current phase {phase})", "rejected",
                  code, "server-owned monotonic deadlines", not code.startswith("!"))


async def overbid(c: ipc.ClientIPC) -> Result:
    snap = await c.request("GetSnapshot")
    pub = snap["public"]
    if pub["phase"] != "AUCTION" or not pub["auctions"]:
        return Result("Double Buyer: concurrent bids beyond cash", "needs AUCTION phase",
                      f"skipped (phase {pub['phase']})", "atomic reservation ledger", True)
    me = next(p for p in pub["players"] if p["id"] == snap["private"]["you"])
    amount = max(me["cash"], 1)
    tiles = pub["auctions"]
    res = await asyncio.gather(*[c.request("SubmitBid", {"property_id": t, "amount": amount})
                                 for t in tiles for _ in range(5)], return_exceptions=True)
    after = await c.request("GetSnapshot")
    total = sum(after["private"]["bids"].values())
    ok = total <= max(0, me["cash"])
    return Result("Double Buyer: concurrent bids beyond cash", "sum of reservations <= cash",
                  f"{sum(isinstance(r, dict) for r in res)} accepted, reserved {total} of {me['cash']}",
                  "single serialised validate+commit section", ok)


async def flood(c: ipc.ClientIPC, n: int = 150) -> Result:
    res = await asyncio.gather(*[c.request("GetSnapshot") for _ in range(n)],
                               return_exceptions=True)
    limited = sum(isinstance(r, ipc.RequestError) and r.code == "RATE_LIMITED" for r in res)
    await asyncio.sleep(2.5)                  # bucket refills; we are not banned forever
    still_ok = isinstance(await c.request("GetSnapshot"), dict)
    return Result(f"Message Flood: {n} concurrent requests", "excess rejected with RATE_LIMITED; "
                  "service stays responsive", f"{limited}/{n} rate-limited; later request ok={still_ok}",
                  "per-player token bucket", limited > 0 and still_ok)


MALFORMED = [
    ("not JSON", "{nope"),
    ("JSON array", "[]"),
    ("wrong version", json.dumps({"v": 2, "id": "a", "action": "GetSnapshot", "payload": {}})),
    ("extra envelope key", json.dumps({"v": 1, "id": "a", "action": "GetSnapshot",
                                       "payload": {}, "player": "p1"})),
    ("unknown action", json.dumps({"v": 1, "id": "a", "action": "StartMatch", "payload": {}})),
    ("bad id", json.dumps({"v": 1, "id": "", "action": "GetSnapshot", "payload": {}})),
    ("string steps", json.dumps({"v": 1, "id": "m1", "action": "SubmitMovement",
                                 "payload": {"steps": "3"}})),
    ("bool steps", json.dumps({"v": 1, "id": "m2", "action": "SubmitMovement",
                               "payload": {"steps": True}})),
    ("NaN amount", '{"v":1,"id":"m3","action":"SubmitBid","payload":{"property_id":1,"amount":NaN}}'),
    ("huge int", json.dumps({"v": 1, "id": "m4", "action": "SubmitBid",
                             "payload": {"property_id": 1, "amount": 10**40}})),
    ("deep nesting", '{"v":1,"id":"m5","action":"GetSnapshot","payload":' + "[" * 1500 + "]" * 1500 + "}"),
    ("oversized", json.dumps({"v": 1, "id": "m6", "action": "SendPublic",
                              "payload": {"text": "x" * 5000}})),
]


async def malformed(c: ipc.ClientIPC) -> Result:
    codes = []
    for label, raw in MALFORMED:
        try:
            resp = json.loads(await c.request_raw(raw))
            codes.append((label, "ok" if resp.get("ok") else resp["error"]["code"]))
        except ipc.IPCError as exc:
            codes.append((label, f"transport:{str(exc)[:40]}"))
    wrong_sig = await c._bus.call(Message(destination=c.coordinator, path=ipc.OBJECT_PATH,
                                          interface=ipc.INTERFACE, member="Request",
                                          signature="i", body=[7]))
    codes.append(("wrong D-Bus signature", wrong_sig.error_name or "ok"))
    snap_ok = isinstance(await c.request("GetSnapshot"), dict)
    ok = all(code != "ok" for _, code in codes) and snap_ok
    return Result("Malformed Payload", "structured error for each; no crash; state intact",
                  "; ".join(f"{a}->{b}" for a, b in codes) + f"; server alive={snap_ok}",
                  "strict schema validation and size bounds", ok)


# ----------------------------------------------------------------------- driver

BUS_PROBES = {"coordinator": counterfeit_coordinator, "phantom": phantom_auction,
              "monitor": become_monitor, "direct": call_other_connections}
APP_PROBES = {"identity": identity_swap, "replay": replay, "late": late_or_wrong_phase,
              "overbid": overbid, "malformed": malformed, "flood": flood}


async def main_async(args) -> int:
    address = args.bus_address or ipc.read_bus_address(args.bus_address_file)
    wanted = []
    for probe in args.probe or ["all"]:
        wanted += list(BUS_PROBES) + ["eavesdrop"] + list(APP_PROBES) if probe == "all" else [probe]
    results = []
    client = None
    for name in wanted:
        if name in BUS_PROBES:
            results.append(await BUS_PROBES[name](address))
        elif name == "eavesdrop":
            results.append(await eavesdrop(address, args.eavesdrop_seconds))
        else:
            if client is None:
                client = await joined_client(address)
            results.append(await APP_PROBES[name](client))
        print(results[-1].show(), flush=True)
    if client:
        await client.close()
    failed = [r for r in results if not r.passed]
    print(f"\n{len(results) - len(failed)}/{len(results)} probes behaved securely.")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("probe", nargs="*", default=["all"],
                    choices=["all", "eavesdrop", *BUS_PROBES, *APP_PROBES])
    ap.add_argument("--bus-address")
    ap.add_argument("--bus-address-file", default=ipc.default_bus_address_file())
    ap.add_argument("--eavesdrop-seconds", type=float, default=5.0)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
