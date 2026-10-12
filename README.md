# Syndicate City

Syndicate City is a text-based multiplayer property game for 4–6 players. Each player is a separate Linux user account. Every round has the same steps:

1. Players negotiate and trade properties.
2. Each player secretly picks how far to move (1–3 tiles).
3. Unowned properties that someone landed on go to sealed-bid auctions.

The game lasts seven rounds, and the highest net worth wins.

**Players: start with the [Player's Manual](USER_MANUAL.md).** This README covers installation, architecture and testing.

All communication between processes goes over a **dedicated, isolated D-Bus bus**. The whole communications layer is one file, **`ipc.py`**.

```
 syndicate-p1  client.py ─┐                                    ┌─ server.py  (syndicate-server)
 syndicate-p2  client.py ─┤   dedicated dbus-daemon            │   owns org.syndicate.City
 syndicate-p3  client.py ─┼── unix:path=/run/syndicate-city/ ──┤   single source of truth
 syndicate-p4  client.py ─┘   bus.sock (runs as syndicate-server)   audit log /var/log/syndicate-city
```

---

## 1. Prerequisites (target Ubuntu 26.04; developed on 24.04)

* `python3` (3.10+), `python3-venv`
* `dbus-daemon` (from the `dbus-daemon`/`dbus` package; present on standard Ubuntu)
* `sudo` rights, needed **once** to create the test accounts and again to start the server
* Network access for `pip install dbus-next` the first time. To work without it, `apt install python3-dbus-next` and pass `--offline` to setup.

Python dependency: `dbus-next==0.2.3` (`requirements.txt`). The tests also need `pytest` (`requirements-dev.txt`).

## 2. Setup (one time)

```bash
sudo scripts/setup.sh                 # 4 players; use --players 6 for six
```

The setup script does the following. It is idempotent and can be undone with `sudo scripts/setup.sh --uninstall`.

| Creates | Purpose |
|---|---|
| group `syndicate` | Members can reach the bus socket directory |
| system user `syndicate-server` (nologin) | Runs the bus daemon and the coordinator |
| users `syndicate-p1` … `syndicate-pN` (locked passwords) | One account per player; reached via `sudo -u` |
| `/opt/syndicate-city/` (root-owned, read-only) | Installed code plus a venv with `dbus-next` |
| `/etc/syndicate-city/bus.conf`, `roster.json` | Rendered bus policy and the UID→slot roster |
| `/var/log/syndicate-city/` (mode 0700, service user) | Restricted audit logs |

At runtime, `/run/syndicate-city/` is created with owner `syndicate-server:syndicate` and mode `0750`. It holds the socket, the address file and the pid file. System-wide bus services are never modified.

The script finishes by printing every account's UID and checking that they are all different.

## 3. Playing a game

**Terminal 0 (operator):**

```bash
sudo scripts/run_server.sh
# test-speed mode (same code paths, shorter timers):
sudo scripts/run_server.sh --negotiation-seconds 5 --movement-seconds 5 --auction-seconds 5 --rounds 2
```

**Terminals 1–4 (one per player, each a different UID):**

```bash
scripts/run_client.sh 1      # runs client.py as syndicate-p1
scripts/run_client.sh 2
scripts/run_client.sh 3
scripts/run_client.sh 4
```

Add `--tui` for the full-screen client (live board, status panel, countdown bar, Tab completion, `1`/`2`/`3` move keys): `scripts/run_client.sh 1 --tui`. It is `tui.py`, a `curses` front end over the same `ClientUI` state and commands, so the line client stays the default and the protocol is unchanged.

Run `id` inside `sudo -u syndicate-p1 bash` to see a player's UID. On the operator console, `players` lists each slot's registered UID.

When all four players have joined, type `start` in terminal 0. A `start` with players missing is refused and nothing changes. Other operator commands are `status` (which also shows request latency percentiles), `players` and `quit`.

**Stop and clean up:** `quit` (or Ctrl-C) in terminal 0 stops the coordinator and the bus. `sudo scripts/stop.sh` stops anything left over. It removes only this project's own runtime files and verifies the PID really is our `dbus-daemon` before killing it.

### Configurable settings

| Flag (server.py / run_server.sh) | Default |
|---|---|
| `--rounds` | 7 |
| `--negotiation-seconds` / `--movement-seconds` / `--auction-seconds` | 45 / 15 / 25 |
| `--require-players N` | off (the roster defines the player count) |
| `--linger` | 30 s (keeps serving snapshots after the match ends) |
| roster | `/etc/syndicate-city/roster.json` (entries take `user` or `uid`; see `config/roster.example.json`) |

## 4. Rules (summary of PRD §4)

* **Setup:** 4–6 players, 7 rounds, 600 starting credits, everyone starts on tile 0 (START). There are 12 tiles.

| # | Tile | Kind | District | Value | Rent | Min bid |
|--:|---|---|---|--:|--:|--:|
| 0 | START | +50 whenever you pass or land from tile 11 | | | | |
| 1 | Dockyard | property | Blue | 160 | 25 | 128 |
| 2 | Old Town | property | Blue | 160 | 25 | 128 |
| 3 | Safehouse | +30 | | | | |
| 4 | Midtown | property | Green | 180 | 30 | 144 |
| 5 | Financial | property | Green | 180 | 30 | 144 |
| 6 | Checkpoint | −40 | | | | |
| 7 | Industrial | property | Purple | 200 | 35 | 160 |
| 8 | Warehouse | property | Purple | 200 | 35 | 160 |
| 9 | Supply Drop | +30 | | | | |
| 10 | Tech Park | property | Red | 220 | 40 | 176 |
| 11 | Uptown | property | Red | 220 | 40 | 176 |

* **Round phases:**
  1. **NEGOTIATION (45 s).** Public, private and group chat are open. A property's owner can make a formal offer to sell it for credits; the buyer may accept or reject it. Accepted trades settle immediately and atomically. Unanswered offers expire when the phase ends.
  2. **MOVEMENT (15 s).** Each player privately picks 1, 2 or 3 and may change the choice until the deadline. Anyone who doesn't choose moves 1. All moves are revealed and resolved at the same moment.
  3. **AUCTION (25 s, only if something was discovered).** Every unowned property that someone landed on goes to a sealed-bid auction.
  4. **SETTLEMENT.** Auctions close, results are announced, and the next round begins.
* **Landing effects:**
  * Landing on another player's property costs its rent. Rent doubles if the owner holds both properties in that district.
  * Rent and Checkpoint charges can push cash below zero. Negative cash is debt, not elimination.
  * Purchases and bids must be covered by **available cash** = `max(0, cash − reserved bids)`.
* **Auction rules:**
  * Minimum bid is `ceil(0.8 × value)`.
  * Each player has at most one bid per property. A new bid replaces the old one, and bids can be cancelled.
  * The full bid amount is reserved across all auctions, so you can never overspend.
  * The highest bid wins. A player who landed on the property that round gets **15 credits off**.
  * Ties go to the rotating priority order: round *r* starts with slot `(r−1) mod N`. The order is announced at the start of each round.
  * Losing bids are never published.
* **Scoring:** `cash + face value of properties + 80 × completed districts`. Ties are broken by cash, then by property count; if still tied, there are joint winners.

**Sample round:** Bob sells Old Town to Alice for 230, which completes Blue for her. Everyone picks a move in secret. Carol lands on Alice's Dockyard and pays 50 (doubled rent). Dave discovers Tech Park and bids 200. Carol bids 210 and wins it.

### Client commands

```
/help  /board  /status  /players  /offers  /quit
/say <msg>                     /tell <player> <msg>          /group <p1,p2> <msg>
/offer <buyer> <property> <price>    /accept <id>   /reject <id>   /canceloffer <id>
/move <1|2|3>                  /bid <property> <amount>      /cancelbid <property>
short forms: /m /b /a /r /t /s /p /h /q
```

Properties can be given by tile number or name (`10`, `techpark`, `tech park`). A line without a leading `/` is sent as public chat. Every output line is tagged `SERVER / PUBLIC / GROUP / PRIVATE / TRADE / AUCTION / ERROR` along with the round and phase. All text from other players is stripped of terminal control characters before it is printed.

## 5. Communications library: `ipc.py`

`ipc.py` is the only module that imports `dbus_next`; a test enforces this. It contains no game rules.

```python
import ipc

# --- player side -------------------------------------------------------------
c = await ipc.GameIPC.connect_client(bus_address)      # refuses non-unix: addresses
snapshot = await c.join()                               # identity comes from the bus, not args
result = await c.request("SubmitBid", {"property_id": 10, "amount": 200})  # raises RequestError
await c.send_public("hi");  await c.send_private("bob", "psst")
await c.send_group(["bob", "carol"], "plan")
async for event in c.events():   # only events whose D-Bus sender is the verified coordinator
    ...
await c.close()

# --- coordinator side --------------------------------------------------------
s = await ipc.GameIPC.start_server(bus_address, roster={uid: "p1", ...},
                                   allowed_actions=[...])   # owns org.syndicate.City or fails
s.on_request(async_handler)        # handler(caller: Caller, request: Request) -> dict
s.on_disconnect(async_handler)     # handler(player_id) when a bound connection vanishes
s.broadcast(type, data, round)     # signal with no destination: public data only
s.send_to("p2", type, data, round) # signal addressed to p2's current unique name
s.multicast(["p2","p3"], ...)      # fan-out of individually addressed signals
await s.close()
```

| Concern | What `ipc.py` does |
|---|---|
| Transport | Only a `unix:` address for the dedicated bus. Never the session or system bus, never TCP |
| Name | `org.syndicate.City`, requested with `DO_NOT_QUEUE`. Startup aborts unless we become the primary owner |
| Method | One method, `org.syndicate.City1.Request(s) → s`, carrying a versioned JSON envelope `{v, id, action, payload}`. Exact key set, `v == 1`, `id` of 1–64 safe characters, allowlisted action, max 4096 bytes. Responses are also limited to 4096 bytes |
| Identity | On `Join`, the D-Bus header `sender` (set by the daemon, unforgeable) goes to `GetConnectionUnixUser`, which gives a UID, which maps to a roster slot. Binding is `unique name ↔ slot`. Later requests are authorized only by that binding. A second live connection for the same slot is refused |
| Disconnects | Watches the bus driver's `NameOwnerChanged` (with the sender checked). When a bound unique name vanishes, the binding is cleared and `on_disconnect` fires |
| Events | Signal `org.syndicate.City1.GameEvent(s)` with `{v, seq, pseq?, type, visibility, round, data}`. `pseq` counts public events contiguously so clients can detect missed broadcasts |
| Provenance (client) | AddMatch with `sender='org.syndicate.City'`. Additionally, any `GameEvent` whose sender is not the coordinator's unique name (resolved at connect time) is dropped and reported locally as `_ForgedSignalIgnored`. Visibility is derived from the D-Bus destination header, not from the payload |
| Bounds | Request timeout of 5 s. Client event queue of 1000. Server outbox is bounded. Event size ≤ 16 KiB |

**Limitations:**

* JSON inside a D-Bus string trades D-Bus-level typing for schema validation in the application.
* Event `seq` provides ordering, not durability or integrity.
* Clients and the server are not authenticated cryptographically; trust rests on the bus daemon's `EXTERNAL` (SO_PEERCRED) authentication.
* The coordinator's in-memory state is lost if it crashes.

## 6. D-Bus architecture and policy

`config/bus.conf.in` is rendered by `scripts/render_bus_conf.py`.

* **Who may connect:** `<allow user=…>` for `syndicate-server` and each roster player only. Authentication is `EXTERNAL` only, and the socket directory is `0750 syndicate-server:syndicate`.
* **Default deny:** nobody may own names, send method calls, or send signals.
* **Allowed for everyone:** calls to the bus driver, except the `Monitoring` interface and `UpdateActivationEnvironment`. Calls to `org.syndicate.City` / `org.syndicate.City1.Request` (plus `Introspectable`/`Peer`). Requested replies.
* **Service user only:** `own="org.syndicate.City"` and sending `org.syndicate.City1.GameEvent` signals.
* **No `eavesdrop="true"` rule anywhere.** Unicast messages reach only their destination. `BecomeMonitor` is available only to root or the daemon's UID (`syndicate-server`).
* **Limits:** 64 KiB messages, 3 connections per user, 32 match rules per connection, plus reply and auth timeouts.

| Communication | Mechanism | Who receives it |
|---|---|---|
| player → coordinator | method call to `org.syndicate.City` | coordinator only; reply goes to the caller |
| coordinator → all | signal, **no destination** | every connection with a match rule (public data only) |
| coordinator → one | signal **with destination** = player's unique name | that connection only (enforced by the daemon) |
| coordinator → group | N individually addressed signals | exactly the N recipients |
| player → player chat | method call → coordinator validates → addressed signals | sender label set by the coordinator |

A broadcast with a client-side `group_id` filter would **not** be private: every subscriber receives the bytes. For that reason private and group data are never broadcast.

## 7. Security model and controls

**In scope:**

* Untrusted player processes under their own UIDs. They can modify the client, use `busctl` or raw D-Bus, send malformed requests, race requests, disconnect, or flood.
* Social lying and betrayal are allowed as gameplay.

**Out of scope:** root, a compromised daemon or coordinator account, and kernel bugs. "Private" means hidden from other *unprivileged* players; the coordinator and root can see everything.

| Control | Implementation |
|---|---|
| Caller verification | Slot comes from the bus-supplied sender → UID. Payloads may not contain unknown fields (`player_id` → `BAD_PAYLOAD`) |
| Name ownership | Policy plus `DO_NOT_QUEUE` primary-owner check at startup |
| Signal provenance | Policy forbids player signals. Client checks the sender unique name |
| Confidentiality | Offers, bids, movement choices and private/group chat travel only as addressed signals. Snapshots split into `public` and the caller's `private` view. Losing bid amounts are never published |
| Authorization and phases | `game.py` checks owner, recipient, phase and deadline (`time.monotonic()` on the server) before any mutation |
| Atomicity | One `asyncio.Lock` around every validate-and-commit, with no awaits inside. Events are emitted after commit |
| Idempotency | Per-player ledger of request IDs for the whole match (cap 5000). An identical replay returns the recorded outcome; a reused ID with a different payload returns `ID_REUSED` |
| Rate and size | Token bucket of 10/s with burst 20 per player, chat 5/s. Request ≤ 4096 bytes, chat ≤ 300 characters, ≤ 5 group recipients, integers range-checked, NaN rejected |
| Disconnect safety | Accepted bids stay active. Offers expire on schedule. The slot can be reclaimed only by the same UID, and a fresh `Join` returns a full snapshot including your bids and offers |
| Logs | Audit JSONL (`txn`, wall time and monotonic time per transition) at mode 0600 under a 0700 directory owned by the service user |

## 8. Tests

```bash
scripts/run_tests.sh                    # no root needed; ~45 s
sudo scripts/integration_multiuser.sh   # after setup: real distinct UIDs end-to-end
```

| Suite | What it covers |
|---|---|
| `tests/test_game.py` | Every rule in §4: wrap and START bonus, special tiles, rent and district doubling, debt, defaults, deadlines, auction minimum, replacement, cancellation, reservation, tie-break rotation, discount, trades (stale, duplicate, atomic), chat limits, scoring and ties. Also **100 randomized 4–6-player matches** checking invariants after every step |
| `tests/test_ipc.py` | Real private `dbus-daemon`: join and roster mapping, unknown UID, duplicate session, public/private/group routing, contiguous `pseq`, a full match, private bid status, crash during auction then rejoin with the bid kept, single coordinator name, no `dbus_next` imports outside `ipc.py` |
| `tests/test_concurrency.py` | 20 concurrent overbids (no overspend), 10 concurrent accepts (settles once), concurrent replay, simultaneous movement, median ack latency < 250 ms |
| `tests/test_security.py` | Policy with a non-service UID: cannot own the name, cannot `BecomeMonitor`, cannot start a server, player signals never delivered, no direct calls between players. Plus forged-signal rejection by the client, eavesdrop match rules, identity swap, replay, wrong phase, malformed input, overbid, flood, terminal escapes, audit log mode |
| `tests/test_processes.py` | Scripted players as separate OS processes. Player 2 is killed with SIGKILL mid-auction and restarted; the logs are then checked with `verify_multiuser.py` |
| `scripts/integration_multiuser.sh` | The same checks with **four distinct Linux UIDs** plus the service UID. Adversarial probes run as `syndicate-p4` |

The pytest integration tests run under a single account. They swap only the UID resolver (a test-only hook) so that several slots can be filled; every other code path, including the policy file, is the production one. The multi-UID script needs no such hook.

## 9. Packaging

```bash
scripts/package.sh            # needs report.pdf in the project root
scripts/package.sh --draft    # review build without the report
```

The script checks that every required file is present, that only `ipc.py` imports `dbus_next`, and that the sources compile. It then writes `dist/syndicate-city.tgz`.

## 10. Files

```
ipc.py        communications library (single file)      game.py    pure rules engine + invariants
server.py     coordinator: auth binding, lock, clock     board.py   tile table
client.py     interactive terminal client                config/    bus policy template, roster example
scripts/      setup, run, stop, tests, package           tests/     unit/integration/security/adversarial
```
