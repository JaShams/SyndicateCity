"""Pure, deterministic Syndicate City rules engine (PRD sections 4 and 9).

This module has no I/O, no clock and no D-Bus. Every method that depends on
time takes ``now`` (a ``time.monotonic()`` value supplied by the coordinator),
which makes the engine fully testable with scripted timestamps.

State-changing methods either raise :class:`GameError` *before* mutating
anything, or commit completely and return a list of :class:`Event` objects for
the coordinator to deliver. Callers must serialise calls (the coordinator holds
one lock around each call), so validation and commit never interleave.
"""

from __future__ import annotations

import enum
import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

import board
from board import TILES, DISTRICTS, PROPERTY_IDS, BOARD_SIZE

MAX_CHAT_CHARS = 300
MAX_GROUP_RECIPIENTS = 5
MAX_AMOUNT = 1_000_000
MAX_PHASE_SECONDS = 3600.0
MAX_TRADE_PROPS = 4          # properties per side of one offer
MAX_LINKED_OFFERS = 3        # offers in one all-or-nothing group
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩]")


class Phase(str, enum.Enum):
    LOBBY = "LOBBY"
    NEGOTIATION = "NEGOTIATION"
    MOVEMENT = "MOVEMENT"
    AUCTION = "AUCTION"
    SETTLEMENT = "SETTLEMENT"
    ENDED = "ENDED"


READY_PHASES = (Phase.NEGOTIATION, Phase.MOVEMENT, Phase.AUCTION)


class OfferStatus(str, enum.Enum):
    PENDING = "PENDING"
    AGREED = "AGREED"          # recipient said yes; waiting for the rest of its linked group
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"


class GameError(Exception):
    """A rule violation. Raised before any state is mutated."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Event:
    type: str
    data: dict
    visibility: str = "public"          # "public" | "private"
    recipients: tuple = ()              # player ids, for private events

    @classmethod
    def private(cls, type_: str, data: dict, recipients: Iterable[str]) -> "Event":
        # dict.fromkeys keeps order while removing duplicates.
        return cls(type_, data, "private", tuple(dict.fromkeys(recipients)))


def check_phase_seconds(value, name: str = "phase duration") -> float:
    """Return ``value`` as a float if it is a usable phase length, else raise ValueError.

    nan, inf, zero and negative lengths would stall a phase forever or end it at once.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or not (0 < value <= MAX_PHASE_SECONDS):
        raise ValueError(f"{name} must be a finite number of seconds in (0, {MAX_PHASE_SECONDS:g}]")
    return value


@dataclass
class Settings:
    rounds: int = 7
    negotiation_seconds: float = 45.0
    movement_seconds: float = 15.0
    auction_seconds: float = 25.0
    starting_cash: int = 600
    min_players: int = 4
    max_players: int = 6
    max_pending_offers_per_proposer: int = 3
    max_pending_offers_per_recipient: int = 6
    # Negotiation is pointless while nobody owns anything (always true in round 1).
    skip_empty_negotiation: bool = True

    def __post_init__(self):
        if isinstance(self.rounds, bool) or not isinstance(self.rounds, int) or self.rounds < 1:
            raise ValueError("rounds must be an integer >= 1")
        for name in ("negotiation_seconds", "movement_seconds", "auction_seconds"):
            setattr(self, name, check_phase_seconds(getattr(self, name), name))


@dataclass
class Player:
    id: str
    registered_uid: int
    display_name: str
    cash: int
    position: int = board.START_TILE
    owned_property_ids: set = field(default_factory=set)
    joined: bool = False
    connected: bool = False


@dataclass
class Property:
    tile_id: int
    name: str
    district: str
    face_value: int
    base_rent: int
    owner_id: Optional[str] = None


@dataclass
class TradeOffer:
    """``from_id`` proposes to hand over ``give_*`` in exchange for ``to_id``'s ``get_*``.

    Offers sharing a ``group`` are linked: they all settle together or not at all.
    """
    id: str
    from_id: str
    to_id: str
    give_props: tuple
    give_cash: int
    get_props: tuple
    get_cash: int
    round_created: int
    group: Optional[str] = None
    status: OfferStatus = OfferStatus.PENDING

    @property
    def props(self) -> tuple:
        return self.give_props + self.get_props

    @property
    def parties(self) -> tuple:
        return (self.from_id, self.to_id)

    @property
    def is_open(self) -> bool:
        return self.status in (OfferStatus.PENDING, OfferStatus.AGREED)

    def to_dict(self, linked: int = 0) -> dict:
        return {"offer_id": self.id, "from": self.from_id, "to": self.to_id,
                "give": {"props": list(self.give_props), "cash": self.give_cash},
                "get": {"props": list(self.get_props), "cash": self.get_cash},
                "group": self.group, "linked": linked,
                "round": self.round_created, "status": self.status.value}


@dataclass
class Auction:
    property_id: int
    discoverer_ids: set
    deadline: float
    active_bids: dict = field(default_factory=dict)   # player_id -> amount
    closed: bool = False

    @property
    def contested(self) -> bool:
        """Two or more landers bid against each other; a single lander just buys."""
        return len(self.discoverer_ids) > 1

    @property
    def min_bid(self) -> int:
        tile = TILES[self.property_id]
        return tile.min_bid if self.contested else tile.face_value


def sanitize_text(text: str) -> str:
    """Strip terminal control characters (escape injection, bidi overrides)."""
    return _CONTROL_RE.sub("", text)


class Game:
    def __init__(self, roster: list[tuple[str, int, str]], settings: Optional[Settings] = None,
                 match_id: str = "match"):
        """``roster`` is a list of ``(player_id, uid, display_name)`` in slot order."""
        self.settings = settings or Settings()
        s = self.settings
        if not (s.min_players <= len(roster) <= s.max_players):
            raise ValueError(f"roster must have {s.min_players}-{s.max_players} players")
        if s.rounds < 1:
            raise ValueError("rounds must be >= 1")
        ids = [r[0] for r in roster]
        uids = [r[1] for r in roster]
        names = [r[2] for r in roster]
        if len(set(ids)) != len(ids) or len(set(uids)) != len(uids) or len(set(names)) != len(names):
            raise ValueError("roster player ids, UIDs and display names must all be distinct")
        for n in names:
            if not NAME_RE.match(n):
                raise ValueError(f"invalid display name {n!r}")

        self.match_id = match_id
        self.round_number = 0
        self.phase = Phase.LOBBY
        self.phase_deadline_monotonic = 0.0
        self.players: dict[str, Player] = {
            pid: Player(pid, uid, name, s.starting_cash) for pid, uid, name in roster}
        self.slot_order: list[str] = ids
        self.properties: dict[int, Property] = {
            t.index: Property(t.index, t.name, t.district, t.face_value, t.base_rent)
            for t in TILES if t.is_property}
        self.offers: dict[str, TradeOffer] = {}
        self.auctions: dict[int, Auction] = {}
        self.decisions: dict[str, int] = {}
        self.ready: set[str] = set()          # players finished with the current phase
        self.player_priority_order: list[str] = list(ids)
        self.last_movement: list[dict] = []
        self.final_scores: Optional[list[dict]] = None
        self._offer_counter = 0
        self._settled_rounds: set[int] = set()
        self._settled_auctions: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------ lookups

    def resolve_player(self, ref) -> str:
        if not isinstance(ref, str):
            raise GameError("UNKNOWN_PLAYER", "player reference must be a string")
        key = ref.strip().lower()
        for p in self.players.values():
            if key == p.id.lower() or key == p.display_name:
                return p.id
        raise GameError("UNKNOWN_PLAYER", f"no player named {ref!r}")

    def name_of(self, pid: str) -> str:
        return self.players[pid].display_name

    def district_complete(self, owner_id: Optional[str], district: str) -> bool:
        return owner_id is not None and all(
            self.properties[t].owner_id == owner_id for t in DISTRICTS[district])

    def rent_for(self, tile_id: int) -> int:
        prop = self.properties[tile_id]
        if prop.owner_id is None:
            return 0
        mult = 2 if self.district_complete(prop.owner_id, prop.district) else 1
        return prop.base_rent * mult

    def reserved(self, pid: str) -> int:
        return sum(a.active_bids.get(pid, 0) for a in self.auctions.values() if not a.closed)

    def available_cash(self, pid: str) -> int:
        return max(0, self.players[pid].cash - self.reserved(pid))

    def completed_districts(self, pid: str) -> list[str]:
        return [d for d in DISTRICTS if self.district_complete(pid, d)]

    def net_worth(self, pid: str) -> int:
        p = self.players[pid]
        return (p.cash + sum(self.properties[t].face_value for t in p.owned_property_ids)
                + board.DISTRICT_BONUS * len(self.completed_districts(pid)))

    def seconds_remaining(self, now: float) -> float:
        if self.phase in (Phase.LOBBY, Phase.ENDED, Phase.SETTLEMENT):
            return 0.0
        return round(max(0.0, self.phase_deadline_monotonic - now), 1)

    # ------------------------------------------------------------- snapshots

    def public_snapshot(self, now: float) -> dict:
        """Everything every player may see. Never contains secrets."""
        return {
            "match": self.match_id,
            "round": self.round_number,
            "rounds": self.settings.rounds,
            "phase": self.phase.value,
            "remaining": self.seconds_remaining(now),
            "priority": list(self.player_priority_order),
            "players": [{
                "id": p.id, "name": p.display_name, "pos": p.position, "cash": p.cash,
                "props": sorted(p.owned_property_ids), "connected": p.connected,
            } for p in (self.players[i] for i in self.slot_order)],
            "owners": {str(t): pr.owner_id for t, pr in self.properties.items() if pr.owner_id},
            "auctions": sorted(t for t, a in self.auctions.items() if not a.closed),
            "landers": {str(t): sorted(a.discoverer_ids)
                        for t, a in self.auctions.items() if not a.closed},
            "ready": sorted(self.ready) if self.phase in READY_PHASES else [],
        }

    def private_view(self, pid: str) -> dict:
        """Secrets visible to ``pid`` only: own offers, own bids, own movement choice."""
        offers = [self._offer_dict(o) for o in self.offers.values()
                  if o.is_open and pid in o.parties]
        bids = {str(t): a.active_bids[pid] for t, a in self.auctions.items()
                if not a.closed and pid in a.active_bids}
        return {"you": pid, "offers": offers, "bids": bids,
                "movement": self.decisions.get(pid), "available": self.available_cash(pid)}

    def snapshot_for(self, pid: str, now: float) -> dict:
        return {"public": self.public_snapshot(now), "private": self.private_view(pid)}

    # -------------------------------------------------------------- helpers

    def _require_phase(self, phase: Phase, now: float) -> None:
        if self.phase != phase:
            raise GameError("WRONG_PHASE", f"action only allowed during {phase.value} "
                                           f"(current phase: {self.phase.value})")
        if now >= self.phase_deadline_monotonic:
            raise GameError("DEADLINE_PASSED", f"{phase.value} deadline has passed")

    def _require_player(self, pid: str) -> Player:
        p = self.players.get(pid)
        if p is None or not p.joined:
            raise GameError("NOT_JOINED", "caller has not joined the match")
        return p

    @staticmethod
    def _require_int(value, name: str, lo: int, hi: int = MAX_AMOUNT) -> int:
        if type(value) is not int or not (lo <= value <= hi):
            raise GameError("INVALID_ARGUMENT", f"{name} must be an integer in [{lo}, {hi}]")
        return value

    def _require_property(self, value) -> Property:
        if type(value) is not int or value not in self.properties:
            raise GameError("UNKNOWN_PROPERTY", "property_id must be a property tile index")
        return self.properties[value]

    def _transfer_property(self, tile_id: int, new_owner: Optional[str]) -> None:
        prop = self.properties[tile_id]
        if prop.owner_id is not None:
            self.players[prop.owner_id].owned_property_ids.discard(tile_id)
        prop.owner_id = new_owner
        if new_owner is not None:
            self.players[new_owner].owned_property_ids.add(tile_id)

    def _enter_phase(self, phase: Phase, seconds: float, now: float) -> None:
        self.phase = phase
        self.phase_deadline_monotonic = now + seconds
        self.ready.clear()

    def _phase_event(self, now: float) -> Event:
        return Event("PhaseChanged", {
            "round": self.round_number, "phase": self.phase.value,
            "remaining": self.seconds_remaining(now),
            "priority": list(self.player_priority_order),
            "auctions": sorted(t for t, a in self.auctions.items() if not a.closed),
        })

    def _board_event(self, now: float) -> Event:
        return Event("BoardUpdated", self.public_snapshot(now))

    # ------------------------------------------------------ lobby / sessions

    def mark_joined(self, pid: str) -> list[Event]:
        p = self.players[pid]
        was_joined, was_connected = p.joined, p.connected
        p.joined = True
        p.connected = True
        if was_connected:
            return []
        etype = "PlayerRejoined" if was_joined else "PlayerJoined"
        return [Event(etype, {"player": pid, "name": p.display_name})]

    def mark_disconnected(self, pid: str) -> list[Event]:
        p = self.players[pid]
        if not p.connected:
            return []
        p.connected = False
        return [Event("PlayerDisconnected", {"player": pid, "name": p.display_name})]

    def start_match(self, now: float) -> list[Event]:
        if self.phase != Phase.LOBBY:
            raise GameError("WRONG_PHASE", "match already started")
        missing = [p.display_name for p in self.players.values() if not p.connected]
        if missing:
            raise GameError("NOT_READY", "waiting for players: " + ", ".join(missing))
        events = [Event("MatchStarted", {
            "match": self.match_id, "rounds": self.settings.rounds,
            "players": [{"id": i, "name": self.name_of(i)} for i in self.slot_order]})]
        events += self._begin_round(1, now)
        return events

    # ------------------------------------------------------- phase machine

    def _begin_round(self, number: int, now: float) -> list[Event]:
        self.round_number = number
        self.decisions.clear()
        self.auctions.clear()
        n = len(self.slot_order)
        k = (number - 1) % n
        self.player_priority_order = self.slot_order[k:] + self.slot_order[:k]
        if self.settings.skip_empty_negotiation and not any(
                p.owner_id for p in self.properties.values()):
            self._enter_phase(Phase.MOVEMENT, self.settings.movement_seconds, now)
            return [self._phase_event(now), self._board_event(now),
                    Event("Notice", {"text": f"Nobody owns a property yet, so round {number} "
                                             "skips negotiation."})]
        self._enter_phase(Phase.NEGOTIATION, self.settings.negotiation_seconds, now)
        return [self._phase_event(now), self._board_event(now)]

    def all_ready(self) -> bool:
        """Every connected player is finished with this phase (and someone is connected).

        Disconnected players count as finished: they cannot act, and they would
        otherwise hold the table hostage until the deadline.
        """
        if self.phase not in READY_PHASES:
            return False
        connected = [p.id for p in self.players.values() if p.connected]
        return bool(connected) and all(pid in self.ready for pid in connected)

    def advance(self, now: float) -> list[Event]:
        """Close the current phase if its deadline has passed or everyone is ready.

        Idempotent otherwise."""
        if self.phase in (Phase.LOBBY, Phase.ENDED, Phase.SETTLEMENT):
            return []
        if now < self.phase_deadline_monotonic and not self.all_ready():
            return []
        if self.phase == Phase.NEGOTIATION:
            events = self._expire_offers()
            self._enter_phase(Phase.MOVEMENT, self.settings.movement_seconds, now)
            events.append(self._phase_event(now))
            return events
        if self.phase == Phase.MOVEMENT:
            events = self._resolve_movement(now)
            if self.auctions:
                self._enter_phase(Phase.AUCTION, self.settings.auction_seconds, now)
                for a in self.auctions.values():
                    a.deadline = self.phase_deadline_monotonic
                events.append(self._phase_event(now))     # phase first, so clients label it
                events.append(Event("AuctionOpened", {
                    "round": self.round_number,
                    "auctions": [{"property_id": t, "name": TILES[t].name,
                                  "min_bid": a.min_bid, "contested": a.contested,
                                  "discoverers": sorted(a.discoverer_ids)}
                                 for t, a in sorted(self.auctions.items())],
                    "priority": list(self.player_priority_order)}))
                events.append(self._board_event(now))
                return events
            return events + self._settle_round(now)
        if self.phase == Phase.AUCTION:
            return self._settle_round(now)
        return []

    def _expire_offers(self) -> list[Event]:
        events = []
        for o in self.offers.values():
            if o.is_open:
                o.status = OfferStatus.EXPIRED
                events.append(self._offer_event("TradeUpdated", o))
        return events

    def _resolve_movement(self, now: float) -> list[Event]:
        # Ownership snapshot taken after negotiation, before any movement effect.
        owners = {t: p.owner_id for t, p in self.properties.items()}
        rents = {t: self.rent_for(t) for t in self.properties}
        # Compute all destinations simultaneously, then settle in slot order.
        plan = []
        for pid in self.slot_order:
            p = self.players[pid]
            steps = self.decisions.get(pid, 1)
            dest = (p.position + steps) % BOARD_SIZE
            plan.append((pid, p.position, steps, dest, p.position + steps >= BOARD_SIZE,
                         pid not in self.decisions))
        moves = []
        for pid, start, steps, dest, passed_start, defaulted in plan:
            p = self.players[pid]
            p.position = dest
            entry = {"player": pid, "from": start, "steps": steps, "to": dest,
                     "defaulted": defaulted, "effects": []}
            if passed_start:
                p.cash += board.START_BONUS
                entry["effects"].append({"kind": "start_bonus", "amount": board.START_BONUS})
            t = TILES[dest]
            if t.category == "special":
                p.cash += t.cash_effect
                entry["effects"].append({"kind": "tile", "tile": t.name, "amount": t.cash_effect})
            elif t.is_property:
                owner = owners[dest]
                if owner is None:
                    a = self.auctions.get(dest)
                    if a is None:
                        a = self.auctions[dest] = Auction(dest, set(), 0.0)
                    a.discoverer_ids.add(pid)
                    entry["effects"].append({"kind": "discovered", "property_id": dest})
                elif owner != pid:
                    rent = rents[dest]
                    p.cash -= rent
                    self.players[owner].cash += rent
                    entry["effects"].append({"kind": "rent", "property_id": dest,
                                             "owner": owner, "amount": rent})
            moves.append(entry)
        self.last_movement = moves
        self.decisions.clear()
        return [Event("MovementRevealed", {"round": self.round_number, "moves": moves})]

    def _settle_round(self, now: float) -> list[Event]:
        if self.round_number in self._settled_rounds:
            raise AssertionError("round settled twice")
        self._settled_rounds.add(self.round_number)
        self._enter_phase(Phase.SETTLEMENT, 0.0, now)
        events = [self._phase_event(now)]
        priority = {pid: i for i, pid in enumerate(self.player_priority_order)}
        for tile_id in sorted(self.auctions):
            a = self.auctions[tile_id]
            key = (self.round_number, tile_id)
            if a.closed or key in self._settled_auctions:
                raise AssertionError("auction settled twice")
            self._settled_auctions.add(key)
            a.closed = True
            bids = dict(a.active_bids)
            if not bids:
                events.append(Event("AuctionClosed", {
                    "property_id": tile_id, "name": TILES[tile_id].name,
                    "winner": None, "paid": 0}))
                continue
            winner = min(bids, key=lambda pid: (-bids[pid], priority[pid]))
            amount = bids[winner]
            paid = amount
            self.players[winner].cash -= paid
            self._transfer_property(tile_id, winner)
            events.append(Event("AuctionClosed", {
                "property_id": tile_id, "name": TILES[tile_id].name, "winner": winner,
                "paid": paid, "contested": a.contested}))
            for pid, bid in bids.items():
                events.append(Event.private("BidStatus", {
                    "property_id": tile_id, "amount": bid,
                    "status": "WON" if pid == winner else "LOST", "paid": paid if pid == winner else 0},
                    (pid,)))
        self.auctions.clear()   # releases every remaining reservation
        if self.round_number >= self.settings.rounds:
            return events + self._end_match(now)
        events.append(self._board_event(now))           # settled balances before the summary
        events.append(Event("RoundSettled", {"round": self.round_number}))
        return events + self._begin_round(self.round_number + 1, now)

    def _end_match(self, now: float) -> list[Event]:
        self.phase = Phase.ENDED
        self.ready.clear()
        scores = []
        for pid in self.slot_order:
            p = self.players[pid]
            scores.append({"player": pid, "name": p.display_name, "cash": p.cash,
                           "properties": sorted(p.owned_property_ids),
                           "districts": self.completed_districts(pid),
                           "net_worth": self.net_worth(pid)})
        def rank_key(s):
            return (s["net_worth"], s["cash"], len(s["properties"]))
        scores.sort(key=rank_key, reverse=True)
        best = rank_key(scores[0])
        winners = [s["player"] for s in scores if rank_key(s) == best]
        self.final_scores = scores
        return [self._board_event(now),
                Event("MatchEnded", {"scores": scores, "winners": winners})]

    # ----------------------------------------------------------------- chat

    def _chat_text(self, text) -> str:
        if not isinstance(text, str):
            raise GameError("INVALID_ARGUMENT", "text must be a string")
        clean = sanitize_text(text).strip()
        if not clean:
            raise GameError("INVALID_ARGUMENT", "message is empty")
        if len(clean) > MAX_CHAT_CHARS:
            raise GameError("TOO_LONG", f"message exceeds {MAX_CHAT_CHARS} characters")
        return clean

    def _require_chat_phase(self, now: float) -> None:
        if self.phase not in (Phase.NEGOTIATION, Phase.AUCTION):
            raise GameError("WRONG_PHASE", "chat is only available during NEGOTIATION and AUCTION")
        if now >= self.phase_deadline_monotonic:
            raise GameError("DEADLINE_PASSED", "phase deadline has passed")

    def send_public(self, pid: str, text, now: float) -> tuple[dict, list[Event]]:
        self._require_player(pid)
        self._require_chat_phase(now)
        msg = self._chat_text(text)
        return {"delivered": "public"}, [Event("ChatMessage", {
            "scope": "public", "from": pid, "text": msg, "round": self.round_number})]

    def send_private(self, pid: str, to, text, now: float) -> tuple[dict, list[Event]]:
        self._require_player(pid)
        self._require_chat_phase(now)
        target = self.resolve_player(to)
        if target == pid:
            raise GameError("INVALID_ARGUMENT", "cannot message yourself")
        msg = self._chat_text(text)
        return {"delivered": [target]}, [Event.private("ChatMessage", {
            "scope": "private", "from": pid, "to": [target], "text": msg,
            "round": self.round_number}, (target, pid))]

    def send_group(self, pid: str, to, text, now: float) -> tuple[dict, list[Event]]:
        self._require_player(pid)
        self._require_chat_phase(now)
        if not isinstance(to, list) or not (1 <= len(to) <= MAX_GROUP_RECIPIENTS):
            raise GameError("INVALID_ARGUMENT",
                            f"group needs 1-{MAX_GROUP_RECIPIENTS} recipients")
        targets = [self.resolve_player(r) for r in to]
        if len(set(targets)) != len(targets):
            raise GameError("INVALID_ARGUMENT", "group recipients must be unique")
        if pid in targets:
            raise GameError("INVALID_ARGUMENT", "group recipients must exclude the sender")
        msg = self._chat_text(text)
        return {"delivered": targets}, [Event.private("ChatMessage", {
            "scope": "group", "from": pid, "to": targets, "text": msg,
            "round": self.round_number}, targets + [pid])]

    # --------------------------------------------------------------- trades

    def _require_offer(self, offer_id) -> TradeOffer:
        if not isinstance(offer_id, str) or offer_id not in self.offers:
            raise GameError("UNKNOWN_OFFER", "no such offer")
        return self.offers[offer_id]

    def _group(self, offer: TradeOffer) -> list[TradeOffer]:
        if offer.group is None:
            return [offer]
        return [o for o in self.offers.values() if o.group == offer.group]

    def _offer_dict(self, offer: TradeOffer) -> dict:
        return offer.to_dict(linked=len(self._group(offer)) - 1)

    def _offer_event(self, etype: str, offer: TradeOffer, reason: Optional[str] = None) -> Event:
        data = self._offer_dict(offer)
        if reason:
            data["reason"] = reason
        return Event.private(etype, data, (offer.to_id, offer.from_id))

    def _prop_list(self, value, name: str) -> tuple:
        if not isinstance(value, list) or len(value) > MAX_TRADE_PROPS:
            raise GameError("INVALID_ARGUMENT",
                            f"{name} must be a list of at most {MAX_TRADE_PROPS} properties")
        tiles = tuple(self._require_property(v).tile_id for v in value)
        if len(set(tiles)) != len(tiles):
            raise GameError("INVALID_ARGUMENT", f"{name} lists a property twice")
        return tiles

    def _close_group(self, members: list[TradeOffer], status: OfferStatus, reason: str,
                     primary: Optional[TradeOffer] = None) -> list[Event]:
        """Close every open offer in a group: ``primary`` gets ``status``, the rest INVALID.

        With no ``primary``, every open member gets ``status``."""
        events = []
        for m in members:
            if not m.is_open:
                continue
            if primary is None or m is primary:
                m.status = status
                events.append(self._offer_event("TradeUpdated", m, None if m is primary else reason))
            else:
                m.status = OfferStatus.INVALID
                events.append(self._offer_event("TradeUpdated", m, reason))
        return events

    def describe_trade(self, offer: TradeOffer) -> str:
        """Public wording: which properties moved. Amounts of credits are not published."""
        def side(props, cash):
            names = [TILES[t].name for t in props]
            if cash:
                names.append("credits")
            return " + ".join(names)
        return (f"{self.name_of(offer.from_id)} traded {side(offer.give_props, offer.give_cash)} "
                f"to {self.name_of(offer.to_id)} for {side(offer.get_props, offer.get_cash)}")

    def create_offer(self, pid: str, to_ref, give_props, give_cash, get_props, get_cash,
                     now: float, link=None):
        """Propose ``give_props`` + ``give_cash`` in exchange for ``get_props`` + ``get_cash``.

        ``link`` names one of your pending offers; the new offer joins its group, and
        the group settles only when every recipient has accepted.
        """
        self._require_player(pid)
        self._require_phase(Phase.NEGOTIATION, now)
        to = self.resolve_player(to_ref)
        if to == pid:
            raise GameError("INVALID_ARGUMENT", "cannot trade with yourself")
        give = self._prop_list(give_props, "give")
        get = self._prop_list(get_props, "get")
        give_cash = self._require_int(give_cash, "give_cash", 0)
        get_cash = self._require_int(get_cash, "get_cash", 0)
        if give_cash and get_cash:
            raise GameError("INVALID_ARGUMENT", "only one side of a trade can pay credits")
        if not give and not get:
            raise GameError("INVALID_ARGUMENT", "a trade must include at least one property")
        if not (give or give_cash) or not (get or get_cash):
            raise GameError("INVALID_ARGUMENT", "both sides of a trade must give something")
        for t in give:
            if self.properties[t].owner_id != pid:
                raise GameError("NOT_OWNER", f"you do not own {TILES[t].name}")
        for t in get:
            if self.properties[t].owner_id != to:
                raise GameError("NOT_OWNER", f"{self.name_of(to)} does not own {TILES[t].name}")
        base, group = None, None
        if link is not None:
            base = self._require_offer(link)
            if base.from_id != pid:
                raise GameError("UNKNOWN_OFFER", "no such offer")
            members = self._group(base)
            if any(m.status != OfferStatus.PENDING for m in members):
                raise GameError("OFFER_CLOSED", "can only link to offers nobody has answered yet")
            if len(members) >= MAX_LINKED_OFFERS:
                raise GameError("LIMIT_EXCEEDED", f"at most {MAX_LINKED_OFFERS} offers can be linked")
            if {t for m in members for t in m.props} & set(give + get):
                raise GameError("INVALID_ARGUMENT", "linked offers cannot share a property")
            group = base.group or base.id
        open_offers = [o for o in self.offers.values() if o.is_open]
        if sum(o.from_id == pid for o in open_offers) >= self.settings.max_pending_offers_per_proposer:
            raise GameError("LIMIT_EXCEEDED", "too many pending offers from you")
        if sum(o.to_id == to for o in open_offers) >= self.settings.max_pending_offers_per_recipient:
            raise GameError("LIMIT_EXCEEDED", "recipient has too many pending offers")
        self._offer_counter += 1
        offer = TradeOffer(f"o{self._offer_counter}", pid, to, give, give_cash, get, get_cash,
                           self.round_number, group)
        self.offers[offer.id] = offer
        if base is not None:
            base.group = group
        events = [self._offer_event("TradeOffered", offer)]
        if base is not None:
            # Recipients of the earlier offers learn their offer is now conditional.
            events += [self._offer_event("TradeUpdated", m) for m in self._group(offer)
                       if m is not offer]
        return self._offer_dict(offer), events

    def _offer_problem(self, offer: TradeOffer) -> Optional[tuple[str, str, bool]]:
        """Why ``offer`` cannot settle right now: (code, message, kills_the_offer)."""
        for t in offer.give_props:
            if self.properties[t].owner_id != offer.from_id:
                return ("OFFER_INVALID", f"{self.name_of(offer.from_id)} no longer owns "
                                         f"{TILES[t].name}", True)
        for t in offer.get_props:
            if self.properties[t].owner_id != offer.to_id:
                return ("OFFER_INVALID", f"{self.name_of(offer.to_id)} no longer owns "
                                         f"{TILES[t].name}", True)
        if self.available_cash(offer.from_id) < offer.give_cash:
            return ("OFFER_INVALID", f"{self.name_of(offer.from_id)} can no longer pay "
                                     f"{offer.give_cash} credits", True)
        if self.available_cash(offer.to_id) < offer.get_cash:
            return ("INSUFFICIENT_FUNDS", f"need {offer.get_cash} credits, available "
                                          f"{self.available_cash(offer.to_id)}", False)
        return None

    def _group_cash_problem(self, members: list[TradeOffer], acceptor: str):
        """Linked offers settle together, so check each player's *combined* payment."""
        delta: dict[str, int] = {}
        for m in members:
            cash = m.give_cash - m.get_cash          # what from_id pays to to_id
            delta[m.from_id] = delta.get(m.from_id, 0) - cash
            delta[m.to_id] = delta.get(m.to_id, 0) + cash
        for pid, d in delta.items():
            if d < 0 and self.available_cash(pid) < -d:
                if pid == acceptor:
                    return ("INSUFFICIENT_FUNDS", f"the linked trades need {-d} credits from you, "
                                                  f"available {self.available_cash(pid)}", False)
                return ("OFFER_INVALID", f"{self.name_of(pid)} cannot cover {-d} credits "
                                         "across the linked trades", True)
        return None

    def accept_offer(self, pid: str, offer_id, now: float):
        self._require_player(pid)
        offer = self._require_offer(offer_id)
        if offer.to_id != pid:
            # Do not reveal anything about offers addressed to other players.
            raise GameError("UNKNOWN_OFFER", "no such offer")
        self._require_phase(Phase.NEGOTIATION, now)
        if offer.status != OfferStatus.PENDING:
            raise GameError("OFFER_CLOSED", f"offer is {offer.status.value}")
        group = self._group(offer)
        others = [m for m in group if m is not offer]
        waiting = any(m.status == OfferStatus.PENDING for m in others)
        problem = self._offer_problem(offer)
        if problem is None and not waiting:
            # Last acceptance: every linked offer must still be good, all at once.
            for m in others:
                problem = self._offer_problem(m)
                if problem is not None:
                    problem = (problem[0], problem[1], True)
                    break
            else:
                problem = self._group_cash_problem(group, pid)
        if problem is not None:
            code, message, fatal = problem
            if fatal:
                raise GameErrorWithEvents(code, message, self._close_group(
                    group, OfferStatus.INVALID, "a linked offer became invalid"))
            raise GameError(code, message)
        if waiting:
            offer.status = OfferStatus.AGREED
            return self._offer_dict(offer), [self._offer_event("TradeUpdated", offer)]

        # Atomic settlement: all checks passed, now commit everything.
        events, moved = [], set()
        for m in group:
            cash = m.give_cash - m.get_cash
            self.players[m.from_id].cash -= cash
            self.players[m.to_id].cash += cash
            for t in m.give_props:
                self._transfer_property(t, m.to_id)
            for t in m.get_props:
                self._transfer_property(t, m.from_id)
            moved.update(m.props)
            m.status = OfferStatus.ACCEPTED
            events.append(self._offer_event("TradeUpdated", m))
        for other in list(self.offers.values()):
            if other.is_open and set(other.props) & moved:
                events += self._close_group(self._group(other), OfferStatus.INVALID,
                                            "a property in it was traded away")
        for m in group:
            events.append(Event("Notice", {"text": self.describe_trade(m)}))
        events.append(self._board_event(now))
        return self._offer_dict(offer), events

    def reject_offer(self, pid: str, offer_id, now: float):
        self._require_player(pid)
        offer = self._require_offer(offer_id)
        if offer.to_id != pid:
            raise GameError("UNKNOWN_OFFER", "no such offer")
        self._require_phase(Phase.NEGOTIATION, now)
        if offer.status != OfferStatus.PENDING:
            raise GameError("OFFER_CLOSED", f"offer is {offer.status.value}")
        events = self._close_group(self._group(offer), OfferStatus.REJECTED,
                                   "a linked offer was rejected", primary=offer)
        return self._offer_dict(offer), events

    def cancel_offer(self, pid: str, offer_id, now: float):
        self._require_player(pid)
        offer = self._require_offer(offer_id)
        if offer.from_id != pid:
            raise GameError("UNKNOWN_OFFER", "no such offer")
        self._require_phase(Phase.NEGOTIATION, now)
        if not offer.is_open:
            raise GameError("OFFER_CLOSED", f"offer is {offer.status.value}")
        events = self._close_group(self._group(offer), OfferStatus.CANCELLED,
                                   "a linked offer was cancelled", primary=offer)
        return self._offer_dict(offer), events

    # ---------------------------------------------------------------- ready

    def set_ready(self, pid: str, round_no, phase, ready, now: float):
        """Mark ``pid`` finished (or not) with the phase named by ``round_no``/``phase``.

        The client names the phase it means, so a /ready that arrives after the
        phase changed cannot end the next phase by accident.
        """
        self._require_player(pid)
        if self.phase not in READY_PHASES:
            raise GameError("WRONG_PHASE", f"nothing to be ready for during {self.phase.value}")
        if type(ready) is not bool:
            raise GameError("INVALID_ARGUMENT", "ready must be true or false")
        if round_no != self.round_number or phase != self.phase.value:
            raise GameError("STALE_PHASE", f"that phase is over (now round {self.round_number} "
                                           f"{self.phase.value})")
        if now >= self.phase_deadline_monotonic:
            raise GameError("DEADLINE_PASSED", f"{self.phase.value} deadline has passed")
        changed = (pid in self.ready) != ready
        if ready:
            self.ready.add(pid)
        else:
            self.ready.discard(pid)
        result = {"ready": ready, "round": self.round_number, "phase": self.phase.value}
        if not changed:
            return result, []
        return result, [self._ready_event(pid)]

    def _ready_event(self, pid: str) -> Event:
        connected = [p.id for p in self.players.values() if p.connected]
        return Event("PlayerReady", {
            "player": pid, "name": self.name_of(pid), "ready": pid in self.ready,
            "round": self.round_number, "phase": self.phase.value,
            "count": sum(p in self.ready for p in connected), "needed": len(connected)})

    # ------------------------------------------------------------- movement

    def submit_movement(self, pid: str, steps, now: float):
        self._require_player(pid)
        self._require_phase(Phase.MOVEMENT, now)
        steps = self._require_int(steps, "steps", 1, 3)
        self.decisions[pid] = steps
        return {"steps": steps}, []

    # -------------------------------------------------------------- auctions

    def submit_bid(self, pid: str, property_id, amount, now: float):
        self._require_player(pid)
        self._require_phase(Phase.AUCTION, now)
        if type(property_id) is not int or property_id not in self.auctions:
            raise GameError("NO_AUCTION", "that property is not being auctioned")
        auction = self.auctions[property_id]
        name = TILES[property_id].name
        if pid not in auction.discoverer_ids:
            raise GameError("NOT_ELIGIBLE", f"only players who landed on {name} can buy it")
        amount = self._require_int(amount, "amount", 0)
        minimum = auction.min_bid
        if not auction.contested and amount != minimum:
            raise GameError("FIXED_PRICE", f"{name} sells at its listed price of {minimum}")
        if amount < minimum:
            raise GameError("BID_TOO_LOW", f"minimum bid for {name} is {minimum}")
        old = auction.active_bids.get(pid, 0)
        available = max(0, self.players[pid].cash - (self.reserved(pid) - old))
        if amount > available:
            raise GameError("INSUFFICIENT_FUNDS",
                            f"bid {amount} exceeds available cash {available}")
        auction.active_bids[pid] = amount
        result = {"property_id": property_id, "reserved": amount,
                  "available": self.available_cash(pid)}
        return result, [Event.private("BidStatus", dict(result, status="ACTIVE"), (pid,))]

    def cancel_bid(self, pid: str, property_id, now: float):
        self._require_player(pid)
        self._require_phase(Phase.AUCTION, now)
        if type(property_id) is not int or property_id not in self.auctions:
            raise GameError("NO_AUCTION", "that property is not being auctioned")
        auction = self.auctions[property_id]
        if pid not in auction.active_bids:
            raise GameError("NO_BID", "you have no active bid on that property")
        del auction.active_bids[pid]
        result = {"property_id": property_id, "available": self.available_cash(pid)}
        return result, [Event.private("BidStatus", dict(result, status="CANCELLED"), (pid,))]

    # ------------------------------------------------------------ invariants

    def check_invariants(self) -> None:
        """Raise AssertionError if any PRD section 9.1 invariant is violated."""
        for t, prop in self.properties.items():
            if prop.owner_id is not None:
                assert t in self.players[prop.owner_id].owned_property_ids, f"tile {t} owner mismatch"
        for p in self.players.values():
            for t in p.owned_property_ids:
                assert self.properties[t].owner_id == p.id, f"{p.id} lists unowned tile {t}"
            assert 0 <= p.position < BOARD_SIZE
        if self.phase == Phase.AUCTION:
            for pid, p in self.players.items():
                assert self.reserved(pid) <= max(0, p.cash), f"{pid} over-reserved"
        for a in self.auctions.values():
            for pid, amt in a.active_bids.items():
                assert pid in a.discoverer_ids and amt >= a.min_bid
        assert set(PROPERTY_IDS) == set(self.properties)


class GameErrorWithEvents(GameError):
    """A rejection that still commits a status change (an offer becoming INVALID)."""

    def __init__(self, code: str, message: str, events: list[Event]):
        super().__init__(code, message)
        self.events = events
