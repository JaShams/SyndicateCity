# Syndicate City — Player's Manual

Syndicate City is a property game for 4–6 players, played in a text terminal. You buy properties in sealed auctions, trade them with rivals, and collect rent. You also choose your moves in secret, at the same moment as everyone else. After seven rounds, the richest player wins.

This manual is for **players** and for the **operator** running a game. For installation details and the technical design, see `README.md`.

> The terminal excerpts in this manual come from a real game recorded on Ubuntu 26.04, with Alice at the keyboard. A few lines are marked *(format example)*. Those show messages for situations that did not happen in that recorded game, exactly as the client prints them.

---

## Contents

1. [Quick start](#1-quick-start)
2. [What you see on screen](#2-what-you-see-on-screen)
3. [How a round works](#3-how-a-round-works)
4. [The board](#4-the-board)
5. [Money, rent and debt](#5-money-rent-and-debt)
6. [Auctions](#6-auctions)
7. [Trading properties](#7-trading-properties)
8. [Talking to other players](#8-talking-to-other-players)
9. [Winning](#9-winning)
10. [Command reference](#10-command-reference)
11. [Error messages](#11-error-messages)
12. [Strategy tips](#12-strategy-tips)
13. [Troubleshooting](#13-troubleshooting)
14. [Running a game (operator)](#14-running-a-game-operator)
15. [Privacy: who can see what](#15-privacy-who-can-see-what)

---

## 1. Quick start

You need an account that the operator has registered as a player (`syndicate-p1` … `syndicate-p6`). Each player plays as a **different** Linux user.

```bash
scripts/run_client.sh 1        # play as player 1 (alice); use 2, 3, 4 ... for the others
```

If you are already logged in as a player account, run `scripts/run_client.sh` with no number.

Prefer a full-screen view with a live board, a status panel and a ticking timer? Add `--tui`:

```bash
scripts/run_client.sh 1 --tui
```

Everything in this manual applies to both views; [§2](#full-screen-mode---tui) shows what is different.

On joining you see your identity and the other players:

```
Joined as alice (p1); coordinator :1.0. Type /help.
  p1 alice        cash   600  tile  0  -
  p2 bob          cash   600  tile  0  - (disconnected)
  p3 carol        cash   600  tile  0  - (disconnected)
  p4 dave         cash   600  tile  0  - (disconnected)
[SERVER] alice joined
[SERVER] dave joined
[SERVER] carol joined
[SERVER] bob joined
```

Wait for the operator to start the match. Until then, only `/board`, `/players`, `/status` and `/help` do anything useful; chat opens when the first round begins.

**The one rule to remember:** every command starts with `/`. A line *without* a `/` is sent to everyone as public chat. Typing `bid dockyard 200` during an auction without the slash announces your sealed bid to the whole table.

---

## 2. What you see on screen

Every message starts with a tag in brackets: the message type, the round, and the phase.

```
[GROUP R1 NEGOTIATION] bob -> [alice,carol]: group-p2-r1
[SERVER R1 MOVEMENT] MOVEMENT: choose /move 1|2|3 within 6s (default 1)
[AUCTION R1 SETTLEMENT] Dockyard won by alice for 185 (incl. -15 discoverer discount)
```

| Tag | Meaning |
|---|---|
| `SERVER` | Announcements from the game itself: phases, moves, round results |
| `PUBLIC` | Public chat, seen by everyone |
| `PRIVATE` | A one-to-one message to or from you |
| `GROUP` | A message to a chosen group of players that includes you |
| `TRADE` | Property offers to or from you, and what happened to them |
| `AUCTION` | Auction openings, your own bids, and results |
| `ERROR` | Something you did was refused, or a connection problem |

`SERVER`, `TRADE` and `AUCTION` lines can only come from the game server. Another player can type `[SERVER] you won!` into chat, but it will appear as `[PUBLIC ...] bob: [SERVER] you won!`, clearly marked as Bob's message. Chat text is also cleaned of terminal control codes, so nobody can mess up your screen.

In an interactive terminal, your prompt shows your name, the round, the phase and the approximate time left:

```
[alice R1 NEGOTIATION 32s]>
```

The countdown is only a guide. The **server's** clock decides when a phase ends, and an action sent a moment too late is refused even if your countdown still shows time.

### Full-screen mode (`--tui`)

`scripts/run_client.sh <n> --tui` keeps the whole game in view instead of scrolling it away:

* **Top bar:** round, phase and a countdown bar that ticks every second. It turns yellow at 10 s and red at 5 s.
* **Board:** the 12-tile ring. Each property shows its owner (or price), and each player is a numbered, coloured pawn. Your own tile is outlined in your colour, tiles up for auction are highlighted, and `*` marks doubled rent. The centre shows who holds each district.
* **Side panel:** your cash, reserved bids, free cash and net worth; offers to and from you; open auctions with minimum bids and your bid; every player's cash and net worth; the tie-break order; and your properties.
* **Log:** the same tagged lines as the normal client. On terminals 110 columns or wider, chat gets its own column on the right.
* **Hint line:** what you can do right now, for example `bob offers you Old Town for 230: /accept o3 or /reject o3`.

Keys:

| Key | Does |
|---|---|
| `1` `2` `3` | Choose your move during MOVEMENT, when the input line is empty |
| `Tab` | Complete commands, player names, properties and offer ids (press again to cycle) |
| `Up` / `Down` | Command history |
| `PgUp` / `PgDn`, `End` | Scroll the log, jump back to the latest |
| `F1` or `/help` | Help window (`Esc` closes it) |
| `Ctrl-C` or `/quit` | Leave the game |

`/board` hides or shows the map, and `/clear` empties the log. The terminal needs to be at least 80×24 to show the board; on smaller terminals the panel and log still work. The terminal bell rings when someone sends you a trade offer or a private or group message. When the match ends, the final scoreboard opens in a window and is printed again after you quit.

---

## 3. How a round works

A match lasts **7 rounds**. The operator can choose fewer for a quick game. Every round has the same four phases:

```
 NEGOTIATION (45 s)  →  MOVEMENT (15 s)  →  AUCTION (25 s, only if needed)  →  SETTLEMENT (instant)
   chat, trade            secret move          sealed bids                     results, next round
```

### NEGOTIATION — talk and trade

You can chat freely and make, accept or reject formal property offers (see [Trading](#7-trading-properties)). Any offer still unanswered when the phase ends expires.

```
[SERVER R1 NEGOTIATION] === Round 1 === NEGOTIATION for 10s. Tie-break priority: alice > bob > carol > dave
```

The **tie-break priority** shown here decides who wins a tied auction this round. It rotates every round (see [Auctions](#6-auctions)).

### MOVEMENT — choose 1, 2 or 3, in secret

Use `/move 1`, `/move 2` or `/move 3`. You can change your mind as often as you like before the deadline; your last valid choice counts. Nobody else can see your choice. **If you don't choose, you move 1.**

```
[SERVER R1 MOVEMENT] MOVEMENT: choose /move 1|2|3 within 6s (default 1)
[SERVER R1 MOVEMENT] movement choice recorded: 3 (private until reveal)
[SERVER R1 MOVEMENT] movement choice recorded: 1 (private until reveal)
```

When the phase ends, everyone's moves are revealed and applied at the same time:

```
[SERVER R2 MOVEMENT] Movement revealed:
  alice        1 (default): Dockyard -> Old Town; discovered Old Town
  bob          1: Dockyard -> Old Town; discovered Old Town
  carol        2: Dockyard -> Safehouse; Safehouse +30
  dave         2: Dockyard -> Safehouse; Safehouse +30
```

`(default)` means that player did not choose in time. Any number of pawns can share a tile.

### AUCTION — sealed bids on what was discovered

If anyone landed on an **unowned** property, that property goes up for auction. If several properties were discovered, they are all auctioned at once. If nothing was discovered, this phase is skipped.

```
[SERVER R1 AUCTION] AUCTION open for 8s
[AUCTION R1 AUCTION] Sealed auctions: Dockyard [1] min 128 (found by alice, bob, carol, dave). Use /bid <property> <amount>.
```

### SETTLEMENT — results

Auctions close, winners pay, and everyone sees the updated standings:

```
[SERVER R1 SETTLEMENT] SETTLEMENT
[AUCTION R1 SETTLEMENT] Dockyard won by alice for 185 (incl. -15 discoverer discount)
[AUCTION R1 SETTLEMENT] your bid of 200 on Dockyard: WON, paid 185
[SERVER R1 SETTLEMENT] Round 1 settled.
  p1 alice        cash   415  tile  1  Dockyard
  p2 bob          cash   600  tile  1  -
  p3 carol        cash   600  tile  1  -
  p4 dave         cash   600  tile  1  -
```

---

## 4. The board

There are twelve tiles in a loop. Everyone starts on tile 0. Type `/board` to see a map of it. Pawns move clockwise around the ring, starting from START in the top-left corner. Each tile box shows who owns it, and a line like `@ 2 3` marks the pawns standing there. The numbers match the player list in the middle, which also shows everyone's cash and marks you with `you` (and anyone disconnected with `off`):

```
+--------------+--------------+--------------+--------------+
| 0 START      | 1 Dockyard   | 2 Old Town   | 3 Safehouse  |
| +50 passing  | Blue $160    | Blue $160    | +30 credits  |
|              | rent 25      | rent 25      |              |
|              | owner -      | owner -      |              |
| @ 1 2 3 4    |              |              |              |
+--------------+--------------+--------------+--------------+
| 11 Uptown    | SYNDICATE CITY              | 4 Midtown    |
| Red $220     | Round 1  NEGOTIATION        | Green $180   |
| rent 40      | clockwise from START -->    | rent 30      |
| owner -      |                             | owner -      |
|              | 1 alice           600 you   |              |
+--------------+ 2 bob             600       +--------------+
| 10 Tech Park | 3 carol           600       | 5 Financial  |
| Red $220     | 4 dave            600       | Green $180   |
| rent 40      |                             | rent 30      |
| owner -      |                             | owner -      |
|              |                             |              |
+--------------+--------------+--------------+--------------+
| 9 Supply Drop| 8 Warehouse  | 7 Industrial | 6 Checkpoint |
| +30 credits  | Purple $200  | Purple $200  | -40 credits  |
|              | rent 35      | rent 35      |              |
|              | owner -      | owner -      |              |
|              |              |              |              |
+--------------+--------------+--------------+--------------+
```

Long player names are cut short to fit the boxes. `/board list` shows the same board as a table, with full owner names and the minimum bid for each property:

```
   0 START       +50 when passing   <- alice, bob, carol, dave
   1 Dockyard    Blue   value 160 rent 25 min bid 128  owner: -
   2 Old Town    Blue   value 160 rent 25 min bid 128  owner: -
   3 Safehouse   +30 credits
   4 Midtown     Green  value 180 rent 30 min bid 144  owner: -
   5 Financial   Green  value 180 rent 30 min bid 144  owner: -
   6 Checkpoint  -40 credits
   7 Industrial  Purple value 200 rent 35 min bid 160  owner: -
   8 Warehouse   Purple value 200 rent 35 min bid 160  owner: -
   9 Supply Drop +30 credits
  10 Tech Park   Red    value 220 rent 40 min bid 176  owner: -
  11 Uptown      Red    value 220 rent 40 min bid 176  owner: -
```

* **Properties** (8 of them) come in four **districts** of two. If you own both properties in a district, rent on both doubles, and the pair is worth a bonus at the end.
* **Safehouse** and **Supply Drop** give you 30 credits when you land on them. **Checkpoint** takes 40.
* **START:** each time your move carries you from tile 11 past or onto tile 0, you collect **50**. Starting a move on START earns nothing.

In commands, you can name a property by its tile number or its name: `10`, `techpark` and `tech park` all mean Tech Park.

---

## 5. Money, rent and debt

* Everyone starts with **600 credits**. Everyone's cash and properties are public; check them with `/players`.
* **Rent:** landing on a property someone else owns costs you its rent, which is paid to the owner. Landing on your own property costs nothing.

  | District | Rent | Rent with both properties |
  |---|---:|---:|
  | Blue | 25 | 50 |
  | Green | 30 | 60 |
  | Purple | 35 | 70 |
  | Red | 40 | 80 |

* **Debt is allowed, but it locks you out of buying.** Rent and Checkpoint are charged even if they push your cash below zero. You are never eliminated: you keep moving, collecting rent and bonuses, and you can still sell properties. But you can only *bid* or *buy* with money you actually have.

  ```
  [SERVER R3 MOVEMENT] Movement revealed:                                    (format example)
    carol        2: Dockyard -> Old Town; paid 50 rent to alice
  ```

* **Available cash** = your cash minus whatever you have tied up in active bids (never less than zero). Spending checks always use available cash. `/status` shows all three numbers:

  ```
  you: cash 600  reserved 200  available 400  tile 1
  ```

---

## 6. Auctions

* **Who can bid:** everyone, not just the players who discovered the property.
* **Sealed:** nobody sees your bid. After the auction, only the winner and the price they paid are announced; losing bids are never revealed.
* **Minimum bid** is 80% of the property's value: Blue 128, Green 144, Purple 160, Red 176.
* **One bid per property.** Bidding again on the same property *replaces* your earlier bid. `/cancelbid <property>` withdraws it.
* **Your bid is reserved.** The full amount is set aside until the auction ends, so you can't promise the same money twice. For example, with 600 credits you could bid 300 on one property and 300 on another, but not 400 on both.
* **Highest bid wins.** Losing bids are released when the auction closes.
* **Discoverer discount:** if you landed on the property this round and you win it, you pay **15 less** than your bid.
* **Ties** go to whoever comes first in this round's **tie-break priority**. The order is announced at the start of each round and rotates: in round 1 player 1 is first, in round 2 player 2, and so on. The speed of your connection has no effect.
* **No bids?** The property stays unowned and can be discovered again later.

```
[ERROR R1 AUCTION] BID_TOO_LOW: minimum bid for Dockyard is 128
[AUCTION R1 AUCTION] your bid on Dockyard: 200 (available 400)
...
[AUCTION R1 SETTLEMENT] Dockyard won by alice for 185 (incl. -15 discoverer discount)
```

---

## 7. Trading properties

During **NEGOTIATION**, the owner of a property can offer to sell it to one player for a fixed price:

```
/offer bob dockyard 180
```

```
[TRADE R2 NEGOTIATION] offer o1 sent: Dockyard to bob for 180
```

The buyer sees the offer together with its ID:

```
[TRADE R2 NEGOTIATION] alice offers you Dockyard for 180 -> /accept o1 or /reject o1    (format example)
```

The buyer then answers with `/accept o1` or `/reject o1`. The seller can withdraw an offer with `/canceloffer o1`. `/offers` lists your pending offers in both directions (`OUT` means you are selling, `IN` means you are buying):

```
  OUT o1: alice sells Dockyard to bob for 180
```

Things to know:

* **One property for credits.** You can't swap two properties or bundle several into one offer. A swap can be done as two separate sales.
* The **buyer must have the cash** when they accept. Making an offer does not lock up the buyer's money.
* An accepted trade happens immediately and **can't be undone**. If you made several offers for the same property, only the first one accepted goes through; the others become `INVALID`.
* Offers that are still open when NEGOTIATION ends **expire**:

  ```
  [TRADE R2 NEGOTIATION] offer o1 (Dockyard, 180, alice -> bob): EXPIRED
  ```

* Limits: a seller can have at most 3 offers open at once, and a buyer can receive at most 6.

---

## 8. Talking to other players

Chat is open during **NEGOTIATION** and **AUCTION**. Messages are at most 300 characters.

| Command | Who receives it |
|---|---|
| `/say <message>` (or any line without `/`) | everyone |
| `/tell <player> <message>` | only that player |
| `/group <p1,p2,...> <message>` | only the listed players (up to 5, comma-separated, no spaces) |

```
[PUBLIC R1 NEGOTIATION] alice: hello everyone
[PRIVATE R1 NEGOTIATION] you -> bob: want to split the Blue district?
[GROUP R1 NEGOTIATION] alice -> [bob,carol]: nobody bid on Tech Park, ok?
[PRIVATE R1 NEGOTIATION] bob -> you: deal, I'll stay out of Tech Park          (format example)
```

Players can be named by name (`bob`) or slot (`p2`).

**Promises are not enforced.** Agreements made in chat are part of the game, and so is breaking them. Only formal `/offer` trades are binding.

---

## 9. Winning

After the last round, the game scores every player:

> **Net worth = cash + face value of every property you own + 80 for each completed district**

Cash can be negative, which lowers your score. Ties are broken by more cash, then by more properties. If players are still tied after that, they share the win.

```
[SERVER R2 ENDED] MATCH OVER. Final scoreboard:
  1. dave         net worth   630  cash   630  properties 0  districts -
  2. carol        net worth   624  cash   464  properties 1  districts -
  3. bob          net worth   600  cash   600  properties 0  districts -
  4. alice        net worth   575  cash   415  properties 1  districts -
Winner(s): dave
```

(In this short two-round game Alice paid 185 for Dockyard, which is worth 160, so the purchase cost her 25 net worth. Nobody landed on it before the game ended, so it never earned any rent to make that back.)

There is no early win. Disconnected players stay in the game and simply move 1 each round until they return.

---

## 10. Command reference

| Command | Phase | What it does |
|---|---|---|
| `/help` | any | List commands |
| `/board` | any | Board map: owners, pawn positions, player legend |
| `/board list` | any | Board as a table, including minimum bids |
| `/status` | any | Round, phase, time left, tie-break order, your cash, reserved and available money, your bids, movement choice and offers |
| `/players` | any | Everyone's cash, tile and properties |
| `/offers` | any | Your pending offers |
| `/say <msg>` | NEGOTIATION, AUCTION | Public chat |
| `/tell <player> <msg>` | NEGOTIATION, AUCTION | Private message |
| `/group <p1,p2> <msg>` | NEGOTIATION, AUCTION | Message to selected players |
| `/offer <buyer> <property> <price>` | NEGOTIATION | Offer to sell a property you own |
| `/accept <id>` / `/reject <id>` | NEGOTIATION | Answer an offer made to you |
| `/canceloffer <id>` | NEGOTIATION | Withdraw your own offer |
| `/move <1\|2\|3>` | MOVEMENT | Choose your move (changeable until the deadline) |
| `/bid <property> <amount>` | AUCTION | Place or replace a sealed bid |
| `/cancelbid <property>` | AUCTION | Withdraw your bid |
| `/quit` | any | Leave. You stay in the match and can come back (see §13) |

Short forms: `/m` = `/move`, `/b` = `/bid`, `/a` = `/accept`, `/r` = `/reject`, `/t` = `/tell`, `/s` = `/status`, `/p` = `/players`, `/h` = `/help`, `/q` = `/quit`. For example, `/b 1 150` bids 150 on Dockyard.

---

## 11. Error messages

A refused action prints `[ERROR ...] CODE: explanation`, and nothing in the game changes. The most common codes:

| Code | What it means | What to do |
|---|---|---|
| `WRONG_PHASE` | That action isn't allowed in the current phase | Check the phase in your prompt or with `/status` |
| `DEADLINE_PASSED` | The phase ended just before your command arrived | Too late for this phase |
| `INVALID_ARGUMENT` | A bad value, e.g. `/move 5` or a price of 0 | Fix the value |
| `BID_TOO_LOW` | Below the 80% minimum | Bid at least the minimum shown |
| `INSUFFICIENT_FUNDS` | Not enough available cash (debt or other bids count against you) | Lower the bid, or cancel another bid |
| `NO_AUCTION` | That property isn't up for auction this round | Check `open auctions` in `/status` |
| `NO_BID` | `/cancelbid` on a property you haven't bid on | — |
| `NOT_OWNER` | You tried to sell a property you don't own | — |
| `UNKNOWN_OFFER` | No such offer, or it isn't addressed to you | Check `/offers` |
| `OFFER_CLOSED` | The offer was already accepted, rejected, cancelled or expired | — |
| `OFFER_INVALID` | The seller no longer owns the property | — |
| `UNKNOWN_PLAYER` / `UNKNOWN_PROPERTY` | A name the game doesn't recognise | Use names from `/players` or `/board` |
| `LIMIT_EXCEEDED` | Too many open offers | Cancel one or wait |
| `TOO_LONG` | Chat message over 300 characters | Shorten it |
| `RATE_LIMITED` | You sent commands too fast (about 10 per second, chat 5 per second) | Wait a second |
| `NOT_JOINED` | Your session isn't joined | Restart the client |

Typing a command the client doesn't know prints `unknown command /foo; try /help`.

---

## 12. Strategy tips

* **Completing a district is the big prize.** It doubles your rent on both properties and adds 80 to your final score. If you own one half of a district, its other half is worth more to you than to anyone else, and the player who owns that other half knows it.
* **Every round, you choose which tiles you might land on.** From any tile you can reach the next three. Use this to avoid a rival's doubled rent, to reach a +30 tile, or to land on an unowned property and earn the discoverer discount.
* **Watch the tie-break priority.** In a round where you are first in priority, you win every exact tie, so a round number that others are likely to bid is safe for you and risky for them.
* **Reserved bids lock up your money.** Bidding big on several auctions at once leaves you no money for anything else. A bid you don't really want only blocks your cash, so cancel it.
* **Keep some money back.** Rent can push you into debt, and a player in debt can't bid or buy. Near the end, every credit of debt counts directly against your score.
* **Price against face value.** A property is scored at its face value. Buying it below that value (e.g. Dockyard, worth 160, for the 128 minimum) raises your net worth immediately. Paying above it lowers your net worth, and only rent can earn the difference back. With few rounds left, it won't.
* **Talk privately, and verify publicly.** Anyone can promise anything in chat. Trust `/players` and the auction results instead.

---

## 13. Troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `cannot join: /run/syndicate-city/bus.address not found (is the server running?)` | The game server isn't running | Ask the operator to start it |
| `cannot join: the coordinator is not running on this bus` | The message bus is up but the game server isn't (yet) | Wait a moment and retry; ask the operator |
| `cannot join: no permission to read /run/syndicate-city/bus.address ...` | You are not using a player account | Use `scripts/run_client.sh <n>` or log in as `syndicate-pN` |
| `cannot join: UNKNOWN_IDENTITY: UID ... is not on the roster` | Your account is not one of this match's players | Ask the operator which account to use |
| `cannot join: DUPLICATE_SESSION: ...` | Your player is already connected in another terminal | Close the other client first |
| `[ERROR] coordinator left the bus; exiting` | The game server stopped (match over, or the operator quit) | — |
| `[ERROR] missed N public update(s); refreshing` | Your client fell behind briefly | Nothing to do: the client refreshes automatically |
| `[ERROR] ignored a GameEvent signal from non-coordinator ...` | Someone tried to send a fake game message | Nothing to do: it was ignored. You may want to tell the operator |

**Disconnected or crashed?** Just start the client again with the same account. You get your seat back with your cash, properties, bids and open offers unchanged. While you were away, the game kept going: you moved 1 each round, and any bid you had already placed stays in its auction. Other players see `[SERVER] alice disconnected` and later `alice rejoined`.

---

## 14. Running a game (operator)

One-time setup (creates the player accounts; see README §2):

```bash
sudo scripts/setup.sh                 # 4 players; --players 5 or 6 for more
```

Start the server in its own terminal:

```bash
sudo scripts/run_server.sh
```

```
Syndicate City coordinator :1.0 owns org.syndicate.City; match 2f802d35; 4 players; 2 rounds.
Waiting for players. Type 'start' when everyone has joined, 'help' for commands.
PlayerJoined: alice (p1)
...
```

Then have each player run `scripts/run_client.sh <n>`. The server console accepts these commands:

| Command | Effect |
|---|---|
| `players` | Who has joined, their Linux UID, cash, position and properties |
| `start` | Begin the match. Refused, with nothing changed, unless every player is connected |
| `status` | Current round and phase, time left, open auctions, and server response times |
| `quit` | End the game and shut down the server and its message bus |

```
p1 alice        uid=1018   connected  cash=600 pos=0 props=[]
p2 bob          uid=1019   connected  cash=600 pos=0 props=[]
...
round 1/2 phase NEGOTIATION remaining 9.0s auctions []
request latency ms: p50=0.48 p95=1.59 max=1.59 (n=14)
```

**Shorter games**, e.g. for a demo or class session (the rules are identical, only the clock changes):

```bash
sudo scripts/run_server.sh --rounds 3 --negotiation-seconds 20 --movement-seconds 8 --auction-seconds 12
```

**A self-playing demo:** `sudo scripts/demo.sh` (it needs `tmux`) runs a full 4-round match with nobody at the keyboard. The server and four clients share one window, and each client plays its script from `demo/`.

**Scripted players.** `scripts/run_client.sh <n> --script FILE` makes a client play the commands in FILE instead of reading the keyboard. Blank lines and lines starting with `#` are ignored. Any other line is typed exactly as at the prompt, and the window shows it as a `[SCRIPT]` line. Lines starting with `@` control timing:

| Line | Effect |
|---|---|
| `@round 2 movement` | Wait for that round and phase (`negotiation`, `movement` or `auction`; default `negotiation`). If the phase already passed, or never happens (no auction that round), everything up to the next `@round` is skipped |
| `@wait 3` | Pause for 3 seconds, e.g. to pace a conversation |
| `@accept bob` / `@reject bob` | Wait for the next offer made to you (from bob; any seller if no name is given) and accept or reject it. Gives up when the phase ends |
| `@end` | Stop reading the script. The client still waits for the final scoreboard, then exits |

```
@round 1
@wait 2
/tell bob Want Dockyard for 180?
@round 1 movement
/move 2
@round 1 auction
/bid Old Town 150
```

Keep the phase timers long enough for each script's `@wait`s. The scripts in `demo/` assume the timers `scripts/demo.sh` uses: 25 s negotiation, 6 s movement and 12 s auction.

**Afterwards:** `sudo scripts/stop.sh` cleans up anything left running. An audit log of every action, including private bids, is written to `/var/log/syndicate-city/`. Only the server account (and root) can read it.

---

## 15. Privacy: who can see what

| Information | Who sees it |
|---|---|
| Board, ownership, positions, everyone's cash | Everyone |
| Auction winners and prices paid | Everyone, once the auction closes |
| Your movement choice | Only you, until everyone's moves are revealed |
| Your bids | Only you, and losing bids are never revealed |
| Your offers | Only you and the other side of the offer |
| Private and group messages | Only the people they were sent to |

"Private" means hidden from **other players**. The game server necessarily sees everything, and so does the machine's administrator (root). Don't use game chat for real secrets.
