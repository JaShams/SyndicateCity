#!/usr/bin/env python3
"""Syndicate City interactive text client. Run one per Linux player account.

    python3 client.py --bus-address-file /run/syndicate-city/bus.address

Type /help for commands. Lines that do not start with '/' are sent as public chat.

    python3 client.py --script demo/p1.txt

plays a scripted game instead of reading the keyboard; see SCRIPT_HELP below.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Optional

import board
import ipc
from board import TILES
from game import sanitize_text

HELP = """\
/help                               this text
/board                              board map: owners, pawns, player legend
/board list                         board as a table, with minimum bids
/status                             phase, deadline, your cash/bids/offers
/players                            all players, cash, properties
/say <message>                      public chat            (NEGOTIATION/AUCTION)
/tell <player> <message>            private chat
/group <p1,p2,...> <message>        chat to selected players
/offer <buyer> <property> <price>   offer one of your properties (NEGOTIATION)
/offers                             your pending offers
/accept <offer_id>   /reject <offer_id>   /canceloffer <offer_id>
/move <1|2|3>                       secret movement choice (MOVEMENT, editable)
/bid <property> <amount>            sealed bid (AUCTION, replaces previous bid)
/cancelbid <property>
/quit
Properties may be given by tile number or name (e.g. 10 or techpark).
Short forms: /m = /move, /b = /bid, /a = /accept, /r = /reject, /t = /tell, /s = /status"""

ALIASES = {"/m": "/move", "/b": "/bid", "/a": "/accept", "/r": "/reject", "/t": "/tell",
           "/s": "/status", "/p": "/players", "/h": "/help", "/q": "/quit"}

SCRIPT_HELP = """\
A script is a text file of client commands, run top to bottom. Blank lines and
lines starting with '#' are ignored. Lines starting with '@' control timing:

@round <n> [negotiation|movement|auction]   wait for that phase (default negotiation);
                                            if it already passed or never happens
                                            (no auction that round), skip to the next @round
@wait <seconds>                             pause, e.g. to pace a conversation
@accept [player]   @reject [player]         wait for the next offer made to you (from
                                            that player) and answer it; gives up when
                                            the phase ends
@end                                        stop here (the client still waits for the
                                            final scoreboard, then exits)

Everything else is typed as if at the prompt: /say, /tell, /offer, /move, /bid ..."""

PHASE_ORDER = {"LOBBY": 0, "NEGOTIATION": 1, "MOVEMENT": 2, "AUCTION": 3, "SETTLEMENT": 4}
SCRIPT_PHASES = ("NEGOTIATION", "MOVEMENT", "AUCTION")

TAGS = {"SERVER": "\033[1;36m", "PUBLIC": "\033[0;37m", "GROUP": "\033[0;35m",
        "PRIVATE": "\033[1;35m", "TRADE": "\033[1;33m", "AUCTION": "\033[1;32m",
        "ERROR": "\033[1;31m", "SCRIPT": "\033[1;34m"}
RESET = "\033[0m"
DISTRICT_COLORS = {"Blue": "\033[1;34m", "Green": "\033[1;32m", "Purple": "\033[1;35m",
                   "Red": "\033[1;31m"}

# /board map: the tiles sit on the rim of a 4x4 grid, clockwise from START at top-left.
RING = {(0, 0): 0, (0, 1): 1, (0, 2): 2, (0, 3): 3, (1, 3): 4, (2, 3): 5,
        (3, 3): 6, (3, 2): 7, (3, 1): 8, (3, 0): 9, (2, 0): 10, (1, 0): 11}
assert sorted(RING.values()) == list(range(board.BOARD_SIZE))
CELL_W = 14       # inner width of a tile box
CELL_LINES = 5    # text lines per tile box
CENTRE_W = 2 * CELL_W + 1


def clean(value) -> str:
    """Escape anything that came off the wire before it reaches the terminal."""
    return sanitize_text(str(value))


class ClientUI:
    def __init__(self, conn: ipc.ClientIPC, color: bool, interactive: bool):
        self.conn = conn
        self.color = color
        self.interactive = interactive
        self.me: Optional[str] = None
        self.public: dict = {}
        self.offers: dict[str, dict] = {}
        self.bids: dict[int, int] = {}
        self.movement: Optional[int] = None
        self.deadline = 0.0
        self.running = True
        self.ended = False
        self.handled_offers: set[str] = set()
        self._changed = asyncio.Event()

    # ------------------------------------------------------------ waiting

    def _notify(self) -> None:
        """Wake every wait_until() so it re-checks its condition."""
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait_until(self, pred) -> None:
        while not pred() and self.running:
            await self._changed.wait()

    def position(self) -> tuple:
        """(round, phase) as a sortable key; ENDED sorts after everything."""
        if self.ended or self.public.get("phase") == "ENDED":
            return (float("inf"), 0)
        return (self.public.get("round", 0), PHASE_ORDER.get(self.public.get("phase"), 0))

    # ------------------------------------------------------------ rendering

    def name(self, pid) -> str:
        for p in self.public.get("players", []):
            if p["id"] == pid:
                return clean(p["name"])
        return clean(pid)

    def prompt(self) -> str:
        phase = self.public.get("phase", "?")
        rnd = self.public.get("round", 0)
        left = max(0, int(self.deadline - time.monotonic())) if self.deadline else 0
        clock = f" {left}s" if phase in ("NEGOTIATION", "MOVEMENT", "AUCTION") else ""
        return f"[{self.name(self.me) if self.me else '-'} R{rnd} {phase}{clock}]> "

    def out(self, tag: str, text: str) -> None:
        rnd = self.public.get("round", 0)
        phase = self.public.get("phase", "")
        label = f"[{tag} R{rnd} {phase}]" if rnd else f"[{tag}]"
        if self.color:
            label = TAGS.get(tag, "") + label + RESET
        if self.interactive:
            sys.stdout.write("\r\033[K")
        sys.stdout.write(f"{label} {text}\n")
        if self.interactive and self.running:
            sys.stdout.write(self.prompt())
        sys.stdout.flush()

    def show(self, text: str) -> None:
        """Untagged command output (/help, /board, /status ...)."""
        print(text)

    def apply_snapshot(self, snap: dict) -> None:
        self.public = snap["public"]
        priv = snap["private"]
        self.me = priv["you"]
        self.offers = {o["offer_id"]: o for o in priv["offers"]}
        self.bids = {int(k): v for k, v in priv["bids"].items()}
        self.movement = priv.get("movement")
        self.deadline = time.monotonic() + self.public.get("remaining", 0)
        self._notify()

    def render_board(self) -> str:
        owners = self.public.get("owners", {})
        players = self.public.get("players", [])
        marks = {p["id"]: str(n) for n, p in enumerate(players, 1)}
        pawns: dict[int, list[str]] = {}
        for p in players:
            pawns.setdefault(p["pos"], []).append(marks[p["id"]])
        cells = {t.index: self._tile_cell(t, owners.get(str(t.index)), pawns.get(t.index, []))
                 for t in TILES}
        centre = self._board_centre(players, marks)

        full = "+" + "+".join(["-" * CELL_W] * 4) + "+"
        side = "+" + "-" * CELL_W + "+"
        lines = [full]
        for row in range(4):
            for i in range(CELL_LINES):
                if row in (1, 2):
                    mid = centre[(row - 1) * (CELL_LINES + 1) + i]
                    lines.append(f"|{cells[RING[row, 0]][i]}|{mid}|{cells[RING[row, 3]][i]}|")
                else:
                    lines.append("|" + "|".join(cells[RING[row, c]][i] for c in range(4)) + "|")
            lines.append(side + centre[CELL_LINES] + side if row == 1 else full)
        return "\n".join(lines)

    def _tile_cell(self, t, owner, here: list[str]) -> list[str]:
        """The CELL_LINES padded lines of one tile box."""
        text = [f"{t.index} {t.name}"]
        if t.is_property:
            text += [f"{t.district} ${t.face_value}", f"rent {t.base_rent}",
                     f"owner {self.name(owner) if owner else '-'}"]
        elif t.category == "special":
            text += [f"{'+' if t.cash_effect > 0 else ''}{t.cash_effect} credits", "", ""]
        else:
            text += [f"+{board.START_BONUS} passing", "", ""]
        text.append("@ " + " ".join(here) if here else "")
        cell = [(" " + line)[:CELL_W].ljust(CELL_W) for line in text]
        if self.color and t.is_property:
            cell[1] = DISTRICT_COLORS.get(t.district, "") + cell[1] + RESET
        return cell

    def _board_centre(self, players: list, marks: dict) -> list[str]:
        """Title and player legend shown inside the ring."""
        phase = self.public.get("phase", "?")
        rnd = self.public.get("round", 0)
        text = [" SYNDICATE CITY", f" Round {rnd}  {phase}" if rnd else f" {phase}",
                " clockwise from START -->", ""]
        for p in players:
            flag = " you" if p["id"] == self.me else "" if p["connected"] else " off"
            text.append(f" {marks[p['id']]} {clean(p['name'])[:12]:<12} {p['cash']:>6}{flag}")
        size = 2 * CELL_LINES + 1
        return [line[:CENTRE_W].ljust(CENTRE_W) for line in (text + [""] * size)[:size]]

    def render_board_list(self) -> str:
        owners = self.public.get("owners", {})
        pawns: dict[int, list[str]] = {}
        for p in self.public.get("players", []):
            pawns.setdefault(p["pos"], []).append(clean(p["name"]))
        lines = []
        for t in TILES:
            if t.is_property:
                owner = owners.get(str(t.index))
                desc = (f"{t.district:<6} value {t.face_value} rent {t.base_rent} "
                        f"min bid {t.min_bid}  owner: {self.name(owner) if owner else '-'}")
            elif t.category == "special":
                desc = f"{'+' if t.cash_effect > 0 else ''}{t.cash_effect} credits"
            else:
                desc = f"+{board.START_BONUS} when passing"
            here = ", ".join(pawns.get(t.index, []))
            lines.append(f"  {t.index:>2} {t.name:<11} {desc}" + (f"   <- {here}" if here else ""))
        return "\n".join(lines)

    def render_players(self) -> str:
        lines = []
        for p in self.public.get("players", []):
            props = ", ".join(TILES[i].name for i in p["props"]) or "-"
            flag = "" if p["connected"] else " (disconnected)"
            lines.append(f"  {p['id']} {clean(p['name']):<12} cash {p['cash']:>5}  "
                         f"tile {p['pos']:>2}  {props}{flag}")
        return "\n".join(lines)

    def render_status(self) -> str:
        me = next((p for p in self.public.get("players", []) if p["id"] == self.me), None)
        left = max(0.0, self.deadline - time.monotonic())
        lines = [f"round {self.public.get('round')}/{self.public.get('rounds')}  "
                 f"phase {self.public.get('phase')}  ~{left:.0f}s left (server clock decides)"]
        prio = [self.name(p) for p in self.public.get("priority", [])]
        lines.append("tie-break priority: " + " > ".join(prio))
        if me:
            reserved = sum(self.bids.values())
            lines.append(f"you: cash {me['cash']}  reserved {reserved}  "
                         f"available {max(0, me['cash'] - reserved)}  tile {me['pos']}")
        if self.public.get("auctions"):
            lines.append("open auctions: " + ", ".join(
                f"{TILES[t].name} ({t}, min {TILES[t].min_bid})" for t in self.public["auctions"]))
        if self.bids:
            lines.append("your bids: " + ", ".join(f"{TILES[t].name}={a}" for t, a in self.bids.items()))
        if self.movement:
            lines.append(f"your movement choice: {self.movement}")
        if self.offers:
            lines.append(self.render_offers())
        return "\n".join(lines)

    def render_offers(self) -> str:
        if not self.offers:
            return "no pending offers"
        lines = []
        for o in self.offers.values():
            direction = "OUT" if o["seller"] == self.me else "IN "
            lines.append(f"  {direction} {o['offer_id']}: {self.name(o['seller'])} sells "
                         f"{TILES[o['property_id']].name} to {self.name(o['buyer'])} for {o['price']}")
        return "\n".join(lines)

    # --------------------------------------------------------------- events

    def on_event(self, ev: dict) -> None:
        self._on_event(ev)
        if ev["type"] == "MatchEnded":
            self.ended = True
        self._notify()

    def _on_event(self, ev: dict) -> None:
        t, d = ev["type"], ev["data"]
        if ev.get("missed_public"):
            self.out("ERROR", f"missed {ev['missed_public']} public update(s); refreshing")
            asyncio.ensure_future(self.refresh())
        if t == "BoardUpdated":
            self.public = d
            self.deadline = time.monotonic() + d.get("remaining", 0)
        elif t == "PhaseChanged":
            self.public.update(round=d["round"], phase=d["phase"], priority=d["priority"],
                               auctions=d["auctions"], remaining=d["remaining"])
            self.deadline = time.monotonic() + d["remaining"]
            if d["phase"] == "NEGOTIATION":
                self.bids.clear()
                self.movement = None
                prio = " > ".join(self.name(p) for p in d["priority"])
                self.out("SERVER", f"=== Round {d['round']} === NEGOTIATION for {d['remaining']:.0f}s. "
                                   f"Tie-break priority: {prio}")
            elif d["phase"] == "MOVEMENT":
                self.out("SERVER", f"MOVEMENT: choose /move 1|2|3 within {d['remaining']:.0f}s "
                                   "(default 1)")
            elif d["phase"] == "AUCTION":
                self.out("SERVER", f"AUCTION open for {d['remaining']:.0f}s")
            elif d["phase"] == "SETTLEMENT":
                self.out("SERVER", "SETTLEMENT")
        elif t == "MatchStarted":
            self.out("SERVER", f"Match {clean(d['match'])} started: {d['rounds']} rounds, players "
                     + ", ".join(clean(p["name"]) for p in d["players"]))
        elif t in ("PlayerJoined", "PlayerRejoined", "PlayerDisconnected"):
            verb = {"PlayerJoined": "joined", "PlayerRejoined": "rejoined",
                    "PlayerDisconnected": "disconnected"}[t]
            self.out("SERVER", f"{clean(d['name'])} {verb}")
            if d["player"] != self.me:
                asyncio.ensure_future(self.refresh())
        elif t == "ChatMessage":
            sender = self.name(d["from"])
            text = clean(d["text"])
            if d["scope"] == "public":
                self.out("PUBLIC", f"{sender}: {text}")
            elif d["scope"] == "private":
                if d["from"] == self.me:
                    self.out("PRIVATE", f"you -> {self.name(d['to'][0])}: {text}")
                else:
                    self.out("PRIVATE", f"{sender} -> you: {text}")
            else:
                to = ",".join(self.name(p) for p in d["to"])
                self.out("GROUP", f"{sender} -> [{to}]: {text}")
        elif t in ("TradeOffered", "TradeUpdated"):
            o = d
            prop = TILES[o["property_id"]].name
            if o["status"] == "PENDING":
                self.offers[o["offer_id"]] = o
                if o["buyer"] == self.me:
                    self.out("TRADE", f"{self.name(o['seller'])} offers you {prop} for {o['price']} "
                                      f"-> /accept {o['offer_id']} or /reject {o['offer_id']}")
                else:
                    self.out("TRADE", f"offer {o['offer_id']} sent: {prop} to "
                                      f"{self.name(o['buyer'])} for {o['price']}")
            else:
                self.offers.pop(o["offer_id"], None)
                self.out("TRADE", f"offer {o['offer_id']} ({prop}, {o['price']}, "
                                  f"{self.name(o['seller'])} -> {self.name(o['buyer'])}): {o['status']}")
        elif t == "MovementRevealed":
            lines = []
            for m in d["moves"]:
                fx = []
                for e in m["effects"]:
                    if e["kind"] == "start_bonus":
                        fx.append(f"passed START +{e['amount']}")
                    elif e["kind"] == "tile":
                        fx.append(f"{e['tile']} {e['amount']:+d}")
                    elif e["kind"] == "discovered":
                        fx.append(f"discovered {TILES[e['property_id']].name}")
                    elif e["kind"] == "rent":
                        fx.append(f"paid {e['amount']} rent to {self.name(e['owner'])}")
                dflt = " (default)" if m["defaulted"] else ""
                lines.append(f"  {self.name(m['player']):<12} {m['steps']}{dflt}: "
                             f"{TILES[m['from']].name} -> {TILES[m['to']].name}"
                             + (f"; {'; '.join(fx)}" if fx else ""))
            self.out("SERVER", "Movement revealed:\n" + "\n".join(lines))
            self.movement = None
        elif t == "AuctionOpened":
            items = ", ".join(
                f"{a['name']} [{a['property_id']}] min {a['min_bid']} "
                f"(found by {', '.join(self.name(x) for x in a['discoverers'])})"
                for a in d["auctions"])
            self.out("AUCTION", f"Sealed auctions: {items}. Use /bid <property> <amount>.")
        elif t == "BidStatus":
            prop = TILES[d["property_id"]].name
            status = d["status"]
            if status == "ACTIVE":
                self.bids[d["property_id"]] = d["reserved"]
                self.out("AUCTION", f"your bid on {prop}: {d['reserved']} (available {d['available']})")
            elif status == "CANCELLED":
                self.bids.pop(d["property_id"], None)
                self.out("AUCTION", f"bid on {prop} cancelled (available {d['available']})")
            else:
                self.bids.pop(d["property_id"], None)
                extra = f", paid {d['paid']}" if status == "WON" else ""
                self.out("AUCTION", f"your bid of {d['amount']} on {prop}: {status}{extra}")
        elif t == "AuctionClosed":
            if d["winner"]:
                disc = f" (incl. -{d['discount']} discoverer discount)" if d.get("discount") else ""
                self.out("AUCTION", f"{d['name']} won by {self.name(d['winner'])} for {d['paid']}{disc}")
            else:
                self.out("AUCTION", f"{d['name']}: no valid bids, stays unowned")
        elif t == "RoundSettled":
            self.out("SERVER", f"Round {d['round']} settled.\n{self.render_players()}")
        elif t == "MatchEnded":
            lines = []
            for i, s in enumerate(d["scores"], 1):
                lines.append(f"  {i}. {clean(s['name']):<12} net worth {s['net_worth']:>5}  "
                             f"cash {s['cash']:>5}  properties {len(s['properties'])}  "
                             f"districts {','.join(s['districts']) or '-'}")
            winners = ", ".join(self.name(w) for w in d["winners"])
            self.out("SERVER", "MATCH OVER. Final scoreboard:\n" + "\n".join(lines)
                     + f"\nWinner(s): {winners}")
        elif t == "Notice":
            self.out("SERVER", clean(d.get("text", "")))
        elif t == "_ForgedSignalIgnored":
            self.out("ERROR", f"ignored a GameEvent signal from non-coordinator {clean(d['sender'])}")
        elif t in ("_CoordinatorLost", "_BusDisconnected"):
            self.out("ERROR", f"{clean(d['reason'])}; exiting")
            self.running = False

    async def refresh(self) -> None:
        if not self.running or not self.conn.connected:
            return
        try:
            self.apply_snapshot(await self.conn.request("GetSnapshot"))
        except (ipc.RequestError, ipc.IPCError):
            pass

    # ------------------------------------------------------------- commands

    async def command(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if not line.startswith("/"):
            line = "/say " + line
        parts = line.split(None, 1)
        cmd, rest = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        cmd = ALIASES.get(cmd, cmd)
        args = rest.split()
        try:
            await self._command(cmd, rest, args)
        except ipc.RequestError as exc:
            self.out("ERROR", f"{exc.code}: {clean(exc.message)}")
        except ipc.IPCError as exc:
            self.out("ERROR", clean(str(exc)))
        except (ValueError, IndexError):
            self.out("ERROR", f"bad arguments for {cmd}; see /help")

    def _prop(self, ref: str) -> int:
        idx = board.find_property(ref)
        if idx is None:
            raise ValueError(ref)
        return idx

    async def _command(self, cmd: str, rest: str, args: list[str]) -> None:
        r = self.conn.request
        if cmd == "/help":
            self.show(HELP)
        elif cmd == "/board":
            self.show(self.render_board_list() if args[:1] == ["list"] else self.render_board())
        elif cmd == "/status":
            await self.refresh()
            self.show(self.render_status())
        elif cmd == "/players":
            await self.refresh()
            self.show(self.render_players())
        elif cmd == "/offers":
            await self.refresh()
            self.show(self.render_offers())
        elif cmd == "/say":
            await self.conn.send_public(rest)
        elif cmd == "/tell":
            to, text = rest.split(None, 1)
            await self.conn.send_private(to, text)
        elif cmd == "/group":
            to, text = rest.split(None, 1)
            await self.conn.send_group([x for x in to.split(",") if x], text)
        elif cmd == "/offer":
            if len(args) < 3:
                raise ValueError
            buyer, price = args[0], int(args[-1])
            await r("CreateTradeOffer", {"buyer": buyer, "property_id": self._prop(" ".join(args[1:-1])),
                                         "price": price})
        elif cmd in ("/accept", "/reject", "/canceloffer"):
            action = {"/accept": "AcceptTradeOffer", "/reject": "RejectTradeOffer",
                      "/canceloffer": "CancelTradeOffer"}[cmd]
            res = await r(action, {"offer_id": args[0]})
            if cmd == "/accept":
                self.out("TRADE", f"bought {TILES[res['property_id']].name} for {res['price']}")
        elif cmd == "/move":
            res = await r("SubmitMovement", {"steps": int(args[0])})
            self.movement = res["steps"]
            self.out("SERVER", f"movement choice recorded: {res['steps']} (private until reveal)")
        elif cmd == "/bid":
            if len(args) < 2:
                raise ValueError
            await r("SubmitBid", {"property_id": self._prop(" ".join(args[:-1])), "amount": int(args[-1])})
        elif cmd == "/cancelbid":
            await r("CancelBid", {"property_id": self._prop(rest)})
        elif cmd in ("/quit", "/exit"):
            try:
                await r("Leave", timeout=2.0)
            except (ipc.RequestError, ipc.IPCError):
                pass
            self.running = False
        else:
            self.out("ERROR", f"unknown command {clean(cmd)}; try /help")


# ------------------------------------------------------------------ scripts

def parse_script(text: str) -> list[tuple[int, str, list]]:
    """Return [(line_no, kind, args)]; raises ValueError naming the bad line."""
    steps = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("@"):
            steps.append((n, "cmd", [line]))
            continue
        word, *rest = line[1:].split()
        word = word.lower()
        try:
            if word == "round" and 1 <= len(rest) <= 2:
                phase = rest[1].upper() if len(rest) == 2 else "NEGOTIATION"
                if phase not in SCRIPT_PHASES:
                    raise ValueError
                steps.append((n, "round", [int(rest[0]), phase]))
            elif word == "wait" and len(rest) == 1:
                steps.append((n, "wait", [max(0.0, float(rest[0]))]))
            elif word in ("accept", "reject") and len(rest) <= 1:
                steps.append((n, word, [rest[0].lower() if rest else None]))
            elif word == "end" and not rest:
                steps.append((n, "end", []))
            else:
                raise ValueError
        except ValueError:
            raise ValueError(f"line {n}: cannot understand {line!r}") from None
    return steps


async def run_script(ui: ClientUI, steps: list[tuple[int, str, list]]) -> None:
    """Play the steps, then stay for the final scoreboard and return."""
    skipping = False
    for n, kind, a in steps:
        if not ui.running:
            return
        if kind == "round":
            target = (a[0], PHASE_ORDER[a[1]])
            await ui.wait_until(lambda: ui.position() >= target)
            skipping = ui.position() != target
            if skipping and not ui.ended:
                ui.out("SCRIPT", f"skipping round {a[0]} {a[1].lower()} (line {n}): "
                                 "that phase already passed or did not happen")
        elif skipping:
            continue
        elif kind == "end":
            break
        elif kind == "wait":
            await asyncio.sleep(a[0])
        elif kind == "cmd":
            ui.out("SCRIPT", clean(a[0]))
            await ui.command(a[0])
        else:   # accept / reject the next offer addressed to us
            where = ui.position()
            seen = set(ui.handled_offers)

            def incoming():
                for o in ui.offers.values():
                    if (o["buyer"] == ui.me and o["offer_id"] not in seen
                            and (a[0] is None or a[0] in (o["seller"], ui.name(o["seller"]).lower()))):
                        return o
                return None
            await ui.wait_until(lambda: incoming() is not None or ui.position() != where)
            o = incoming()
            if o is None:
                ui.out("SCRIPT", f"no offer arrived for @{kind} (line {n})")
                continue
            ui.handled_offers.add(o["offer_id"])
            line = f"/{kind} {o['offer_id']}"
            ui.out("SCRIPT", line)
            await ui.command(line)
    await ui.wait_until(lambda: ui.ended)
    await asyncio.sleep(0.5)      # let the scoreboard print before we disconnect


async def run(args) -> int:
    steps = None
    if args.script:
        try:
            with open(args.script, encoding="utf-8") as fh:
                steps = parse_script(fh.read())
        except (OSError, ValueError) as exc:
            print(f"cannot use script {args.script}: {exc}", file=sys.stderr)
            return 1
    try:
        address = args.bus_address or ipc.read_bus_address(args.bus_address_file)
    except PermissionError:
        print(f"cannot join: no permission to read {args.bus_address_file} "
              "(is this account a roster player in the 'syndicate' group?)", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"cannot join: {args.bus_address_file} not found (is the server running?)",
              file=sys.stderr)
        return 1
    except ipc.IPCError as exc:
        print(f"cannot join: {exc}", file=sys.stderr)
        return 1
    try:
        conn = await ipc.GameIPC.connect_client(address)
        snap = await conn.join(args.name)
    except (ipc.IPCError, ipc.RequestError) as exc:
        print(f"cannot join: {exc}", file=sys.stderr)
        return 1
    interactive = steps is None and sys.stdin.isatty() and sys.stdout.isatty()
    if args.tui and steps is None:
        if not interactive:
            print("--tui needs an interactive terminal; using the line client", file=sys.stderr)
        else:
            import tui
            return await tui.run(conn, snap, color=not args.no_color)
    ui = ClientUI(conn, color=sys.stdout.isatty() and not args.no_color, interactive=interactive)
    ui.apply_snapshot(snap)
    print(f"Joined as {ui.name(ui.me)} ({ui.me}); coordinator {conn.coordinator}. "
          + (f"Playing script {args.script}." if steps is not None else "Type /help."))
    print(ui.render_players())

    async def pump_events():
        async for ev in conn.events():
            ui.on_event(ev)
            if not ui.running:
                return

    async def pump_input():
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        if interactive:
            sys.stdout.write(ui.prompt())
            sys.stdout.flush()
        while ui.running:
            line = await reader.readline()
            if not line:
                return
            await ui.command(line.decode(errors="replace"))
            if interactive and ui.running:
                # out() may already have redrawn the prompt on this line; replace it.
                sys.stdout.write("\r\033[K" + ui.prompt())
                sys.stdout.flush()

    driver = pump_input() if steps is None else run_script(ui, steps)
    tasks = [asyncio.ensure_future(pump_events()), asyncio.ensure_future(driver)]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    await conn.close()
    print()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Syndicate City player client",
                                 epilog=SCRIPT_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bus-address", help="dedicated bus address (unix:path=...)")
    ap.add_argument("--bus-address-file", default=ipc.default_bus_address_file())
    ap.add_argument("--name", help="display-name suggestion (roster name takes precedence)")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--tui", action="store_true",
                    help="full-screen interface with a live board, status panel and timer")
    ap.add_argument("--script", metavar="FILE", help="play the commands in FILE instead of reading "
                                                     "the keyboard (format below)")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
