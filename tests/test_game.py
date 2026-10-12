"""Rule-engine unit tests (PRD 11.1). Pure Python, no D-Bus."""

import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import board  # noqa: E402
from game import Game, GameError, Phase, Settings, OfferStatus  # noqa: E402

ROSTER = [("p1", 1001, "alice"), ("p2", 1002, "bob"), ("p3", 1003, "carol"), ("p4", 1004, "dave")]


def new_game(rounds=7, roster=ROSTER):
    g = Game(roster, Settings(rounds=rounds, negotiation_seconds=10, movement_seconds=10,
                              auction_seconds=10))
    for pid, _, _ in roster:
        g.mark_joined(pid)
    g.start_match(0.0)
    return g


def now(g):
    """A time safely inside the current phase."""
    return g.phase_deadline_monotonic - 5


def end_phase(g):
    return g.advance(g.phase_deadline_monotonic)


def give(g, pid, tile):
    g._transfer_property(tile, pid)


def to_movement(g):
    assert g.phase == Phase.NEGOTIATION
    end_phase(g)
    assert g.phase == Phase.MOVEMENT


def move(g, choices):
    to_movement(g)
    for pid, steps in choices.items():
        g.submit_movement(pid, steps, now(g))
    return end_phase(g)


def types(events):
    return [e.type for e in events]


# ------------------------------------------------------------------ board

def test_board_table_and_minimum_bids():
    assert [board.TILES[i].min_bid for i in (1, 4, 7, 10)] == [128, 144, 160, 176]
    assert board.DISTRICTS == {"Blue": (1, 2), "Green": (4, 5), "Purple": (7, 8), "Red": (10, 11)}
    assert board.find_property("tech park") == 10 and board.find_property("11") == 11
    assert board.find_property("Safehouse") is None and board.find_property(3) is None


def test_roster_validation():
    with pytest.raises(ValueError):
        Game(ROSTER[:3])
    with pytest.raises(ValueError):
        Game([("p1", 1, "a"), ("p2", 1, "b"), ("p3", 3, "c"), ("p4", 4, "d")])   # same UID
    Game(ROSTER + [("p5", 1005, "erin"), ("p6", 1006, "frank")])


def test_start_requires_everyone_connected():
    g = Game(ROSTER)
    g.mark_joined("p1")
    with pytest.raises(GameError) as e:
        g.start_match(0)
    assert e.value.code == "NOT_READY" and g.phase == Phase.LOBBY
    for pid, _, _ in ROSTER[1:]:
        g.mark_joined(pid)
    g.start_match(0)
    assert g.phase == Phase.NEGOTIATION and g.round_number == 1
    with pytest.raises(GameError):
        g.start_match(0)


# --------------------------------------------------------------- movement

def test_movement_wraps_and_start_bonus():
    g = new_game()
    g.players["p1"].position = 11
    g.players["p2"].position = 10
    g.players["p3"].position = 0
    move(g, {"p1": 1, "p2": 3, "p3": 3})
    p1, p2, p3 = (g.players[x] for x in ("p1", "p2", "p3"))
    assert (p1.position, p1.cash) == (0, 650)          # lands on START: +50
    assert (p2.position, p2.cash) == (1, 650)          # passes START: +50 (Dockyard discovered)
    assert (p3.position, p3.cash) == (3, 630)          # from START, no bonus; Safehouse +30


def test_special_tiles():
    g = new_game()
    g.players["p1"].position = 5     # -> 6 Checkpoint
    g.players["p2"].position = 6     # -> 9 Supply Drop
    move(g, {"p1": 1, "p2": 3, "p3": 3, "p4": 3})
    assert g.players["p1"].cash == 560
    assert g.players["p2"].cash == 630
    assert g.players["p3"].cash == 630 and g.players["p4"].cash == 630   # Safehouse


def test_default_move_is_one_and_last_valid_choice_wins():
    g = new_game()
    to_movement(g)
    t = now(g)
    g.submit_movement("p1", 3, t)
    g.submit_movement("p1", 2, t)
    with pytest.raises(GameError):
        g.submit_movement("p1", 4, t)          # invalid does not replace
    with pytest.raises(GameError):
        g.submit_movement("p1", True, t)       # bool is not an int here
    with pytest.raises(GameError) as e:
        g.submit_movement("p2", 3, g.phase_deadline_monotonic)   # at/after deadline
    assert e.value.code == "DEADLINE_PASSED"
    events = end_phase(g)
    moves = {m["player"]: m for m in events[0].data["moves"]}
    assert moves["p1"]["steps"] == 2 and not moves["p1"]["defaulted"]
    assert moves["p2"]["steps"] == 1 and moves["p2"]["defaulted"]


def test_movement_only_in_movement_phase():
    g = new_game()
    with pytest.raises(GameError) as e:
        g.submit_movement("p1", 2, now(g))
    assert e.value.code == "WRONG_PHASE"


def test_rent_and_district_doubling():
    g = new_game()
    give(g, "p2", 1)
    g.players["p2"].position = g.players["p4"].position = 8      # park on Supply Drop (+30)
    move(g, {"p1": 1, "p3": 2})                # p1 lands on Dockyard (bob), p3 on Old Town
    assert g.players["p1"].cash == 575 and g.players["p2"].cash == 630 + 25
    # complete Blue for bob -> doubled rent
    g2 = new_game()
    give(g2, "p2", 1)
    give(g2, "p2", 2)
    for pid in ("p2", "p3", "p4"):
        g2.players[pid].position = 8
    move(g2, {"p1": 2})
    assert g2.players["p1"].cash == 550 and g2.players["p2"].cash == 630 + 50
    assert g2.rent_for(2) == 50 and g2.rent_for(1) == 50


def test_no_rent_on_own_property_and_debt_allowed():
    g = new_game()
    give(g, "p1", 1)
    give(g, "p2", 2)
    g.players["p3"].cash = 10
    g.players["p2"].position = g.players["p4"].position = 8
    move(g, {"p1": 1, "p3": 2})
    assert g.players["p1"].cash == 600
    assert g.players["p3"].cash == -15          # debt is allowed for mandatory rent
    assert g.available_cash("p3") == 0


def test_rent_uses_ownership_snapshot_and_multiple_pawns_share_tile():
    g = new_game()
    give(g, "p4", 10)
    for pid in ("p1", "p2", "p3"):
        g.players[pid].position = 7
    move(g, {"p1": 3, "p2": 3, "p3": 3, "p4": 1})
    for pid in ("p1", "p2", "p3"):
        assert g.players[pid].position == 10 and g.players[pid].cash == 560
    assert g.players["p4"].cash == 600 + 120


# --------------------------------------------------------------- auctions

def discover(g, tile=1, who=("p1",)):
    choices = {}
    for pid in who:
        g.players[pid].position = tile - 1
        choices[pid] = 1
    for pid in g.slot_order:
        if pid not in choices:
            g.players[pid].position = 8      # -> Supply Drop with default move
    move(g, choices)
    assert g.phase == Phase.AUCTION
    return g


def test_one_auction_for_shared_discovery_no_discount():
    g = discover(new_game(), 1, ("p1", "p2"))
    assert list(g.auctions) == [1] and g.auctions[1].discoverer_ids == {"p1", "p2"}
    assert g.auctions[1].contested and g.auctions[1].min_bid == 128
    t = now(g)
    g.submit_bid("p2", 1, 200, t)
    g.submit_bid("p1", 1, 190, t)
    events = end_phase(g)
    closed = [e for e in events if e.type == "AuctionClosed"][0].data
    assert closed["winner"] == "p2" and closed["paid"] == 200 and closed["contested"]
    assert g.players["p2"].cash == 600 - 200
    assert g.properties[1].owner_id == "p2"
    assert g.players["p1"].cash == 600 and g.reserved("p1") == 0   # losing reservation released


def test_only_landers_can_bid():
    g = discover(new_game(), 1, ("p1", "p2"))
    with pytest.raises(GameError) as e:
        g.submit_bid("p3", 1, 200, now(g))
    assert e.value.code == "NOT_ELIGIBLE"


def test_single_lander_buys_at_listed_price():
    g = discover(new_game(), 4)
    a = g.auctions[4]
    assert not a.contested and a.min_bid == 180
    assert g.public_snapshot(now(g))["landers"] == {"4": ["p1"]}
    t = now(g)
    with pytest.raises(GameError) as e:
        g.submit_bid("p2", 4, 180, t)
    assert e.value.code == "NOT_ELIGIBLE"
    for amount in (144, 179, 181, 300):
        with pytest.raises(GameError) as e:
            g.submit_bid("p1", 4, amount, t)
        assert e.value.code == "FIXED_PRICE"
    g.submit_bid("p1", 4, 180, t)
    closed = [e for e in end_phase(g) if e.type == "AuctionClosed"][0].data
    assert closed == {"property_id": 4, "name": "Midtown", "winner": "p1",
                      "paid": 180, "contested": False}
    assert g.players["p1"].cash == 600 - 180 and g.properties[4].owner_id == "p1"


def test_bid_minimum_phase_and_cash_checks():
    g = discover(new_game(), 4, ("p1", "p2"))
    t = now(g)
    with pytest.raises(GameError) as e:
        g.submit_bid("p1", 4, 143, t)
    assert e.value.code == "BID_TOO_LOW"
    with pytest.raises(GameError) as e:
        g.submit_bid("p1", 5, 150, t)
    assert e.value.code == "NO_AUCTION"
    with pytest.raises(GameError) as e:
        g.submit_bid("p1", 4, 10_000, t)
    assert e.value.code == "INSUFFICIENT_FUNDS"
    with pytest.raises(GameError) as e:
        g.submit_bid("p1", 4, 150, g.phase_deadline_monotonic)
    assert e.value.code == "DEADLINE_PASSED"
    g.players["p2"].cash = -5
    with pytest.raises(GameError) as e:
        g.submit_bid("p2", 4, 144, t)
    assert e.value.code == "INSUFFICIENT_FUNDS"


def test_single_lander_cannot_buy_without_cash():
    g = discover(new_game(), 10)
    g.players["p1"].cash = 219
    with pytest.raises(GameError) as e:
        g.submit_bid("p1", 10, 220, now(g))
    assert e.value.code == "INSUFFICIENT_FUNDS"
    end_phase(g)
    assert g.properties[10].owner_id is None and g.players["p1"].cash == 219


def test_replacement_and_cancellation_and_reservations():
    g = discover(new_game(), 1, ("p1", "p2"))
    t = now(g)
    g.players["p1"].cash = 300
    g.submit_bid("p1", 1, 300, t)
    assert g.available_cash("p1") == 0
    with pytest.raises(GameError):
        g.submit_bid("p1", 1, 301, t)                  # replacement must also fit
    assert g.auctions[1].active_bids["p1"] == 300     # failed replacement kept old bid
    g.submit_bid("p1", 1, 200, t)                      # lowering is fine
    assert g.available_cash("p1") == 100
    g.cancel_bid("p1", 1, t)
    assert g.available_cash("p1") == 300
    with pytest.raises(GameError):
        g.cancel_bid("p1", 1, t)
    g.check_invariants()


def test_tie_break_uses_rotating_priority():
    for rnd_offset in range(4):
        g = new_game()
        for _ in range(rnd_offset):
            # Advance whole rounds without auctions (everyone stays off unowned tiles).
            for pid in g.slot_order:
                g.players[pid].position = 8
            move(g, {})
        assert g.round_number == rnd_offset + 1
        expected_first = g.slot_order[rnd_offset % 4]
        assert g.player_priority_order[0] == expected_first
        discover(g, 10, tuple(g.slot_order))
        t = now(g)
        for pid in g.slot_order:
            if g.players[pid].cash >= 200:
                g.submit_bid(pid, 10, 200, t)
        bidders = [p for p in g.player_priority_order if p in g.auctions[10].active_bids]
        events = end_phase(g)
        closed = [e for e in events if e.type == "AuctionClosed"][0].data
        assert closed["winner"] == bidders[0]


def test_no_bids_leaves_property_unowned_and_rediscoverable():
    g = discover(new_game(), 7)
    end_phase(g)
    assert g.properties[7].owner_id is None and g.phase == Phase.NEGOTIATION
    discover(g, 7, ("p3",))
    assert 7 in g.auctions


def test_bid_amounts_are_private():
    g = discover(new_game(), 1, ("p1", "p2", "p3"))
    g.submit_bid("p2", 1, 222, now(g))
    assert "222" not in repr(g.public_snapshot(now(g)))
    _, events = g.submit_bid("p3", 1, 333, now(g))
    assert all(e.visibility == "private" and e.recipients == ("p3",) for e in events)
    out = end_phase(g)
    for e in out:
        if e.visibility == "public":
            assert "222" not in repr(e.data)       # losing amount never published


# ----------------------------------------------------------------- trades

def test_trade_lifecycle_and_atomicity():
    g = new_game()
    give(g, "p2", 2)
    give(g, "p1", 1)
    t = now(g)
    offer, ev = g.create_offer("p2", "alice", 2, 230, t)
    assert ev[0].recipients == ("p1", "p2")
    with pytest.raises(GameError) as e:
        g.accept_offer("p3", offer["offer_id"], t)       # wrong recipient
    assert e.value.code == "UNKNOWN_OFFER"
    g.accept_offer("p1", offer["offer_id"], t)
    assert g.properties[2].owner_id == "p1" and g.players["p1"].cash == 370
    assert g.players["p2"].cash == 830 and g.rent_for(1) == 50
    with pytest.raises(GameError) as e:
        g.accept_offer("p1", offer["offer_id"], t)       # cannot settle twice
    assert e.value.code == "OFFER_CLOSED"
    assert g.players["p1"].cash == 370
    g.check_invariants()


def test_trade_validation():
    g = new_game()
    give(g, "p2", 2)
    t = now(g)
    for args, code in [(("p1", "bob", 2, 100), "NOT_OWNER"),
                       (("p2", "bob", 2, 100), "INVALID_ARGUMENT"),
                       (("p2", "alice", 2, 0), "INVALID_ARGUMENT"),
                       (("p2", "alice", 2, -5), "INVALID_ARGUMENT"),
                       (("p2", "alice", 3, 10), "UNKNOWN_PROPERTY"),
                       (("p2", "zed", 2, 10), "UNKNOWN_PLAYER")]:
        with pytest.raises(GameError) as e:
            g.create_offer(*args, t)
        assert e.value.code == code, args
    to_movement(g)
    with pytest.raises(GameError) as e:
        g.create_offer("p2", "alice", 2, 100, now(g))
    assert e.value.code == "WRONG_PHASE"


def test_stale_offers_invalidated_and_expiry():
    g = new_game()
    give(g, "p2", 2)
    t = now(g)
    o1, _ = g.create_offer("p2", "alice", 2, 100, t)
    o2, _ = g.create_offer("p2", "carol", 2, 120, t)
    o3, _ = g.create_offer("p2", "dave", 2, 130, t)
    with pytest.raises(GameError) as e:
        g.create_offer("p2", "dave", 2, 140, t)
    assert e.value.code == "LIMIT_EXCEEDED"
    g.accept_offer("p3", o2["offer_id"], t)
    assert g.offers[o1["offer_id"]].status == OfferStatus.INVALID
    with pytest.raises(GameError):
        g.accept_offer("p1", o1["offer_id"], t)
    assert g.offers[o3["offer_id"]].status == OfferStatus.INVALID
    give(g, "p4", 5)
    o4, _ = g.create_offer("p4", "alice", 5, 50, t)
    end_phase(g)
    assert g.offers[o4["offer_id"]].status == OfferStatus.EXPIRED


def test_cannot_buy_in_debt_or_beyond_cash():
    g = new_game()
    give(g, "p2", 2)
    t = now(g)
    o, _ = g.create_offer("p2", "alice", 2, 601, t)
    with pytest.raises(GameError) as e:
        g.accept_offer("p1", o["offer_id"], t)
    assert e.value.code == "INSUFFICIENT_FUNDS"
    assert g.offers[o["offer_id"]].status == OfferStatus.PENDING   # nothing partially done
    g.players["p1"].cash = -10
    o2, _ = g.create_offer("p2", "alice", 2, 1, t)
    with pytest.raises(GameError):
        g.accept_offer("p1", o2["offer_id"], t)


def test_reject_and_cancel_permissions():
    g = new_game()
    give(g, "p2", 2)
    t = now(g)
    o, _ = g.create_offer("p2", "alice", 2, 100, t)
    with pytest.raises(GameError):
        g.cancel_offer("p1", o["offer_id"], t)       # only seller cancels
    with pytest.raises(GameError):
        g.reject_offer("p2", o["offer_id"], t)       # only buyer rejects
    g.reject_offer("p1", o["offer_id"], t)
    assert g.offers[o["offer_id"]].status == OfferStatus.REJECTED


# ------------------------------------------------------------------- chat

def test_chat_rules():
    g = new_game()
    t = now(g)
    _, ev = g.send_private("p1", "bob", "hi\x1b[31m", t)
    assert ev[0].recipients == ("p2", "p1") and ev[0].data["text"] == "hi[31m"
    _, ev = g.send_group("p1", ["bob", "carol"], "x", t)
    assert set(ev[0].recipients) == {"p1", "p2", "p3"} and ev[0].visibility == "private"
    for bad in (["bob", "bob"], ["alice"], [], ["a"] * 6):
        with pytest.raises(GameError):
            g.send_group("p1", bad, "x", t)
    with pytest.raises(GameError):
        g.send_public("p1", "x" * 301, t)
    to_movement(g)
    with pytest.raises(GameError):
        g.send_public("p1", "hi", now(g))


# ---------------------------------------------------------------- scoring

def test_scoring_and_ties():
    g = new_game(rounds=1)
    give(g, "p1", 1)
    give(g, "p1", 2)                 # Blue complete
    give(g, "p2", 10)
    for pid in g.slot_order:
        g.players[pid].position = 8
    events = move(g, {})
    ended = [e for e in events if e.type == "MatchEnded"][0].data
    scores = {s["player"]: s for s in ended["scores"]}
    # Everyone moved 8 -> 9 Supply Drop (+30)
    assert scores["p1"]["net_worth"] == 630 + 320 + 80
    assert scores["p2"]["net_worth"] == 630 + 220
    assert ended["winners"] == ["p1"] and g.phase == Phase.ENDED


def test_joint_winners():
    g = new_game(rounds=1)
    for pid in g.slot_order:
        g.players[pid].position = 8
    ended = [e for e in move(g, {}) if e.type == "MatchEnded"][0].data
    assert sorted(ended["winners"]) == ["p1", "p2", "p3", "p4"]


def test_tie_broken_by_cash_then_property_count():
    g = new_game(rounds=1)
    give(g, "p1", 1)                    # 160 in property
    g.players["p1"].cash = 440          # nw 600 (+30 later)
    g.players["p2"].cash = 600          # nw 600, more cash
    g.players["p3"].cash = 0
    g.players["p4"].cash = 0
    for pid in g.slot_order:
        g.players[pid].position = 8
    ended = [e for e in move(g, {}) if e.type == "MatchEnded"][0].data
    assert ended["winners"] == ["p2"]


# ---------------------------------------------------------- full simulation

def test_seven_rounds_complete_and_invariants_hold_over_100_random_matches():
    rng = random.Random(1234)
    for match in range(100):
        n = rng.choice([4, 5, 6])
        roster = [(f"p{i}", 2000 + i, f"u{i}") for i in range(1, n + 1)]
        g = new_game(roster=roster)
        total_rounds = 0
        while g.phase != Phase.ENDED:
            t = now(g)
            if g.phase == Phase.NEGOTIATION:
                for pid in g.slot_order:
                    for tile in list(g.players[pid].owned_property_ids):
                        if rng.random() < 0.3:
                            buyer = rng.choice([p for p in g.slot_order if p != pid])
                            try:
                                o, _ = g.create_offer(pid, buyer, tile, rng.randint(1, 400), t)
                                if rng.random() < 0.6:
                                    g.accept_offer(buyer, o["offer_id"], t)
                            except GameError:
                                pass
            elif g.phase == Phase.MOVEMENT:
                for pid in g.slot_order:
                    if rng.random() < 0.9:
                        g.submit_movement(pid, rng.randint(1, 3), t)
            elif g.phase == Phase.AUCTION:
                for _ in range(20):
                    pid = rng.choice(g.slot_order)
                    tile = rng.choice(list(g.auctions))
                    try:
                        if rng.random() < 0.1:
                            g.cancel_bid(pid, tile, t)
                        else:
                            g.submit_bid(pid, tile, rng.randint(100, 500), t)
                    except GameError:
                        pass
                    g.check_invariants()
            g.check_invariants()
            before = g.round_number
            end_phase(g)
            if g.round_number != before or g.phase == Phase.ENDED:
                total_rounds += 1
        assert total_rounds == 7
        assert g.final_scores is not None
        for s in g.final_scores:
            assert s["net_worth"] == g.net_worth(s["player"])


def test_event_order_lets_clients_render_current_state():
    """Phase change precedes AuctionOpened; settled board precedes RoundSettled."""
    g = new_game()
    to_movement(g)
    g.submit_movement("p1", 1, now(g))
    opened = types(end_phase(g))
    assert opened.index("PhaseChanged") < opened.index("AuctionOpened")
    g.submit_bid("p1", 1, 200, now(g))
    settled = end_phase(g)
    names = types(settled)
    board_before = max(i for i, t in enumerate(names[:names.index("RoundSettled")]) if t == "BoardUpdated")
    alice = next(p for p in settled[board_before].data["players"] if p["id"] == "p1")
    assert alice["props"] == [1] and alice["cash"] == 600 - 200
