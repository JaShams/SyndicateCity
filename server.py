#!/usr/bin/env python3
"""Syndicate City coordinator: the single authority for all game state.

Run as the dedicated service user (``syndicate-server``) on the dedicated bus::

    python3 server.py --roster config/roster.json --bus-address-file /run/syndicate-city/bus.address

Operator console commands (stdin): ``start``, ``status``, ``players``, ``help``, ``quit``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
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

import cmdlog
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
    """Authority for one match.

    Every state change is a *command* submitted to a command log (``cmdlog.py``)
    and applied by :meth:`apply`, which is deterministic: the same commands in
    the same order always produce the same game, outcomes and events. The log
    decides durability and replication; this class does not care which one it is.
    Only read-only work (rate limits, payload validation, snapshots) happens
    outside the log.
    """

    def __init__(self, game: Game, server: ipc.ServerIPC, audit_path: Optional[str] = None,
                 limits: Optional[Limits] = None, clock: Callable[[], float] = time.monotonic,
                 echo: Callable[[str], None] = lambda line: print(line, flush=True),
                 log: Optional[cmdlog.MemoryLog] = None, digest: str = ""):
        self.game = game
        self.digest = digest
        self.ipc = server
        self.clock = clock                       # local clock: rate limits only
        self.gclock = cmdlog.GameClock(clock)    # game time, carried in the log
        self.echo = echo
        self.limits = limits or Limits()
        self.log = log or cmdlog.MemoryLog()
        self.log.bind(self.apply)
        self.lock = asyncio.Lock()
        self.dedup: dict[str, OrderedDict] = {pid: OrderedDict() for pid in game.players}
        self.buckets = {pid: TokenBucket(self.limits.rate, self.limits.burst, clock)
                        for pid in game.players}
        self.chat_buckets = {pid: TokenBucket(self.limits.chat_rate, self.limits.chat_burst, clock)
                             for pid in game.players}
        self.txn = 0
        self.applied = 0
        self.replaying = False
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
        if self._audit_fd is not None and not self.replaying:
            record = {"txn": self.txn, "ts": time.time(), "mono": round(self.clock(), 4),
                      "kind": kind, **fields}
            os.write(self._audit_fd, (json.dumps(record, default=str) + "\n").encode())
        return self.txn

    # -- delivery (always after commit) -----------------------------------------

    def emit(self, events: list[Event], deliver: bool = True) -> None:
        rnd = self.game.round_number
        for ev in events:
            if ev.visibility == "public":
                if deliver:
                    self.ipc.broadcast(ev.type, ev.data, rnd)
                if not self.replaying:
                    self._operator_line(ev)
            elif deliver:
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

    # -- the state machine --------------------------------------------------------

    def apply(self, cmd: dict, deliver: bool = True) -> tuple:
        """Apply one committed command; return its outcome. Deterministic.

        ``deliver`` is False on replicas that are not serving players and while
        replaying history, so events change state but are not sent to anyone.
        """
        self.applied += 1
        self.gclock.observe(float(cmd.get("now", 0.0)))
        now = self.gclock.last                # never earlier than any previous command
        kind = cmd.get("k")
        g = self.game
        events: list[Event] = []
        try:
            if kind == "req":
                return self._apply_request(cmd, now, deliver)
            if kind == "start":
                events = g.start_match(now)
                self.audit("start", players=g.slot_order)
                outcome = ("ok", {})
            elif kind == "advance":
                before = (g.round_number, g.phase)
                events = g.advance(now)
                if events:
                    self.audit("advance", before=[before[0], before[1].value],
                               after=[g.round_number, g.phase.value],
                               events=[e.type for e in events])
                outcome = ("ok", {})
            elif kind == "disconnect":
                events = g.mark_disconnected(cmd["pid"])
                self.audit("disconnect", player=cmd["pid"])
                outcome = ("ok", {})
            elif kind == "leader":
                self.audit("leader", node=cmd.get("node"), digest=cmd.get("digest"))
                if cmd.get("digest") and self.digest and cmd["digest"] != self.digest \
                        and not self.replaying:
                    self.echo(f"WARNING: leader {cmd.get('node')} runs a different roster or "
                              "settings; replicas will diverge. Fix the configuration.")
                outcome = ("ok", {})
            else:
                outcome = ("err", "BAD_COMMAND", f"unknown command {kind!r}")
        except GameError as exc:
            outcome = ("err", exc.code, exc.message)
            if kind == "start" and not self.replaying:
                self.echo(f"cannot start: {exc.message}")
        self.emit(events, deliver)
        self._track_lifecycle()
        return outcome

    def _apply_request(self, cmd: dict, now: float, deliver: bool) -> tuple:
        pid, rid, action, payload = cmd["pid"], cmd["id"], cmd["action"], cmd["payload"]
        record = action not in UNRECORDED_ACTIONS
        seen = self.dedup[pid]
        if record:
            if rid in seen:
                fingerprint, outcome = seen[rid]
                if fingerprint != cmd["fp"]:
                    return ("err", "ID_REUSED", "request id reused with a different payload")
                return outcome
            if len(seen) >= MAX_REQUEST_IDS_PER_PLAYER:
                return ("err", "ID_LIMIT", "request id budget for this match exhausted")
        try:
            result, events = self._dispatch(pid, action, payload, now)
        except GameErrorWithEvents as exc:
            outcome = ("err", exc.code, exc.message)
            self.audit("request", player=pid, id=rid, action=action,
                       payload=payload, ok=False, error=exc.code)
            self.emit(exc.events, deliver)
        except GameError as exc:
            outcome = ("err", exc.code, exc.message)
            self.audit("request", player=pid, id=rid, action=action,
                       payload=payload, ok=False, error=exc.code)
        else:
            outcome = ("ok", result)
            self.audit("request", player=pid, id=rid, action=action,
                       payload=payload, ok=True, events=[e.type for e in events])
            self.emit(events, deliver)
        if record:
            seen[rid] = (cmd["fp"], outcome)
        self._track_lifecycle()
        return outcome

    def _track_lifecycle(self) -> None:
        if self.game.phase != Phase.LOBBY:
            self.started.set()
        if self.game.phase == Phase.ENDED:
            self.ended.set()

    def replay(self, history: list[dict]) -> None:
        """Rebuild state from a log's history (synchronously: no request can interleave)."""
        self.replaying = True
        try:
            for cmd in history:
                self.apply(cmd, deliver=False)
        finally:
            self.replaying = False
        self.gclock.resume()
        if history:
            self.audit("recovered", commands=len(history), round=self.game.round_number,
                       phase=self.game.phase.value, game_time=round(self.gclock.last, 3))

    async def submit(self, cmd: dict) -> tuple:
        """Stamp a command with the current game time and commit it through the log."""
        if self.log.kind == "raft":
            # The log orders and applies; never hold the lock across a network round trip.
            cmd["now"] = round(self.gclock.now(), 6)
            return await self.log.submit(cmd)
        async with self.lock:          # one serialised stamp-persist-apply path
            cmd["now"] = round(self.gclock.now(), 6)
            return await self.log.submit(cmd)

    async def forget_stale_sessions(self) -> None:
        """After a restart or fail-over nobody is connected to *this* process yet."""
        for pid, p in self.game.players.items():
            if p.connected and not self.ipc.is_connected(pid):
                await self.submit({"k": "disconnect", "pid": pid})

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
        if req.action == "GetSnapshot":          # read-only: served locally, never logged
            try:
                self.game._require_player(pid)
            except GameError as exc:
                raise ipc.RequestError(exc.code, exc.message) from None
            return self.game.snapshot_for(pid, self.gclock.now())
        try:
            outcome = await self.submit({"k": "req", "pid": pid, "id": req.id,
                                         "fp": req.fingerprint, "action": req.action,
                                         "payload": req.payload})
        except cmdlog.NotLeader as exc:
            raise ipc.RequestError("NOT_LEADER", str(exc) or "this coordinator is not the leader")
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
        raise GameError("UNKNOWN_ACTION", action)

    async def handle_disconnect(self, pid: str) -> None:
        if not self.log.is_leader:
            return
        with contextlib.suppress(cmdlog.NotLeader):
            await self.submit({"k": "disconnect", "pid": pid})

    # -- operator ----------------------------------------------------------------

    async def start_match(self) -> bool:
        if not self.log.is_leader:
            self.echo(f"cannot start: this replica is not the leader ({self.log.status()})")
            return False
        try:
            outcome = await self.submit({"k": "start"})
        except cmdlog.NotLeader as exc:
            self.echo(f"cannot start: {exc}")
            return False
        return outcome[0] == "ok"

    async def run_clock(self) -> None:
        """Close phases at their deadlines (only the replica that orders commands does)."""
        while True:
            await self.started.wait()
            if self.game.phase == Phase.ENDED:
                break
            if not self.log.is_leader:
                await asyncio.sleep(0.2)
                continue
            delay = self.game.phase_deadline_monotonic - self.gclock.now()
            if delay > 0:
                await asyncio.sleep(min(delay, 0.5))
                continue
            try:
                await self.submit({"k": "advance"})
            except cmdlog.NotLeader:
                await asyncio.sleep(0.2)
        self.ended.set()

    async def console_command(self, line: str) -> bool:
        """Handle one operator command; return False to quit."""
        cmd = line.strip().lower()
        if cmd == "start":
            await self.start_match()
        elif cmd == "status":
            snap = self.game.public_snapshot(self.gclock.now())
            self.echo(f"round {snap['round']}/{snap['rounds']} phase {snap['phase']} "
                      f"remaining {snap['remaining']}s auctions {snap['auctions']}")
            self.echo(f"{self.log.status()}; {self.applied} commands applied")
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


def config_digest(roster, settings: Settings) -> str:
    """Fingerprint of everything replicas must agree on to stay identical."""
    blob = json.dumps({"roster": [list(r) for r in roster], "settings": vars(settings)},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _split_peers(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


async def main_async(args) -> int:
    roster = load_roster(args.roster)
    settings = Settings(rounds=args.rounds, negotiation_seconds=args.negotiation_seconds,
                        movement_seconds=args.movement_seconds,
                        auction_seconds=args.auction_seconds)
    if args.require_players and len(roster) != args.require_players:
        print(f"roster has {len(roster)} players, expected {args.require_players}", file=sys.stderr)
        return 2
    digest = config_digest(roster, settings)
    clustered = bool(args.raft_self)
    if clustered and not args.listen:
        print("fatal: a replicated coordinator serves players over --listen wss://...",
              file=sys.stderr)
        return 2

    # -- command log: memory, local file, or Raft ----------------------------------
    header = {"k": "boot", "v": 1, "match": uuid.uuid4().hex[:8],
              "roster": [list(r) for r in roster], "settings": vars(settings)}
    history: list[dict] = []
    if clustered:
        try:
            password = ipc.read_private_file(args.cluster_secret_file).decode()
            log_ = cmdlog.RaftLog(args.raft_self, _split_peers(args.raft_peers), password,
                                  state_dir=args.state_dir, node=args.node_id or args.raft_self,
                                  digest=digest)
        except (OSError, ValueError, ipc.IPCError, TypeError) as exc:
            print(f"fatal: cannot start Raft: {exc}", file=sys.stderr)
            return 1
        header["match"] = digest[:8]           # every replica must use the same id
    elif args.state_dir:
        os.makedirs(args.state_dir, mode=0o700, exist_ok=True)
        log_ = cmdlog.FileLog(os.path.join(args.state_dir, "commands.jsonl"))
        try:
            header, history = log_.open(header)
        except (OSError, ValueError) as exc:
            print(f"fatal: {exc}", file=sys.stderr)
            return 1
    else:
        log_ = cmdlog.MemoryLog()
    game = Game(roster, settings, match_id=header["match"])

    # -- transport ------------------------------------------------------------------
    try:
        if args.listen:
            secret = ipc.read_private_file(args.token_secret_file)
            server = await ipc.GameIPC.start_server(
                args.listen, {uid: pid for pid, uid, _ in roster}, ALLOWED_ACTIONS,
                tls_cert=args.tls_cert, tls_key=args.tls_key, token_secret=secret,
                server_id=args.node_id or None)
        else:
            address = args.bus_address or ipc.read_bus_address(args.bus_address_file)
            server = await ipc.GameIPC.start_server(
                address, {uid: pid for pid, uid, _ in roster}, ALLOWED_ACTIONS)
    except (ipc.IPCError, OSError, TypeError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 1

    coord = Coordinator(game, server, args.audit_log, log=log_, digest=digest)
    coord.audit("boot", match=game.match_id, roster=roster, unique_name=server.unique_name,
                settings=vars(settings), log=log_.kind)
    coord.replay(history)                     # synchronous: no request can interleave
    if history:
        print(f"Recovered {len(history)} commands from {log_.path}: round "
              f"{game.round_number} phase {game.phase.value}.")
        await coord.forget_stale_sessions()

    if clustered:
        coord.gclock.freeze()
        server.accepting = False
        server.refusal = "this replica is not the leader"

        async def on_leader():
            coord.gclock.resume()
            server.accepting = True
            coord.echo(f"*** {log_.node} is the LEADER: serving players on {args.listen}")
            await coord.forget_stale_sessions()

        async def on_follower():
            coord.gclock.freeze()
            server.accepting = False
            coord.echo(f"*** {log_.node} is now a follower: players moved to the new leader")
            await server.drop_all("leadership moved to another replica")

        log_.on_leader, log_.on_follower = on_leader, on_follower
        log_._now = coord.gclock.now
        log_.start()

    where = args.listen or ipc.BUS_NAME
    print(f"Syndicate City coordinator {server.unique_name} ({server.transport}) on {where}; "
          f"match {game.match_id}; {len(roster)} players; {settings.rounds} rounds; "
          f"{log_.status()}.")
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
    await server.close()
    await log_.close()
    coord.close()
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
    net = ap.add_argument_group("network play (instead of the D-Bus bus)")
    net.add_argument("--listen", metavar="wss://HOST:PORT",
                     help="serve players over WebSocket+TLS on this address")
    net.add_argument("--tls-cert", help="TLS certificate (PEM), e.g. from scripts/net_keys.py")
    net.add_argument("--tls-key", help="TLS private key (PEM, mode 0600)")
    net.add_argument("--token-secret-file", help="HMAC secret that signs player tokens (0600)")
    dur = ap.add_argument_group("durability and replication")
    dur.add_argument("--state-dir", help="keep the command log here: survives a crash. With "
                                         "--raft-self, holds this replica's Raft journal")
    dur.add_argument("--raft-self", metavar="HOST:PORT",
                     help="run as one replica of a Raft cluster; address peers reach us on")
    dur.add_argument("--raft-peers", metavar="HOST:PORT,...", default="",
                     help="the other replicas' --raft-self addresses")
    dur.add_argument("--cluster-secret-file", help="shared password for Raft traffic (0600)")
    dur.add_argument("--node-id", help="name for this replica in logs (default: --raft-self)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    os.umask(0o077)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
