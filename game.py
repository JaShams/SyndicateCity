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
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

import board
from board import TILES, DISTRICTS, PROPERTY_IDS, BOARD_SIZE

MAX_CHAT_CHARS = 300
MAX_GROUP_RECIPIENTS = 5
MAX_AMOUNT = 1_000_000
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩]")


class Phase(str, enum.Enum):
    LOBBY = "LOBBY"
    NEGOTIATION = "NEGOTIATION"
    MOVEMENT = "MOVEMENT"
    AUCTION = "AUCTION"
    SETTLEMENT = "SETTLEMENT"
    ENDED = "ENDED"


class OfferStatus(str, enum.Enum):
    PENDING = "PENDING"
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


@dataclass
class Settings:
    rounds: int = 7
    negotiation_seconds: float = 45.0
    movement_seconds: float = 15.0
    auction_seconds: float = 25.0
    starting_cash: int = 600
    min_players: int = 4
    max_players: int = 6
    max_pending_offers_per_seller: int = 3
    max_pending_offers_per_buyer: int = 6


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
    id: str
    seller_id: str
    buyer_id: str
    property_id: int
    price: int
    round_created: int
    status: OfferStatus = OfferStatus.PENDING

    def to_dict(self) -> dict:
        return {"offer_id": self.id, "seller": self.seller_id, "buyer": self.buyer_id,
                "property_id": self.property_id, "price": self.price,
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
        }

    def private_view(self, pid: str) -> dict:
        """Secrets visible to ``pid`` only: own offers, own bids, own movement choice."""
        offers = [o.to_dict() for o in self.offers.values()
                  if o.status == OfferStatus.PENDING and pid in (o.seller_id, o.buyer_id)]
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
        self.phase = Phase.NEGOTIATION
        self.phase_deadline_monotonic = now + self.settings.negotiation_seconds
        return [self._phase_event(now), self._board_event(now)]

    def advance(self, now: float) -> list[Event]:
        """Close the current phase if its deadline has passed. Idempotent otherwise."""
        if self.phase in (Phase.LOBBY, Phase.ENDED, Phase.SETTLEMENT):
            return []
        if now < self.phase_deadline_monotonic:
            return []
        if self.phase == Phase.NEGOTIATION:
            events = self._expire_offers()
            self.phase = Phase.MOVEMENT
            self.phase_deadline_monotonic = now + self.settings.movement_seconds
            events.append(self._phase_event(now))
            return events
        if self.phase == Phase.MOVEMENT:
            events = self._resolve_movement(now)
            if self.auctions:
                self.phase = Phase.AUCTION
                self.phase_deadline_monotonic = now + self.settings.auction_seconds
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
            if o.status == OfferStatus.PENDING:
                o.status = OfferStatus.EXPIRED
                events.append(Event.private("TradeUpdated", o.to_dict(), (o.seller_id, o.buyer_id)))
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
        self.phase = Phase.SETTLEMENT
        self.phase_deadline_monotonic = now
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

    def create_offer(self, pid: str, buyer_ref, property_id, price, now: float):
        self._require_player(pid)
        self._require_phase(Phase.NEGOTIATION, now)
        prop = self._require_property(property_id)
        buyer = self.resolve_player(buyer_ref)
        price = self._require_int(price, "price", 1)
        if prop.owner_id != pid:
            raise GameError("NOT_OWNER", "you can only offer properties you own")
        if buyer == pid:
            raise GameError("INVALID_ARGUMENT", "cannot sell to yourself")
        pending = [o for o in self.offers.values() if o.status == OfferStatus.PENDING]
        if sum(o.seller_id == pid for o in pending) >= self.settings.max_pending_offers_per_seller:
            raise GameError("LIMIT_EXCEEDED", "too many pending offers from you")
        if sum(o.buyer_id == buyer for o in pending) >= self.settings.max_pending_offers_per_buyer:
            raise GameError("LIMIT_EXCEEDED", "recipient has too many pending offers")
        self._offer_counter += 1
        offer = TradeOffer(f"o{self._offer_counter}", pid, buyer, prop.tile_id, price,
                           self.round_number)
        self.offers[offer.id] = offer
        return offer.to_dict(), [Event.private("TradeOffered", offer.to_dict(), (buyer, pid))]

    def _require_offer(self, offer_id) -> TradeOffer:
        if not isinstance(offer_id, str) or offer_id not in self.offers:
            raise GameError("UNKNOWN_OFFER", "no such offer")
        return self.offers[offer_id]

    def accept_offer(self, pid: str, offer_id, now: float):
        self._require_player(pid)
        offer = self._require_offer(offer_id)
        if offer.buyer_id != pid:
            # Do not reveal anything about offers addressed to other players.
            raise GameError("UNKNOWN_OFFER", "no such offer")
        self._require_phase(Phase.NEGOTIATION, now)
        if offer.status != OfferStatus.PENDING:
            raise GameError("OFFER_CLOSED", f"offer is {offer.status.value}")
        prop = self.properties[offer.property_id]
        events = []
        if prop.owner_id != offer.seller_id:
            offer.status = OfferStatus.INVALID
            events.append(Event.private("TradeUpdated", offer.to_dict(),
                                        (offer.seller_id, offer.buyer_id)))
            raise GameErrorWithEvents("OFFER_INVALID", "seller no longer owns the property", events)
        if self.available_cash(pid) < offer.price:
            raise GameError("INSUFFICIENT_FUNDS",
                            f"need {offer.price} credits, available {self.available_cash(pid)}")
        # Atomic settlement: all checks passed, now commit everything.
        self.players[pid].cash -= offer.price
        self.players[offer.seller_id].cash += offer.price
        self._transfer_property(offer.property_id, pid)
        offer.status = OfferStatus.ACCEPTED
        events.append(Event.private("TradeUpdated", offer.to_dict(),
                                    (offer.seller_id, offer.buyer_id)))
        for other in self.offers.values():
            if other.status == OfferStatus.PENDING and other.property_id == offer.property_id:
                other.status = OfferStatus.INVALID
                events.append(Event.private("TradeUpdated", other.to_dict(),
                                            (other.seller_id, other.buyer_id)))
        events.append(Event("Notice", {"text": f"{self.name_of(offer.seller_id)} sold "
                                               f"{TILES[offer.property_id].name} to "
                                               f"{self.name_of(pid)}"}))
        events.append(self._board_event(now))
        return offer.to_dict(), events

    def reject_offer(self, pid: str, offer_id, now: float):
        self._require_player(pid)
        offer = self._require_offer(offer_id)
        if offer.buyer_id != pid:
            raise GameError("UNKNOWN_OFFER", "no such offer")
        self._require_phase(Phase.NEGOTIATION, now)
        if offer.status != OfferStatus.PENDING:
            raise GameError("OFFER_CLOSED", f"offer is {offer.status.value}")
        offer.status = OfferStatus.REJECTED
        return offer.to_dict(), [Event.private("TradeUpdated", offer.to_dict(),
                                               (offer.seller_id, offer.buyer_id))]

    def cancel_offer(self, pid: str, offer_id, now: float):
        self._require_player(pid)
        offer = self._require_offer(offer_id)
        if offer.seller_id != pid:
            raise GameError("UNKNOWN_OFFER", "no such offer")
        self._require_phase(Phase.NEGOTIATION, now)
        if offer.status != OfferStatus.PENDING:
            raise GameError("OFFER_CLOSED", f"offer is {offer.status.value}")
        offer.status = OfferStatus.CANCELLED
        return offer.to_dict(), [Event.private("TradeUpdated", offer.to_dict(),
                                               (offer.seller_id, offer.buyer_id))]

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
