#!/usr/bin/env python3
"""Syndicate City coordinator: the single authority for all game state.

Run as the dedicated service user (``syndicate-server``) on the dedicated bus::

    python3 server.py --roster config/roster.json --bus-address-file /run/syndicate-city/bus.address

Operator console commands (stdin): ``start``, ``status``, ``players``, ``help``, ``quit``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pwd
import signal
import sys
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional

import ipc
from game import Event, Game, GameError, GameErrorWithEvents, Phase, Settings

log = logging.getLogger("syndicate.server")

MAX_REQUEST_IDS_PER_PLAYER = 5000

# --------------------------------------------------------------- request schemas
# field -> (validator, required). Payloads must match exactly: no unknown keys.


def _str(max_len: int):
    def check(v):
        return isinstance(v, str) and 0 < len(v) <= max_len
    check.desc = f"string of 1-{max_len} chars"
    return check


def _int(v):
    return type(v) is int and -2**31 <= v < 2**31
_int.desc = "32-bit integer"


def _str_list(max_items: int, max_len: int):
    def check(v):
        return (isinstance(v, list) and 0 < len(v) <= max_items
                and all(isinstance(x, str) and 0 < len(x) <= max_len for x in v))
    check.desc = f"list of 1-{max_items} strings"
    return check


SCHEMAS: dict[str, dict] = {
    "Join": {"display_name": (_str(16), False)},
    "GetSnapshot": {},
    "SendPublic": {"text": (_str(1200), True)},
    "SendPrivate": {"to": (_str(32), True), "text": (_str(1200), True)},
    "SendGroup": {"to": (_str_list(8, 32), True), "text": (_str(1200), True)},
    "CreateTradeOffer": {"buyer": (_str(32), True), "property_id": (_int, True),
                         "price": (_int, True)},
    "AcceptTradeOffer": {"offer_id": (_str(16), True)},
    "RejectTradeOffer": {"offer_id": (_str(16), True)},
    "CancelTradeOffer": {"offer_id": (_str(16), True)},
    "SubmitMovement": {"steps": (_int, True)},
    "SubmitBid": {"property_id": (_int, True), "amount": (_int, True)},
    "CancelBid": {"property_id": (_int, True)},
    "Leave": {},
}
CHAT_ACTIONS = frozenset({"SendPublic", "SendPrivate", "SendGroup"})
UNRECORDED_ACTIONS = frozenset({"Join", "GetSnapshot", "Leave"})   # read-only / session


def validate_payload(action: str, payload: dict) -> None:
    schema = SCHEMAS[action]
    unknown = set(payload) - set(schema)
    if unknown:
        raise ipc.RequestError("BAD_PAYLOAD", f"unknown field(s): {', '.join(sorted(unknown))}")
    for name, (check, required) in schema.items():
        if name not in payload:
            if required:
                raise ipc.RequestError("BAD_PAYLOAD", f"missing field {name!r}")
            continue
        if not check(payload[name]):
            raise ipc.RequestError("BAD_PAYLOAD", f"{name!r} must be a {check.desc}")


# ------------------------------------------------------------------ rate limits

class TokenBucket:
    def __init__(self, rate: float, burst: float, clock: Callable[[], float]):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens = burst
        self.stamp = clock()

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


@dataclass
class Limits:
    rate: float = 10.0
    burst: float = 20.0
    chat_rate: float = 5.0
    chat_burst: float = 5.0


# ---------------------------------------------------------------------- roster

def load_roster(path: str) -> list[tuple[str, int, str]]:
    """Return ``[(player_id, uid, display_name)]`` from a roster JSON file."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for entry in data["players"]:
        if "uid" in entry:
            uid = int(entry["uid"])
        else:
            uid = pwd.getpwnam(entry["user"]).pw_uid
        out.append((str(entry["id"]), uid, str(entry["name"]).lower()))
    return out


# ------------------------------------------------------------------ coordinator

class Coordinator:
    def __init__(self, game: Game, server: ipc.ServerIPC, audit_path: Optional[str] = None,
                 limits: Optional[Limits] = None, clock: Callable[[], float] = time.monotonic,
                 echo: Callable[[str], None] = lambda line: print(line, flush=True)):
        self.game = game
        self.ipc = server
        self.clock = clock
        self.echo = echo
        self.limits = limits or Limits()
        self.lock = asyncio.Lock()
        self.dedup: dict[str, OrderedDict] = {pid: OrderedDict() for pid in game.players}
        self.buckets = {pid: TokenBucket(self.limits.rate, self.limits.burst, clock)
                        for pid in game.players}
        self.chat_buckets = {pid: TokenBucket(self.limits.chat_rate, self.limits.chat_burst, clock)
                             for pid in game.players}
        self.txn = 0
        self.started = asyncio.Event()
        self.ended = asyncio.Event()
        self.latencies: list[float] = []
        self._audit_fd = None
        if audit_path:
            self._audit_fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(self._audit_fd, 0o600)
        server.on_request(self.handle_request)
        server.on_disconnect(self.handle_disconnect)

    # -- audit -----------------------------------------------------------------

    def audit(self, kind: str, **fields) -> int:
        self.txn += 1
        if self._audit_fd is not None:
            record = {"txn": self.txn, "ts": time.time(), "mono": round(self.clock(), 4),
                      "kind": kind, **fields}
            os.write(self._audit_fd, (json.dumps(record, default=str) + "\n").encode())
        return self.txn

    # -- delivery (always after commit) -----------------------------------------

    def emit(self, events: list[Event]) -> None:
        rnd = self.game.round_number
        for ev in events:
            if ev.visibility == "public":
                self.ipc.broadcast(ev.type, ev.data, rnd)
                self._operator_line(ev)
            else:
                self.ipc.multicast(ev.recipients, ev.type, ev.data, rnd)

    def _operator_line(self, ev: Event) -> None:
        d = ev.data
        if ev.type == "PhaseChanged":
            self.echo(f"[round {d['round']}] phase {d['phase']} ({d['remaining']}s)")
        elif ev.type in ("PlayerJoined", "PlayerRejoined", "PlayerDisconnected"):
            self.echo(f"{ev.type}: {d['name']} ({d['player']})")
        elif ev.type == "AuctionClosed":
            self.echo(f"auction {d['name']}: winner={d['winner']} paid={d['paid']}")
        elif ev.type == "MatchEnded":
            self.echo("MATCH ENDED. winners: " + ", ".join(d["winners"]))
            for s in d["scores"]:
                self.echo(f"  {s['name']:<12} net={s['net_worth']:>5} cash={s['cash']:>5} "
                          f"props={s['properties']} districts={s['districts']}")

    # -- requests ----------------------------------------------------------------

    async def handle_request(self, caller: ipc.Caller, req: ipc.Request) -> dict:
        t0 = time.perf_counter()
        try:
            return await self._handle(caller, req)
        finally:
            self.latencies.append(time.perf_counter() - t0)
            if len(self.latencies) > 10000:
                del self.latencies[:5000]

    async def _handle(self, caller: ipc.Caller, req: ipc.Request) -> dict:
        pid = caller.player_id
        if not self.buckets[pid].take():
            raise ipc.RequestError("RATE_LIMITED", "too many requests; slow down")
        if req.action in CHAT_ACTIONS and not self.chat_buckets[pid].take():
            raise ipc.RequestError("RATE_LIMITED", "too many chat messages; slow down")
        validate_payload(req.action, req.payload)

        record = req.action not in UNRECORDED_ACTIONS
        seen = self.dedup[pid]

        async with self.lock:          # one serialised validate-and-commit path
            if record:
                # Checked under the lock so two concurrent copies cannot both run.
                if req.id in seen:
                    fingerprint, outcome = seen[req.id]
                    if fingerprint != req.fingerprint:
                        raise ipc.RequestError("ID_REUSED",
                                               "request id reused with a different payload")
                    return self._replay(outcome)
                if len(seen) >= MAX_REQUEST_IDS_PER_PLAYER:
                    raise ipc.RequestError("ID_LIMIT", "request id budget for this match exhausted")
            now = self.clock()
            try:
                result, events = self._dispatch(pid, req.action, req.payload, now)
            except GameErrorWithEvents as exc:
                outcome = ("err", exc.code, exc.message)
                self.audit("request", player=pid, id=req.id, action=req.action,
                           payload=req.payload, ok=False, error=exc.code)
                self.emit(exc.events)
            except GameError as exc:
                outcome = ("err", exc.code, exc.message)
                self.audit("request", player=pid, id=req.id, action=req.action,
                           payload=req.payload, ok=False, error=exc.code)
            else:
                outcome = ("ok", result)
                self.audit("request", player=pid, id=req.id, action=req.action,
                           payload=req.payload, ok=True, events=[e.type for e in events])
                self.emit(events)
            if record:
                seen[req.id] = (req.fingerprint, outcome)
        return self._replay(outcome)

    @staticmethod
    def _replay(outcome) -> dict:
        if outcome[0] == "ok":
            return outcome[1]
        raise ipc.RequestError(outcome[1], outcome[2])

    def _dispatch(self, pid: str, action: str, p: dict, now: float):
        g = self.game
        if action == "Join":
            events = g.mark_joined(pid)
            return g.snapshot_for(pid, now), events
        if action == "GetSnapshot":
            g._require_player(pid)
            return g.snapshot_for(pid, now), []
        if action == "Leave":
            return {"left": True}, g.mark_disconnected(pid)
        if action == "SendPublic":
            return g.send_public(pid, p["text"], now)
        if action == "SendPrivate":
            return g.send_private(pid, p["to"], p["text"], now)
        if action == "SendGroup":
            return g.send_group(pid, p["to"], p["text"], now)
        if action == "CreateTradeOffer":
            return g.create_offer(pid, p["buyer"], p["property_id"], p["price"], now)
        if action == "AcceptTradeOffer":
            return g.accept_offer(pid, p["offer_id"], now)
        if action == "RejectTradeOffer":
            return g.reject_offer(pid, p["offer_id"], now)
        if action == "CancelTradeOffer":
            return g.cancel_offer(pid, p["offer_id"], now)
        if action == "SubmitMovement":
            return g.submit_movement(pid, p["steps"], now)
        if action == "SubmitBid":
            return g.submit_bid(pid, p["property_id"], p["amount"], now)
        if action == "CancelBid":
            return g.cancel_bid(pid, p["property_id"], now)
        raise ipc.RequestError("UNKNOWN_ACTION", action)

    async def handle_disconnect(self, pid: str) -> None:
        async with self.lock:
            events = self.game.mark_disconnected(pid)
            self.audit("disconnect", player=pid)
            self.emit(events)

    # -- operator ----------------------------------------------------------------

    async def start_match(self) -> bool:
        async with self.lock:
            try:
                events = self.game.start_match(self.clock())
            except GameError as exc:
                self.echo(f"cannot start: {exc.message}")
                return False
            self.audit("start", players=self.game.slot_order)
            self.emit(events)
        self.started.set()
        return True

    async def run_clock(self) -> None:
        """Close phases at their server-side deadlines."""
        await self.started.wait()
        while self.game.phase != Phase.ENDED:
            delay = self.game.phase_deadline_monotonic - self.clock()
            if delay > 0:
                await asyncio.sleep(min(delay, 1.0))
                continue
            async with self.lock:
                before = (self.game.round_number, self.game.phase)
                events = self.game.advance(self.clock())
                self.audit("advance", before=[before[0], before[1].value],
                           after=[self.game.round_number, self.game.phase.value],
                           events=[e.type for e in events])
                self.emit(events)
        self.ended.set()

    async def console_command(self, line: str) -> bool:
        """Handle one operator command; return False to quit."""
        cmd = line.strip().lower()
        if cmd == "start":
            await self.start_match()
        elif cmd == "status":
            snap = self.game.public_snapshot(self.clock())
            self.echo(f"round {snap['round']}/{snap['rounds']} phase {snap['phase']} "
                      f"remaining {snap['remaining']}s auctions {snap['auctions']}")
            if self.latencies:
                lat = sorted(self.latencies)
                self.echo(f"request latency ms: p50={lat[len(lat)//2]*1000:.2f} "
                          f"p95={lat[int(len(lat)*0.95)]*1000:.2f} max={lat[-1]*1000:.2f} "
                          f"(n={len(lat)})")
        elif cmd == "players":
            for pid in self.game.slot_order:
                p = self.game.players[pid]
                self.echo(f"{pid} {p.display_name:<12} uid={p.registered_uid:<6} "
                          f"{'connected' if p.connected else ('joined' if p.joined else 'absent'):<10}"
                          f" cash={p.cash} pos={p.position} props={sorted(p.owned_property_ids)}")
        elif cmd in ("quit", "exit", "stop"):
            return False
        elif cmd in ("help", "?", ""):
            self.echo("commands: start | status | players | quit")
        else:
            self.echo(f"unknown command {cmd!r}; try help")
        return True

    async def run_console(self, stream) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), stream)
        while True:
            line = await reader.readline()
            if not line:          # EOF: keep serving; operator can still Ctrl-C
                await asyncio.Event().wait()
            if not await self.console_command(line.decode(errors="replace")):
                return

    def close(self) -> None:
        if self._audit_fd is not None:
            os.close(self._audit_fd)
            self._audit_fd = None


ALLOWED_ACTIONS = frozenset(SCHEMAS)


async def main_async(args) -> int:
    roster = load_roster(args.roster)
    settings = Settings(rounds=args.rounds, negotiation_seconds=args.negotiation_seconds,
                        movement_seconds=args.movement_seconds,
                        auction_seconds=args.auction_seconds)
    if args.require_players and len(roster) != args.require_players:
        print(f"roster has {len(roster)} players, expected {args.require_players}", file=sys.stderr)
        return 2
    game = Game(roster, settings, match_id=uuid.uuid4().hex[:8])
    address = args.bus_address or ipc.read_bus_address(args.bus_address_file)
    try:
        server = await ipc.GameIPC.start_server(
            address, {uid: pid for pid, uid, _ in roster}, ALLOWED_ACTIONS)
    except ipc.IPCError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 1
    coord = Coordinator(game, server, args.audit_log)
    coord.audit("boot", match=game.match_id, roster=roster, unique_name=server.unique_name,
                settings=vars(settings))
    print(f"Syndicate City coordinator {server.unique_name} owns {ipc.BUS_NAME}; "
          f"match {game.match_id}; {len(roster)} players; {settings.rounds} rounds.")
    print("Waiting for players. Type 'start' when everyone has joined, 'help' for commands.",
          flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.ensure_future(coord.run_clock()),
             asyncio.ensure_future(coord.run_console(sys.stdin)),
             asyncio.ensure_future(stop.wait())]
    ended = asyncio.ensure_future(coord.ended.wait())
    done, _ = await asyncio.wait(tasks + [ended], return_when=asyncio.FIRST_COMPLETED)
    if ended in done and not stop.is_set() and args.linger > 0:
        print(f"Match over; serving snapshots for {args.linger}s (Ctrl-C to exit).")
        await asyncio.wait([asyncio.ensure_future(stop.wait())], timeout=args.linger)
    for t in tasks + [ended]:
        t.cancel()
    coord.audit("shutdown")
    coord.close()
    await server.close()
    return 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Syndicate City coordinator")
    ap.add_argument("--roster", required=True, help="roster JSON (UID -> player slot)")
    ap.add_argument("--bus-address", help="dedicated bus address (unix:path=...)")
    ap.add_argument("--bus-address-file", default=ipc.default_bus_address_file())
    ap.add_argument("--audit-log", default=None, help="restricted JSONL audit log path")
    ap.add_argument("--rounds", type=int, default=7)
    ap.add_argument("--negotiation-seconds", type=float, default=45.0)
    ap.add_argument("--movement-seconds", type=float, default=15.0)
    ap.add_argument("--auction-seconds", type=float, default=25.0)
    ap.add_argument("--require-players", type=int, default=0,
                    help="refuse to boot unless the roster has exactly this many players")
    ap.add_argument("--linger", type=float, default=30.0,
                    help="seconds to keep serving after the match ends")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    os.umask(0o077)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
