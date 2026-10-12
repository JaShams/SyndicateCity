#!/usr/bin/env python3
"""Non-interactive scripted player used by scripts/integration_multiuser.sh.

Joins as the current Linux user's roster slot, plays simple moves and bids,
and writes one JSON line per received event to stdout. Exits 0 on MatchEnded.
"""

import argparse
import asyncio
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ipc  # noqa: E402


def emit(kind, **data):
    print(json.dumps({"kind": kind, **data}), flush=True)


async def play(args) -> int:
    rng = random.Random(args.seed if args.seed is not None else os.getuid())
    address = args.bus_address or ipc.read_bus_address(args.bus_address_file)
    conn = await ipc.GameIPC.connect_client(address)
    snap = await conn.join()
    me = snap["private"]["you"]
    others = [p["name"] for p in snap["public"]["players"] if p["id"] != me]
    emit("joined", uid=os.getuid(), player=me, unique_name=conn.unique_name,
         coordinator=conn.coordinator, phase=snap["public"]["phase"],
         bids=snap["private"]["bids"])

    async def act(ev):
        t, d = ev["type"], ev["data"]
        try:
            if t == "PhaseChanged" and d["phase"] == "NEGOTIATION" and args.chat:
                target = others[d["round"] % len(others)]
                await conn.send_private(target, f"secret-{me}-to-{target}-r{d['round']}")
                await conn.send_group(others[:2], f"group-{me}-r{d['round']}")
            elif t == "PhaseChanged" and d["phase"] == "MOVEMENT":
                await conn.request("SubmitMovement", {"steps": rng.randint(1, 3)})
            elif t == "AuctionOpened":
                s = await conn.request("GetSnapshot")
                cash = s["private"]["available"]
                for a in d["auctions"]:
                    if me not in a["discoverers"]:
                        continue
                    amount = a["min_bid"] + (rng.randint(0, 40) if a["contested"] else 0)
                    if amount <= cash and rng.random() < 0.7:
                        await conn.request("SubmitBid", {"property_id": a["property_id"],
                                                         "amount": amount})
                        cash -= amount
        except ipc.RequestError as exc:
            emit("rejected", code=exc.code, message=exc.message)

    async for ev in conn.events():
        emit("event", type=ev["type"], visibility=ev["visibility"], data=ev["data"])
        if ev["type"] == "MatchEnded":
            await conn.close()
            return 0
        if ev["type"].startswith("_") and ev["type"] != "_ForgedSignalIgnored":
            return 3
        asyncio.ensure_future(act(ev))
    return 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bus-address")
    ap.add_argument("--bus-address-file", default=ipc.default_bus_address_file())
    ap.add_argument("--seed", type=int)
    ap.add_argument("--no-chat", dest="chat", action="store_false")
    return asyncio.run(play(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
