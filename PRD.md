# Syndicate City — Product Requirements Document (PRD)

**Version:** 1.0  
**Status:** Implementation-ready baseline  
**Target:** Ubuntu 26.04, local multi-user Linux environment  
**Game type:** Text-based, simultaneous-round property-control and negotiation  
**IPC primitive:** D-Bus over a dedicated local message bus  
**Suggested stack:** Python 3, `asyncio`, `dbus-next`, `dbus-daemon`  
**Deliverable:** One `.tgz` archive containing original source code, README, setup/test scripts, one-file communications library, and a PDF report

---

## 1. Executive summary

Syndicate City is a compact, multiplayer strategy game inspired by Monopoly but centered on **simultaneous decisions**, **sealed auctions**, **private negotiations**, and **risk management**. Four to six players compete for ownership of eight properties on a twelve-tile circular board. Each player runs a distinct Linux process under a distinct Linux user account. A separate coordinator process runs as a dedicated service user and is the sole authority for all game state.

Players negotiate and trade properties, secretly choose movement distances of one to three spaces, and compete in sealed-bid auctions for previously unowned properties they discover. Completed two-property districts double rent, while limited cash and incoming rent create pressure to buy selectively. The game ends after seven rounds; the highest net worth wins.

All participant/coordinator communication traverses a **dedicated D-Bus bus**. An original, **single-source-file IPC library (`ipc.py`)** abstracts D-Bus method calls, client event reception, public announcements, one-to-one messages, and group fan-out. The project deliberately demonstrates identity verification via D-Bus sender credentials, permissions and policy, broadcast versus addressed signals, asynchronous events, atomic state updates, and behavior when clients crash or send malicious requests.

### Product principles

1. **Game first, OS purpose explicit:** a complete, enjoyable game rather than an IPC demo with superficial gameplay.
2. **Small, frozen rules:** no mortgages, houses, random cards, hidden inventories, complex alliances, or networked D-Bus for the MVP.
3. **Untrusted clients:** players may modify their clients or invoke D-Bus methods directly; the coordinator enforces all rules.
4. **Visible concurrency:** independent processes can negotiate and submit decisions simultaneously.
5. **Measurable security:** classmates can test impersonation, information disclosure, forged signals, overspending, invalid messages, and disconnections.
6. **Reproducibility:** a README and scripts set up every required Linux account and run a full game on Ubuntu.

## 2. Goals and non-goals

### 2.1 Goals

- Implement a fully playable text-based local game for **4–6 distinct players** (configurable; exactly four players sufficient for the primary demo).
- Demonstrate actual separate Linux users, not merely simulated player IDs.
- Provide one-to-one, selected-group, and public message delivery via a **single-file** communications library.
- Run timed simultaneous decision phases with server-owned deadlines.
- Support property ownership, rent, negotiations, formal trades, sealed auctions, and fixed-round scoring.
- Prevent unauthorized game operations, overspending, duplicate settlement, and private-message leakage under the defined threat model.
- Support client disconnect/rejoin without corrupting a live match.
- Include repeatable integration and adversarial tests useful for peer review and the report.

### 2.2 Non-goals for v1

- Internet/WAN multiplayer, TCP relay, or remote D-Bus connectivity.
- Graphics, web UI, voice chat, database, or persistent accounts.
- Houses, hotels, mortgages, bankruptcy elimination, interest, loans, player-controlled random events, or complex taxes.
- Server restart recovery, durable game state, cross-host authentication, general-purpose group membership, or a transferable IPC framework.
- Cryptographic commitments, genuinely trustless sealed bidding, or protection from the privileged machine administrator.
- Deliberately vulnerable production code. Any intentionally weak demonstration must be isolated from the normal game.

## 3. Users and roles

| Role | Process and Linux identity | Responsibilities |
|---|---|---|
| Game coordinator | `server.py` as service user `syndicate-server` | Own authoritative game state; validate commands; run phase clock; send messages; log outcomes |
| Player | One `client.py` process per distinct Linux user (`syndicate-p1` … `syndicate-p6`) | View board and balances; negotiate; select movement; bid; manage trade offers |
| Game bus | Dedicated `dbus-daemon` process, with restrictive policy | Authenticate connections and route typed D-Bus messages |
| Operator/tester | Instructor, author, or classmate with permitted test access | Set up local users; launch game; run adversarial tests; capture evidence |

**Identity rule:** In the MVP, each player slot is provisioned to one *specific Linux UID* at setup. A display name is not an authentication credential. The coordinator resolves the caller's D-Bus unique sender name to an authenticated UID and maps it to a slot. The bus must not permit ordinary players to own the coordinator's well-known name.

**Distinct-user verification:** the test harness must assert that all participating players have different numeric UIDs. Multiple terminals under a single UID do not satisfy this requirement.

## 4. Full game rules — normative

The rules in this section take precedence over illustrative UI screenshots and examples.

### 4.1 Match settings

| Setting | Value |
|---|---|
| Players | 4–6; default 4 |
| Rounds | Exactly 7 |
| Starting position | Tile 0 (START) for everyone |
| Starting cash | 600 credits each |
| Starting properties | None; all initially unowned |
| Board size | 12 clockwise positions, indexed 0–11 |
| Movement | Exactly one secret choice of 1, 2, or 3 positions per round; editable until deadline |
| Negotiation | 45 seconds per round |
| Movement submission | 15 seconds per round |
| Sealed auction | 25 seconds per round if properties were discovered |
| Winner | Highest final net worth after round 7 |

Durations are configurable through development/test settings; these are the production defaults.

### 4.2 Board

| Index | Tile | Category | District | Face value | Base rent |
|---:|---|---|---|---:|---:|
| 0 | START | Start | — | — | — |
| 1 | Dockyard | Property | Blue | 160 | 25 |
| 2 | Old Town | Property | Blue | 160 | 25 |
| 3 | Safehouse | Special | — | — | — |
| 4 | Midtown | Property | Green | 180 | 30 |
| 5 | Financial | Property | Green | 180 | 30 |
| 6 | Checkpoint | Special | — | — | — |
| 7 | Industrial | Property | Purple | 200 | 35 |
| 8 | Warehouse | Property | Purple | 200 | 35 |
| 9 | Supply Drop | Special | — | — | — |
| 10 | Tech Park | Property | Red | 220 | 40 |
| 11 | Uptown | Property | Red | 220 | 40 |

**Property rules:**

- Unowned property: landing discovers it; it goes to the round's auction. Landing does **not** grant automatic purchase or ownership.
- Owned by another player: landing player pays rent to that owner.
- Owned by oneself: no rent.
- Complete district: owning both properties in the same district doubles their respective base rents; no further upgrades.
- Property ownership is public at all times. Negotiations, movement choices before reveal, and auction bids before settlement are private.

**Special tile rules:**

- Safehouse (3): add 30 credits to the landing player's cash.
- Checkpoint (6): deduct 40 credits.
- Supply Drop (9): add 30 credits.
- START (0): earn 50 credits when a move crosses from tile 11 to 0, whether the player finishes on 0 or continues past it. Starting at START does not award a bonus. As max movement is 3, at most one START bonus occurs per move.

### 4.3 Money, debt, and fairness

- Cash is a signed integer; **mandatory rent and Checkpoint charges may create negative cash**. Negative cash is debt, not elimination.
- A player with negative cash can still move, receive chat, receive START/special rewards, collect rent, and sell owned property.
- A voluntary purchase or bid is valid only if the buyer has sufficient **available cash**; players cannot intentionally borrow to trade or bid.
- Total money is updated only by authoritative coordinator operations. Rent and private trades transfer cash between players without creation; START and positive special-tile effects create cash; Checkpoint removes cash.
- There is **no interest, bankruptcy, mortgage, forced liquidation, or elimination** in the MVP.
- No negative property counts or fractional credits; all numeric game amounts are nonnegative integers except balances after mandatory costs.

**Rationale:** This reduces complexity, while bidding too aggressively still creates exposure to mandatory rent and poor end-game liquidity.

### 4.4 Round state machine

A match starts after all configured, distinct-UID players have joined and the operator issues `start`. Every round follows this sequence:

1. **NEGOTIATION — 45 s.** Public/private/group chat is enabled. The current owner of a property can send a formal sale offer to another player, who may accept/reject it. Accepted trades settle immediately and atomically. Unaccepted offers expire when the phase ends.
2. **MOVEMENT — 15 s.** Each player submits one movement choice (1–3), optionally replacing it during the phase. Choices remain private. At deadline, the coordinator chooses default `1` for nonresponders, computes all new positions using a consistent state snapshot, then resolves events deterministically and broadcasts the reveal.
3. **AUCTION — 25 s when applicable.** Every *distinct* unowned property landed on in the movement phase is offered in a sealed-bid auction; multiple auctions run in the same window. Any player may bid on any of them. If none was discovered, skip directly to settlement.
4. **SETTLEMENT.** Close auctions; choose winners; debit credits; transfer properties; release losing reservations; publish results and board snapshot; increment round or end match.

Chat is available during negotiation and auction phases; formal property trades occur **only** in negotiation. Movement selection occurs **only** in movement. Auction bids occur **only** in auction. The coordinator rejects game-changing commands issued in the wrong phase.

**Phase start/stop:** Coordinator uses `time.monotonic()` to maintain deadlines; the `PhaseChanged` announcement is informative. The server's phase and clock control acceptance, not a client's local clock. Late calls are rejected even if clients missed the announcement.

### 4.5 Movement resolution and simultaneous actions

- The coordinator records the last valid movement submitted before the movement deadline. Invalid choices do not replace a previously valid choice.
- At deadline, use the ownership snapshot **after negotiation and before movement** for all rent checks.
- Compute every new position simultaneously, then settle in stable player-slot order for reproducible logs. This order cannot affect rent ownership, since properties are not traded during resolution.
- A player landing on an occupied square **does not** conflict with other pawns; multiple pawns may occupy a tile.
- Two or more players landing on the same unowned property generate only **one** auction for that property. All who landed on it count as discoverers.
- Movement decisions are hidden until all are committed or timed out. The result reveal is public.

### 4.6 Sealed auction rules

- Minimum acceptable bid is `ceil(0.8 × face_value)`: Blue 128, Green 144, Purple 160, Red 176.
- Bids are integer credits, must meet the minimum, and must be backed by the bidder's **available cash**.
- A player may submit **at most one active bid per property**. Submitting a later bid for that property replaces the previous bid if the replacement is valid. For replacement, the coordinator temporarily excludes the old reservation when testing the new bid; if the new bid fails, the old bid and reservation remain unchanged. A player may cancel an active bid before the deadline.
- A player's available cash is `max(0, cash - sum(active_bid_reservations))`; reservations prevent committing the same cash to multiple auctions.
- Every accepted bid reserves its **full bid amount**, even if the bidder qualifies for a discovery discount.
- Bids remain private to the bidder and coordinator until the auction closes. The result announcement reveals the winning player and paid price, **not all losing bid amounts**.
- Highest valid bid wins. **Tie-breaking:** use a rotating fixed player priority (`round_index modulo player_count` as first priority, then cyclic slot order). This ordering is announced at the start of the round. Packet timing does not break ties.
- If the winner physically landed on the property during this round's movement, subtract a **15-credit discoverer discount** from the amount debited. Otherwise debit the full winning bid. Discount never yields a negative purchase price.
- All losing reservations are released. A property with zero valid bids stays unowned and can be discovered again in a later round.
- Multiple auctions settle in ascending tile ID order. Since each player's full bid amounts were already jointly reserved, multiple wins cannot create voluntary overspending.

**Deadline semantics:** A bid counts only if the coordinator has received and validated the complete D-Bus method call while `time.monotonic() < auction_deadline`. There is no claim that two different remote processes experience identical scheduling delays; the documented server-side rule is authoritative.

### 4.7 Formal property trades

- A trade is **one property for a positive integer credit amount**, proposed by its **current owner** to one recipient. No swaps, bundled properties, or credit-only gifts in v1.
- Formal trade offer fields: `offer_id`, `seller_player_id`, `buyer_player_id`, `property_id`, `price`, `created_round`, `status`.
- The coordinator validates ownership, recipient, price > 0, phase, and offer limits when the offer is created.
- The recipient can accept or reject the offer during the **same negotiation phase**. Offers expire automatically at the phase deadline. Seller can cancel while pending.
- At acceptance, coordinator **revalidates** ownership and buyer's available cash and settles owner/cash changes atomically. Existing offers involving a property that has since been sold become invalid.
- Multiple acceptances, duplicate requests, cancelled/expired offers, or acceptance by a different player must never settle twice.
- An offer does **not** lock the property or reserve buyer cash until accepted. Failure at acceptance returns a meaningful error rather than partially completing a sale.
- Both counterparties receive a directed confirmation. Public ownership changes are reflected in the subsequent board snapshot; no private chat content is published.

### 4.8 Scoring and win condition

At completion of round 7:

`Net worth = final cash + sum(face values of properties owned) + 80 × completed districts`

Cash may be negative. For ties, compare **cash**; if cash also ties, compare **property count**; if still tied, declare joint winners.

No interim winner and no early victory condition. Disconnects do not eliminate players; they continue as default-move participants until they reconnect or the seven rounds finish.

### 4.9 Example round (illustrative)

1. Alice owns Dockyard; Bob owns Old Town. Alice privately asks Bob to sell Old Town.
2. Bob sends a formal offer of 230 credits; Alice accepts. Alice now owns both Blue properties, so Blue rent doubles.
3. Carol and Dave privately agree not to compete for Tech Park; these promises are not enforced.
4. Everyone secretly submits a movement value. Alice lands on Carol's property and pays rent; Dave discovers Tech Park.
5. An auction opens for Tech Park. Carol breaks her promise and outbids Dave.
6. The coordinator settles the auction and broadcasts the outcome. Players see the new public ownership and balances, but not Carol's private negotiations.

## 5. Experience and terminal UI

### 5.1 CLI principles

- Text-only, readable in standard Linux terminal, no graphical requirements.
- Prompt remains usable while asynchronous notifications arrive (an input task and a D-Bus event task cooperate).
- Every visible event is tagged **SERVER / PUBLIC / GROUP / PRIVATE / TRADE / AUCTION / ERROR** with round/phase and sender when relevant.
- Server-originated events must be distinguishable from player-written text; the client verifies coordinator sender identity.
- Show next deadline and current phase. Lack of a UI timer must not affect the server clock.
- Provide `help`, `board`, `status`, `players`, and `quit` commands.

### 5.2 Suggested commands

```text
/help
/board
/status
/players
/say <message>                     # public chat
/tell <player> <message>           # private, one-to-one
/group <player1,player2> <message> # directed copies to selected recipients
/offer <buyer> <property> <price>  # only current owner; negotiation phase
/offers                            # incoming/outgoing pending offers
/accept <offer_id>
/reject <offer_id>
/canceloffer <offer_id>
/move <1|2|3>                      # movement phase
/bid <property> <amount>          # auction phase
/cancelbid <property>
/quit
```

Unknown commands, invalid arguments, oversized text, wrong-phase actions, and unauthenticated attempts return clear errors rather than terminating the process.

### 5.3 Information visibility

| Information | Visibility |
|---|---|
| Positions, ownership, board, district sets | Public |
| Player cash balances | Public in MVP |
| Published round phase and auction properties | Public |
| Movement choices before reveal | Player + coordinator only |
| Active bid amounts | Bidder + coordinator only |
| Formal offers | Seller, buyer + coordinator only |
| Private/group chat content | Intended recipients + coordinator only |
| Auction winner and paid price after close | Public |
| Server audit log | Coordinator/operator only |

**Privacy statement:** The coordinator is a trusted authority and can see all bids/messages. A privileged host administrator can also bypass normal process isolation. “Private” means hidden from other unprivileged player processes, not cryptographically hidden from the coordinator.

## 6. D-Bus architecture

### 6.1 Deployment topology

- Dedicated **local** `dbus-daemon` launched by setup/run scripts, not an ordinary user's session bus and **not** the machine's primary system bus.
- Transport: local Unix D-Bus address, authenticated using `EXTERNAL`, with dedicated runtime directory and restricted filesystem permissions.
- Coordinator connects under `syndicate-server` and owns the well-known name **`org.syndicate.City`**.
- Object path: **`/org/syndicate/City`**.
- Interface: **`org.syndicate.City1`** (protocol version 1).
- Each player connects as its distinct UID and gets a bus-assigned **unique name** (for example `:1.17`); the server binds that name to its verified UID and player slot.
- The game bus is isolated from normal desktop session buses. The security policy explicitly controls who can connect, acquire names, send messages, and use monitoring capabilities.
- Runtime directory and bus endpoint should be provisioned by the setup script with access for the intended users; fail closed if ownership/policy is wrong.

### 6.2 Identity and joining

**Roster:** Setup provides a map of `player_slot -> Linux UID`. At most one live D-Bus connection controls each slot. The display name is fixed in the roster or selected at join subject to validation.

**Join flow:**

1. Client establishes a connection to the dedicated bus.
2. Client calls `Join` on `org.syndicate.City`, optionally suggesting a display label.
3. Server reads the D-Bus message's authenticated unique sender and resolves that bus connection to its Unix UID using `org.freedesktop.DBus.GetConnectionUnixUser` (or credentials method).
4. Server maps UID to roster slot. Reject unknown UIDs, duplicate active sessions, or joins after match start by unrecognized identities.
5. Server binds `{unique_name, uid, player_id}` and returns player ID and a current snapshot.
6. On D-Bus `NameOwnerChanged`/disconnect for a player's **unique name**, mark them disconnected and clear the active session binding. A rejoining process with the same registered UID can reclaim only its own disconnected slot.

**Critical:** Treat sender unique name as authoritative only as supplied by the bus on a received method call, not as a caller-provided payload. Authorization occurs before any state-changing action. Recheck the binding before forwarding private events.

### 6.3 Single-file communications library

All application D-Bus access belongs in **`ipc.py`**. Both coordinator and client import it. The game logic should never need to create raw `dbus_next.Message` objects.

Proposed high-level contract (illustrative; names may be refined in code):

```python
# Client side
client = await GameIPC.connect_client(bus_address)
identity = await client.join(display_name)
reply = await client.request("SubmitMovement", {"steps": 2}, request_id="...")
await client.send_private("bob", "Let's trade")
await client.send_group(["bob", "carol"], "Coordinate bids")
await client.send_public("I'm interested in Tech Park")
async for event in client.events():
    ...

# Coordinator side
server = await GameIPC.start_server(bus_address, roster)
server.on_request(handler)       # handler receives trusted authenticated player ID
await server.send_to("alice", event)
await server.multicast(["bob", "carol"], event)
await server.broadcast(event)
await server.close()
```

The IPC library is responsible for:

- Connect/disconnect, D-Bus bus address, well-known name ownership, message handlers.
- Authentication binding of D-Bus unique names to Linux UIDs/player slots.
- Typed request/reply handling with strict message and protocol-version validation.
- Event routing for single recipient, selected recipients, and public broadcasts.
- Safe size limits, timeouts, error propagation, and event queues.
- Sender verification for privileged/public coordinator events.
- A clearly documented API in README and report.

It is **not** responsible for game rules, bid economics, property ownership, or movement logic.

**Library choice:** `dbus-next` with `asyncio`. Use its low-level message API where sender headers or destination-addressed signals are needed; avoid mixing low-level and high-level bus handlers on the same connection without explicit testing. The required one-file library can still wrap the installed `dbus-next` dependency.

### 6.4 Public, private, and group delivery

| Communication | D-Bus mechanism | Privacy and semantics |
|---|---|---|
| Player action → coordinator | Method call to named coordinator | Authenticated unique sender; standard reply/error |
| Coordinator → all players | **Broadcast signal** without destination | Public data only; players subscribe using matching rules |
| Coordinator → one player | **Directed/unicast signal** with destination = player's unique name | Only intended recipient under restrictive policy |
| Coordinator → selected group | Fan-out of individual directed messages | Avoid using ordinary broadcast signals for private data |
| Player → player/group chat | Method call to coordinator, then coordinator-authorized forward | Caller cannot directly impersonate a sender in displayed chat |

D-Bus broadcast signals are not private. A client-side filter on a `group_id` is **not** a confidentiality boundary. The bus configuration must prohibit ordinary players from receiving others' unicast messages or becoming monitors.

### 6.5 Proposed protocol interface

For ease of implementation and future compatibility, use **one D-Bus method with a versioned JSON payload**, carried as a D-Bus string. This is a conscious simplification: D-Bus supplies typed transport-level `s`, while the application validates the nested JSON schema. It avoids an explosion of custom D-Bus method signatures without hiding sender identity.

**Method:** `Request(s payload) -> s response` on `org.syndicate.City1`.

**Client request example:**

```json
{
  "v": 1,
  "id": "req-00042",
  "action": "SubmitBid",
  "payload": {"property_id": 10, "amount": 240}
}
```

**Server reply example:**

```json
{
  "v": 1,
  "id": "req-00042",
  "ok": true,
  "result": {"property_id": 10, "reserved": 240}
}
```

**Event:** D-Bus `GameEvent(s json_event)`; broadcast only for public events, destination-addressed for private events. Example payload:

```json
{
  "v": 1,
  "seq": 117,
  "type": "AuctionClosed",
  "visibility": "public",
  "round": 3,
  "data": {"property_id": 10, "winner": "bob", "paid": 240}
}
```

**Request contract requirements:**

- `v` is required and equals `1`; reject unknown versions.
- `id` is an opaque request identifier, length 1–64, unique per player for the match; retries of the same ID return the recorded response, not a second mutation. Retain request IDs and response outcomes for the entire short-lived match (cap at 5,000 distinct IDs per player; reject further requests if exceeded); reject a reused ID with different payloads. This guarantees match-lifetime deduplication rather than relying on a short rolling cache.
- `action` is a known allowlisted action; `payload` must match that action's schema exactly, with no silently accepted unknown privileged fields.
- JSON input is bounded to **4096 UTF-8 bytes per request**; chat message text ≤ 300 characters; group recipients ≤ 5 and unique, excluding sender.
- `Request` responses are bounded to 4096 bytes; large status/board snapshots may be returned as compact JSON or split into a separate bounded snapshot call only if needed.
- An `Event` has monotonically increasing **coordinator sequence number** and protocol version. Event sequence helps notice missed public updates; it does not create durability or cryptographic integrity.
- Untrusted text is escaped/sanitized for terminal output (no control-character escape injection).
- Client IDs, balance amounts, ownership, and role labels included in request payloads are **never** trusted as authority.

**Allowed actions:** `Join`, `GetSnapshot`, `SendPublic`, `SendPrivate`, `SendGroup`, `CreateTradeOffer`, `AcceptTradeOffer`, `RejectTradeOffer`, `CancelTradeOffer`, `SubmitMovement`, `SubmitBid`, `CancelBid`, `Leave`. `StartMatch` is operator-only through the coordinator process's local console command (`start` on its stdin), not callable by ordinary players via D-Bus.

**Event types:** `PlayerJoined`, `PlayerDisconnected`, `PlayerRejoined`, `MatchStarted`, `PhaseChanged`, `ChatMessage`, `TradeOffered`, `TradeUpdated`, `MovementRevealed`, `AuctionOpened`, `BidStatus` (private), `AuctionClosed`, `BoardUpdated`, `MatchEnded`, `Notice`.

### 6.6 Coordinator concurrency model

- Run an **`asyncio` event loop** in `server.py` handling independent method calls and timers.
- Authoritative state mutations use **one serialized transaction path**. Methods may be processed concurrently on the I/O side, but no two updates may interleave between validation and commit.
- Prefer a single lock around the short, synchronous validation-and-mutation section, or a single actor-style command queue. Do not hold a lock while waiting for D-Bus replies, network I/O, or user input.
- The coordinator's game state is the source of truth. Events are emitted **after successful commit**.
- Simultaneous movement means *concealed decisions collected within one phase*, not simultaneous unsynchronized mutation of property ownership.
- Action limits: default max **10 accepted requests/second per player** with burst 20 (chat max 5/sec), and reject/slow excessive calls without starving other players.
- Bound outbound event queues; if a client is slow or cannot receive directed messages, report/disconnect it while keeping the match alive.

### 6.7 Startup, operation, and teardown

1. Administrator provisions the game-specific group, service account, and player UIDs. Never reuse real personal accounts without explicit operator choice.
2. Setup creates the dedicated runtime directory with secure ownership and mode, generates the D-Bus policy/config, and validates required tools.
3. A launch script starts the dedicated bus; saves its address in a runtime file accessible to allowed clients; starts the coordinator as `syndicate-server`.
4. Clients run as separate Linux users and connect to the bus address; operator verifies distinct UIDs.
5. Coordinator announces ready state and starts match when required players are present and the operator types `start` in the coordinator's terminal. A `start` attempt with fewer than the configured number of distinct joined UIDs fails without changing state.
6. On graceful shutdown, clients and coordinator close bus connections; setup/stop scripts clean up owned runtime files and bus process, never global system/session services.

**Operational note:** The exact `dbus-daemon` policy syntax, endpoint ownership, and cross-user connection permissions must be tested on target Ubuntu 26.04. Do not assume normal session-bus defaults provide confidentiality between users.

## 7. Security and threat model

### 7.1 Threats in scope

- Player operates an untrusted Linux process under their assigned, unprivileged UID.
- Player may modify the game client, use `busctl`/custom D-Bus scripts, send malformed protocol requests, race commands, intentionally disconnect, or flood messages.
- Player may lie socially, form alliances, and betray agreements; those behaviors are **allowed gameplay**.
- Attacks may target the dedicated game bus or its game service, but testing must be conducted in the isolated game environment.

### 7.2 Out of scope

- Root-level adversary, compromised bus daemon, compromised coordinator UID, kernel vulnerabilities, or physical console access to other users' terminals.
- Resource starvation by a malicious administrator with control over all processes on the host.
- Perfect privacy against a privileged user or coordinator.

### 7.3 Required controls

1. **Caller verification:** Every mutating request derives player identity from D-Bus sender credentials; never trust submitted `player_id`.
2. **Bus name ownership:** Only the service UID can own `org.syndicate.City`; abort startup if it cannot be acquired exclusively.
3. **Bus access policy:** Admit only configured game accounts and required service processes; deny unintended service ownership, monitoring/eavesdropping, and arbitrary sends to other users' service names as applicable.
4. **Signal provenance:** Clients accept game-authoritative events only from the verified coordinator unique name; ignore player-generated lookalike signals.
5. **Confidentiality:** Private/group chat, offers, and sealed bids never use broadcast payloads; no secret data in public snapshots or terminal logs viewable by other player UIDs.
6. **Authorization:** Caller must own property for sale, be correct offer recipient for acceptance, have sufficient credits to buy, be in the right phase, and have joined the game.
7. **Atomicity:** Validate+commit trades, bids, rentals, and awards as one logical state transition.
8. **Idempotency:** Reused request ID with identical content cannot trigger repeated effects; different payload with reused ID is invalid.
9. **Rate and size limits:** Prevent one player from exhausting coordinator resources through excessive calls, oversized JSON, or chat floods.
10. **Disconnect safety:** Disconnect does not leave partial trades, unreleased finished-auction reservations, or stale authorizations.
11. **Log protection:** Server event/audit logs readable by coordinator and operator, not other player accounts; redact secrets from ordinary player-facing logs.

### 7.4 Peer-review security challenges

| Challenge | Attacker tries to... | Secure expected behavior |
|---|---|---|
| Phantom Auction | Emit a forged `AuctionOpened` signal | Other players ignore it; only coordinator signal accepted |
| Syndicate Spy | Subscribe to an unrelated group's private messages or bids | No private payload delivered to attacker |
| Counterfeit Coordinator | Own `org.syndicate.City` as a player | Bus rejects name ownership |
| Identity Swap | Put Alice's ID in Bob's request | Server authenticates Bob; unauthorized action rejected |
| Double Buyer | Concurrently bid beyond available credits | Aggregate reservations prevent overspending |
| Replay | Resubmit `AcceptTradeOffer` with same request ID | No duplicate settlement |
| Late Bid | Submit just after auction deadline | Rejected; published rule respected |
| Message Flood | Saturate bus/service with chat or status calls | Rate-limited; other clients continue |
| Crash During Auction | Terminate client process | Server remains healthy; existing accepted bid remains valid; player may reconnect |
| Malformed Payload | Submit bad types, oversized strings, wrong protocol version | Structured error; no state corruption |

**Do not introduce intentional production vulnerabilities solely to let classmates succeed.** A failed exploit with a well-understood explanation is valuable feedback.

## 8. Reliability, disconnects, and edge cases

### 8.1 Disconnect/reconnect

- Detect losing a D-Bus unique-name connection via bus ownership change or failed delivery.
- Mark player disconnected; match continues on timer. During movement timeout default to one space. During auction, an **already accepted valid bid remains active**; disconnected player cannot edit it until they rejoin. Pending unaccepted trade offers still expire at phase end.
- A disconnected account may reconnect from a fresh client process under the **same Linux UID**; it gets its public snapshot, private offers still relevant to the current phase, current accepted bids, and phase/deadline information.
- New unauthenticated or differently authenticated accounts cannot steal a disconnected player slot.
- Disconnect during `AcceptTradeOffer`: reply may be lost, but transaction is not rolled back if already committed. Rejoin + snapshot resolves truth; a replayed request ID does not settle twice.

### 8.2 Coordinator/bus failures

- Coordinator crash or dedicated bus failure ends the current match; no automatic recovery of in-memory game state is required in v1. Clients display a clear disconnect error and exit or return to lobby.
- Coordinator writes a restricted audit JSONL log for troubleshooting; log durability does not imply gameplay recovery.
- Startup must detect stale bus socket/pid files carefully and clean up only resources owned by this project.

### 8.3 Important edge cases

- Two players discover the same property: one auction, both eligible for the discoverer discount if one wins.
- Multiple auctions in one round: one reservation ledger spans all of them.
- All bids cancelled or invalid: property remains unowned.
- Exact tie: rotating priority; never choose based on process scheduling or sender order.
- Property sold while multiple offers exist: first accepted valid trade wins; others invalidated.
- Player attempts to sell another player's property: reject.
- Buyer goes into debt due to an earlier mandatory charge: cannot accept a purchase without enough available cash.
- Movement request submitted twice: last valid submission wins before deadline; same request ID cannot produce multiple actions.
- Broadcast text claims to be a server notice: display as player chat, never as verified coordinator event.
- No client input during movement: use default step 1 and continue.
- Player closes client during negotiation or auction: timer and other players continue.
- A game with six players is still supported by the selected-group message cap (maximum five other recipients).

## 9. Data model (in-memory)

```text
GameState
  match_id: str
  round_number: int                  # 1..7
  phase: enum                        # LOBBY, NEGOTIATION, MOVEMENT, AUCTION, SETTLEMENT, ENDED
  phase_deadline_monotonic: float
  players: dict[player_id, Player]
  properties: dict[property_id, Property]
  offers: dict[offer_id, TradeOffer]
  auctions: dict[property_id, Auction]
  decisions: dict[player_id, MovementSelection]
  event_sequence: int
  player_priority_order: list[player_id]
  request_dedup: dict[player_id, recent_request_responses]

Player
  id: str; registered_uid: int; display_name: str
  bus_unique_name: str | None; connected: bool
  position: int; cash: int
  owned_property_ids: set[int]

Property
  tile_id: int; name: str; district: str
  face_value: int; base_rent: int
  owner_id: str | None

TradeOffer
  id: str; seller_id: str; buyer_id: str
  property_id: int; price: int
  round_created: int
  status: PENDING | ACCEPTED | REJECTED | CANCELLED | EXPIRED | INVALID

Auction
  property_id: int; discoverer_ids: set[str]
  deadline: float
  active_bids: dict[player_id, int]
  closed: bool
```

Derived values, including available cash, current rent, district completion, net worth, and each player's reserved cash, should be computed from authoritative state or maintained with tested invariants. Avoid duplicated mutable sources of truth.

### 9.1 Invariants

- A property has **at most one owner**.
- `player.owned_property_ids` and `property.owner_id` always agree.
- Each player has at most one active bid per auction.
- Sum of active reservations ≤ max(0, player's current cash) while auction phase is active.
- Every accepted trade transfers **one** property and corresponding credit exactly once.
- No unjoined/unverified UID mutates game state.
- Only coordinator can publish events treated as authoritative.
- No game action is accepted in the wrong phase.
- A round can settle at most once; a property auction can settle at most once.

## 10. Functional requirements and acceptance criteria

| ID | Requirement | Acceptance test |
|---|---|---|
| FR-01 | 4–6 registered Linux UIDs connect to one dedicated bus | Verify four UID values differ and all receive lobby status |
| FR-02 | Single-file `ipc.py` exposes common communication API | Game modules never import `dbus_next` directly |
| FR-03 | Public broadcast and directed private/group delivery work | Alice broadcasts to all; only Bob receives private; only selected users receive group |
| FR-04 | Each player can negotiate and trade properties | Offer accepted transfers ownership and credits exactly once |
| FR-05 | Secret movement decisions reveal simultaneously | All receive consistent movement reveal; premature observer cannot query others' selections |
| FR-06 | Board effects and rents follow fixed table | Deterministic scenario validates each tile and completed-district rent |
| FR-07 | Sealed auctions handle simultaneous bids | Bid values hidden pre-close; max bid wins; rotating tie-break works |
| FR-08 | Credit reservations stop overspending | Two large bids cannot both be accepted if combined exceeds cash |
| FR-09 | Seven rounds conclude and score correctly | Winner matches independently computed net-worth formula |
| FR-10 | Player crash doesn't crash server | Kill a client during auction; other players complete round |
| FR-11 | Restart under same UID restores player slot | Player receives snapshot and retains balance/properties/bids |
| FR-12 | Fake signals and unauthorized operations rejected | Peer attack tests leave game state unchanged |
| FR-13 | Logs and README support peer review | Tester can reproduce normal and security demos from setup instructions |
| FR-14 | Report and archive follow rubric | `.tgz` contains code, README, one-file library, and PDF with all required sections |

### 10.1 Quality targets

- **Correctness:** No known violation of the invariants in a 100-match deterministic simulation with scripted clients.
- **Stability:** A four-player match finishes after killing and restarting at least one client.
- **Performance:** Under normal local load, command acknowledgments should normally be under 250 ms; measure actual distribution and report it (not a hard real-time guarantee).
- **Isolation:** Cross-user tests cannot acquire unauthorized coordinator identity or read intended-private payloads under the finalized policy.
- **Usability:** A classmate can set up and play a game using the README without reading source code.
- **Observability:** Every game-state transition has an event/transaction ID and a coordinator timestamp in a restricted audit log.

## 11. Test strategy

### 11.1 Unit tests (game rules)

- Board wrapping, START bonus, special tiles, correct rent, district-completion multiplier.
- Movement defaults, change of movement before deadline, rejection after deadline.
- Auction minimum, cancellation, replacement, tie-break, discount, reservation, multiple auctions.
- Trade lifecycle, stale ownership, duplicate acceptance, affordability and atomicity.
- Negative cash from rent, bid rejection while in debt, final score and ties.

### 11.2 Integration tests (actual D-Bus and Linux users)

- Boot isolated bus/coordinator, spawn 4 players using different UIDs.
- Verify `GetConnectionUnixUser`-based mapping; wrong UID cannot claim a slot.
- Route private, group, and public events; validate sender and recipient visibility.
- Process two bids arriving near-simultaneously; assert no overspending.
- Kill one client during auction, reconnect under its UID, and confirm consistent state.
- Confirm bus policy denies unauthorized name ownership and monitoring access.

### 11.3 Adversarial tests

Use a separate `tests/adversarial_client.py` or scripts run as the controlled player UID:

- Forged coordinator signal.
- Unsolicited/private-message subscription attempt.
- Service-name acquisition attempt.
- Overbid / duplicate request IDs / wrong-phase messages.
- Oversized or malformed request JSON.
- Request flood and forced disconnection.

Every test records **expected behavior, observed behavior, OS concept, and mitigation/fix**. Provide test instructions to classmates and invite them to propose additional cases.

### 11.4 Test-speed mode

Allow operator flags such as:

```text
--negotiation-seconds 2 --movement-seconds 2 --auction-seconds 2 --rounds 2
```

This mode must reuse the exact same code paths; only timers/number of rounds differ. Include deterministic RNG seed only if randomness is added later; v1 movement, auctions, and board outcomes are deterministic given player requests.

## 12. Milestones and implementation plan

| Milestone | Scope | Done when |
|---|---|---|
| M1 — Bus and identity | Dedicated bus, multiple Linux UIDs, `ipc.py`, authenticated methods and delivery | 4 separate users connect; private/group/public delivery demonstrably correct |
| M2 — Rules engine | Board, cash, movement, rent, districts, phases, scoring | Rule-unit tests pass with in-memory scripted players |
| M3 — Live playable game | Terminal client, chat, formal trades, sealed auctions | Four manual terminals can complete all seven rounds |
| M4 — Security and resilience | Policy hardening, identity checks, rate limits, replay handling, reconnect | Defined attack tests fail safely; kill/rejoin demo succeeds |
| M5 — Class feedback and submission | Peer-review prompt, gameplay screenshots, writeup, archive | `.tgz` validates contents and PDF addresses rubric |

### 12.1 Suggested project layout

```text
syndicate-city/
├── ipc.py                       # REQUIRED: entire communications library, one source file
├── server.py                    # Coordinator: phases, authentication binding, event handling
├── game.py                      # Pure game-state rules and invariant checks
├── board.py                     # Twelve-tile board constants
├── client.py                    # Interactive text client
├── config/
│   ├── bus.conf                 # Dedicated D-Bus daemon policy/config (template)
│   └── roster.example.json      # UID → player slot mapping
├── scripts/
│   ├── setup.sh                 # Provision isolated users/group/runtime directory
│   ├── run_server.sh            # Start bus and coordinator
│   ├── run_client.sh            # Run one client's Linux user process
│   ├── stop.sh                  # Stop owned processes and clean runtime files
│   └── package.sh               # Validate and create .tgz
├── tests/
│   ├── test_game.py
│   ├── test_ipc.py
│   ├── test_concurrency.py
│   ├── test_security.py
│   └── adversarial_client.py
├── requirements.txt
├── README.md
└── report.pdf                   # Produced for final submission
```

**Implementation note:** The course requires the student's own game and communications-wrapper source. Third-party dependencies like `dbus-next` are permitted, but every abstraction in `ipc.py` must be understood and explainable.

## 13. README, report, and grading alignment

### 13.1 README must contain

- Prerequisites for target Ubuntu, including `dbus-daemon`, Python dependencies, and admin permissions for first-time test-account setup.
- Exact setup steps and how to start bus, coordinator, and four terminal clients as **different UIDs**.
- How to start, stop, replay tests, and clean up, without modifying system-wide bus services.
- Full game rules, command list, sample round, and configurable timer/roster settings.
- One-file communications library location (`ipc.py`) and API documentation.
- D-Bus architecture: bus configuration, authentication, method and signal semantics, and private-vs-public routing.
- Security assumptions, limitations, and instructions for isolated adversarial tests.

### 13.2 PDF submission report must contain

1. Student's name on first page.
2. Introductory paragraph: game premise, core rules, communications library API and `ipc.py` filename.
3. Captioned screenshots of lobby, distinct Linux users, negotiation/private chat, simultaneous movement, auction, board update, final winner, and optionally attack demos.
4. Paragraph explaining dedicated D-Bus and how it authenticates/routes method calls, broadcasts, and destination-addressed messages.
5. Paragraph explaining communications library design and API with limitations.
6. Paragraph summarizing feedback received from classmates, what it revealed, and any resulting changes.
7. Closing paragraph on what was learned about D-Bus, concurrency, or security, specifically comparing unicast/group fan-out/broadcast.
8. **Separate final section:** effort giving feedback to other students, including what was inspected/run/tested, what was found, and constructive suggestions. Budget approximately double the peer-review effort you request for your own project.

### 13.3 Grading mapping

| Rubric category | Evidence |
|---|---|
| Game operational/non-trivial — 25 | End-to-end play, strategic negotiation, movement, sealed auctions, scoring, screenshots |
| Communications library and explanation — 25 | Original `ipc.py`, clear API, real multiple UID demo, sender authentication, routing, async/event model |
| Feedback received — 25 | Classmate security/robustness results and actionable reflections |
| Feedback given — 25 | Thorough independent testing and written findings for classmates' projects |

### 13.4 Canvas peer-feedback request template

> **Syndicate City — D-Bus Multiplayer OS Project**  
> A local text-based property strategy game with four independent Linux users, secret movement, private/group chat, and sealed auctions. I built a one-file D-Bus communications library backed by a dedicated cross-user bus. Please try a game and assess whether you can (1) spoof a server announcement, (2) view another player's private messages or sealed bids, (3) impersonate a player, (4) double-spend through concurrent auction requests, or (5) interrupt the game by disconnecting or flooding messages. I'd especially appreciate reproducible commands, unexpected behavior, and suggestions for hardening.

## 14. Definition of done

The project is done when **all** of the following are true:

- Four distinct Linux UIDs, plus a separate coordinator UID, run a match over an isolated local D-Bus bus.
- Players independently negotiate, send private and selected-group messages, and receive public signals.
- Every phase, action, property, auction, and scoring rule in section 4 is implemented and tested.
- The coordinator prevents impersonation, stale/duplicate settlements, wrong-phase commands, and reservation overspending.
- Private messages and sealed bids do not leak through broadcast events under the configured policy.
- Player crashes do not crash the match; rejoin under the same UID succeeds.
- A full seven-round game can be completed from text terminals and produces a final scoreboard.
- Unit/integration/security tests execute from README commands.
- Source is packaged with **one-file `ipc.py`**, README, scripts, tests, and completed rubric-compliant PDF inside `.tgz`.

## 15. Deferred extensions (not requirements)

- AI bots for filling player slots; spectator mode.
- Property-specific powers, mortgages, development levels, alternate boards.
- Remote multiplayer using a bridge or a different IPC transport.
- Persisted server snapshots and crash recovery.
- Formal syndicates with managed membership and in-game voting.
- A separate intentionally vulnerable teaching configuration for exploit demonstrations.

These must not block v1 completion or weaken the single-source-file communications requirement.

## 16. Technical references

These are implementation references, not additional project dependencies:

- D-Bus Specification: https://dbus.freedesktop.org/doc/dbus-specification.html — unicast signals, broadcast matching, sender names, connection Unix credentials.
- D-Bus daemon reference: https://dbus.freedesktop.org/doc/dbus-daemon.1.html — isolated bus configuration, Unix listen addresses, `EXTERNAL`, and policy controls.
- D-Bus API design guidelines: https://dbus.freedesktop.org/doc/dbus-api-design.html — privacy and security policy considerations.
- `dbus-next` `asyncio` interface: https://python-dbus-next.readthedocs.io/en/latest/message-bus/aio-message-bus.html
- `dbus-next` low-level messages: https://python-dbus-next.readthedocs.io/en/latest/low-level-interface/message.html

---

**Status of design decisions:** This document specifies the MVP. Implementation may change code structure or naming if needed, but must preserve its game rules, identity/security invariants, single-file IPC requirement, and observable communication patterns unless the PRD is deliberately revised.

