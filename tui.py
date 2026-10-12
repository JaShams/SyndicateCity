"""Full-screen curses interface for the Syndicate City client (``client.py --tui``).

The game logic, event handling and commands are the line client's (``ClientUI``);
this module only replaces how things are shown and how input is read:

    header    phase, round and a live countdown bar
    board     the 12-tile ring with owners and pawns, next to a status panel
    log       tagged events (chat gets its own column on wide terminals)
    input     line editor with history, Tab completion and 1/2/3 move keys

Uses only the standard-library ``curses`` module. Everything that came off the
wire still goes through ``client.clean()`` before it is drawn.
"""

from __future__ import annotations

import asyncio
import curses
import locale
import logging
import os
import signal
import sys
import time
import unicodedata
from typing import Optional

import board
import ipc
from board import DISTRICTS, TILES
from client import ClientUI, clean

PHASE_INFO = {
    "LOBBY": ("waiting for the operator to start", "dim"),
    "NEGOTIATION": ("chat and trade", "TRADE"),
    "MOVEMENT": ("pick 1-3 in secret", "PRIVATE"),
    "AUCTION": ("buy or bid", "AUCTION"),
    "SETTLEMENT": ("settling the round", "SERVER"),
    "ENDED": ("match over", "ERROR"),
}
CHAT_TAGS = ("PUBLIC", "PRIVATE", "GROUP")
COMMANDS = ("/say", "/tell", "/group", "/offer", "/accept", "/reject", "/canceloffer", "/offers",
            "/move", "/buy", "/bid", "/cancelbid", "/board", "/status", "/players", "/help", "/clear",
            "/quit")

HELP_LINES = """\
KEYS
  Enter         send chat or run a command
  Tab           complete commands, names, properties, offer ids
  Up / Down     command history
  PgUp / PgDn   scroll the log (End jumps back to the latest)
  1  2  3       choose your move during MOVEMENT (input empty)
  F1            this help          Esc   close a window / clear input
  Ctrl-C        leave the game

CHAT  (NEGOTIATION and AUCTION)
  just type                 public chat
  /tell bob hi              private message              /t
  /group bob,carol hi       message a few players

TRADING  (NEGOTIATION)
  /offer bob oldtown 230    offer one of your properties
  /accept o3   /reject o3   answer an offer to you       /a /r
  /canceloffer o3           withdraw your own offer

MOVING  (MOVEMENT)
  press 1, 2 or 3           secret, can be changed until the deadline
  /move 2                   same thing                   /m

BUYING  (AUCTION)
  /buy 1                    buy tile 1 at its value, if only you landed there
  /bid 1 150                sealed bid on tile 1, if you and others landed there  /b
  /cancelbid 1              withdraw that purchase or bid

OTHER
  /board        hide or show the map     /board list   table with min bids
  /status  /players  /offers  /clear  /quit

Properties can be a tile number or a name: 10, techpark, "tech park".
Rent doubles when one owner holds both tiles of a district (marked *).""".splitlines()

# mask bits for box-drawing junctions
U, D, L, R = 1, 2, 4, 8
UNICODE_BOX = {L | R: "─", U | D: "│", D | R: "╭", D | L: "╮", U | R: "╰", U | L: "╯",
               U | D | R: "├", U | D | L: "┤", L | R | D: "┬", L | R | U: "┴",
               U | D | L | R: "┼"}
ASCII_BOX = {L | R: "-", U | D: "|"}


def text_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def fit(s: str, width: int) -> str:
    """Truncate ``s`` to ``width`` terminal cells."""
    if width <= 0:
        return ""
    out, used = [], 0
    for ch in s:
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def clip(s: str, width: int, more: str = "…") -> str:
    """Like fit(), but marks truncation with ``more``."""
    if text_width(s) <= width:
        return s
    return fit(s, width - text_width(more)) + more


def wrap(s: str, width: int) -> list[str]:
    """Word-wrap one line of text to ``width`` cells (hard-breaking long words)."""
    if width <= 1:
        return [s]
    lines, cur = [], ""
    for word in s.split(" "):
        cand = word if not cur else cur + " " + word
        if text_width(cand) <= width:
            cur = cand
            continue
        if cur:
            lines.append(cur)
        while text_width(word) > width:
            head = fit(word, width)
            lines.append(head)
            word = word[len(head):]
        cur = word
    lines.append(cur)
    return lines


class TuiUI(ClientUI):
    def __init__(self, conn: ipc.ClientIPC, scr, color: bool):
        super().__init__(conn, color=False, interactive=False)
        self.scr = scr
        self.unicode = "utf" in (locale.getpreferredencoding(False) or "").lower()
        self.g = {"pawn": "●", "own": "◆", "star": "*", "dot": "·", "arrow": "→",
                  "full": "█", "empty": "░", "prompt": "›", "more": "…", "sq": "■"}
        if not self.unicode:
            self.g = {"pawn": "o", "own": "#", "star": "*", "dot": "-", "arrow": "->",
                      "full": "#", "empty": ".", "prompt": ">", "more": "~", "sq": "#"}
        self.attrs = self._init_colors(color)
        self.log: list[tuple[str, str, str]] = []      # (tag, stamp, text)
        self.scroll = 0
        self.unseen = 0
        self.buf = ""
        self.cur = 0
        self.history: list[str] = []
        self.hist_i: Optional[int] = None
        self.comp: Optional[tuple[int, str, list[str], int]] = None  # (start, tail, matches, idx)
        self.overlay: Optional[tuple[str, list[str]]] = None
        self.overlay_top = 0
        self.show_board = True
        self.phase_total = 0.0
        self.lost: Optional[str] = None
        self.final_lines: list[str] = []
        self.dirty = True
        self.queue: asyncio.Queue = asyncio.Queue()
        self.log_width = 80
        self.log_rows = 10

    # ------------------------------------------------------------- colours

    def _init_colors(self, want: bool) -> dict:
        a = {k: curses.A_NORMAL for k in ("SERVER", "PUBLIC", "GROUP", "PRIVATE", "TRADE",
                                          "AUCTION", "ERROR", "INFO")}
        a.update(dim=curses.A_DIM, bold=curses.A_BOLD, accent=curses.A_BOLD,
                 good=curses.A_BOLD, bad=curses.A_BOLD, warn=curses.A_BOLD)
        self.district_attr = {d: curses.A_BOLD for d in DISTRICTS}
        self.player_attr = [curses.A_BOLD] * 6
        if not (want and curses.has_colors()):
            a["ERROR"] = a["TRADE"] = curses.A_BOLD
            return a
        try:
            curses.start_color()
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        names = ["cyan", "white", "magenta", "yellow", "green", "red", "blue"]
        pair = {}
        for i, n in enumerate(names, 1):
            curses.init_pair(i, getattr(curses, f"COLOR_{n.upper()}"), bg)
            pair[n] = curses.color_pair(i)
        a.update(SERVER=pair["cyan"] | curses.A_BOLD, PUBLIC=curses.A_NORMAL,
                 GROUP=pair["magenta"], PRIVATE=pair["magenta"] | curses.A_BOLD,
                 TRADE=pair["yellow"] | curses.A_BOLD, AUCTION=pair["green"] | curses.A_BOLD,
                 ERROR=pair["red"] | curses.A_BOLD, INFO=curses.A_NORMAL,
                 accent=pair["cyan"] | curses.A_BOLD, good=pair["green"] | curses.A_BOLD,
                 bad=pair["red"] | curses.A_BOLD, warn=pair["yellow"] | curses.A_BOLD)
        self.district_attr = {"Blue": pair["blue"] | curses.A_BOLD,
                              "Green": pair["green"] | curses.A_BOLD,
                              "Purple": pair["magenta"] | curses.A_BOLD,
                              "Red": pair["red"] | curses.A_BOLD}
        self.player_attr = [pair[n] | curses.A_BOLD
                            for n in ("cyan", "yellow", "green", "magenta", "blue", "red")]
        return a

    def pattr(self, pid) -> int:
        for n, p in enumerate(self.public.get("players", [])):
            if p["id"] == pid:
                return self.player_attr[n % len(self.player_attr)]
        return curses.A_NORMAL

    def pnum(self, pid) -> str:
        for n, p in enumerate(self.public.get("players", []), 1):
            if p["id"] == pid:
                return str(n)
        return "?"

    # ---------------------------------------------------- ClientUI overrides

    def out(self, tag: str, text: str) -> None:
        rnd = self.public.get("round", 0)
        phase = self.public.get("phase", "")
        stamp = f"R{rnd} {phase[:3]}" if rnd else ""
        self.log.append((tag, stamp, text))
        if len(self.log) > 2000:
            del self.log[:500]
        if self.scroll:
            self.unseen += 1
            self.scroll += sum(len(wrap(x, self.log_width)) for x in text.split("\n"))
        self.dirty = True

    def show(self, text: str) -> None:
        self.out("INFO", text)

    def apply_snapshot(self, snap: dict) -> None:
        super().apply_snapshot(snap)
        self.phase_total = max(self.phase_total, float(self.public.get("remaining", 0)))
        self.dirty = True

    def on_event(self, ev: dict) -> None:
        t, d = ev["type"], ev["data"]
        if t in ("_CoordinatorLost", "_BusDisconnected"):
            # Keep the screen (and any final scoreboard) up; the player leaves with /quit.
            self.lost = clean(d["reason"])
            self.out("ERROR", f"{self.lost}. Press Enter or type /quit to exit.")
            return
        if t == "PhaseChanged":
            self.phase_total = float(d["remaining"])
        elif t == "TradeOffered" and d.get("buyer") == self.me and d.get("status") == "PENDING":
            self.alert()
        elif t == "ChatMessage" and d["scope"] != "public" and d["from"] != self.me:
            self.alert()
        super().on_event(ev)
        if t == "MatchEnded":
            self.final_scoreboard(d)
        self.dirty = True

    def final_scoreboard(self, d: dict) -> None:
        lines = []
        for i, s in enumerate(d["scores"], 1):
            mark = self.g["star"] if s["player"] in d["winners"] else " "
            lines.append(f" {mark} {i}. {clean(s['name']):<12} {s['net_worth']:>5}   "
                         f"cash {s['cash']:>5}   props {len(s['properties'])}   "
                         f"districts {','.join(s['districts']) or '-'}")
        winners = ", ".join(self.name(w) for w in d["winners"])
        me_won = self.me in d["winners"]
        lines += ["", f"   Winner{'s' if len(d['winners']) > 1 else ''}: {winners}",
                  "   You won!" if me_won else "",
                  "", "   Esc closes this window. /quit leaves the game."]
        self.final_lines = lines
        self.open_overlay("Final scoreboard  (net worth)", lines)

    def alert(self) -> None:
        try:
            curses.beep()
        except curses.error:
            pass

    async def _command(self, cmd: str, rest: str, args: list[str]) -> None:
        if cmd == "/help":
            self.open_overlay("Help", HELP_LINES)
        elif cmd == "/board" and not args:
            self.show_board = not self.show_board
            self.dirty = True
        elif cmd == "/clear":
            self.log.clear()
            self.scroll = self.unseen = 0
        elif cmd in ("/quit", "/exit") and self.lost:
            self.running = False
        else:
            await super()._command(cmd, rest, args)
        self.dirty = True

    def open_overlay(self, title: str, lines: list[str]) -> None:
        self.overlay = (title, lines)
        self.overlay_top = 0
        self.dirty = True

    # ---------------------------------------------------------------- input

    def on_input(self) -> None:
        while True:
            try:
                key = self.scr.get_wch()
            except curses.error:
                return
            self.handle_key(key)
            self.dirty = True

    def submit(self, line: str) -> None:
        self.queue.put_nowait(line)

    def handle_key(self, key) -> None:
        if key == curses.KEY_RESIZE:
            curses.update_lines_cols()
            return
        if key != "\t":
            self.comp = None
        if self.overlay:
            if key in ("\x1b", "\n", "\r", curses.KEY_ENTER, "q", " "):
                self.overlay = None
            elif key in (curses.KEY_UP, "k"):
                self.overlay_top = max(0, self.overlay_top - 1)
            elif key in (curses.KEY_DOWN, "j"):
                self.overlay_top += 1
            elif key == curses.KEY_PPAGE:
                self.overlay_top = max(0, self.overlay_top - 10)
            elif key == curses.KEY_NPAGE:
                self.overlay_top += 10
            return
        if key in ("\n", "\r", curses.KEY_ENTER):
            line, self.buf, self.cur, self.hist_i = self.buf, "", 0, None
            if line.strip():
                if not self.history or self.history[-1] != line:
                    self.history.append(line)
                self.scroll = self.unseen = 0
                self.submit(line)
            elif self.lost:
                self.running = False
            return
        if key == "\x03":
            self.submit("/quit")
        elif key == "\x04":
            if not self.buf:
                self.submit("/quit")
        elif key == "\x1b":
            self.buf, self.cur = "", 0
        elif key == curses.KEY_F1:
            self.open_overlay("Help", HELP_LINES)
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
            if self.cur:
                self.buf = self.buf[:self.cur - 1] + self.buf[self.cur:]
                self.cur -= 1
        elif key == curses.KEY_DC:
            self.buf = self.buf[:self.cur] + self.buf[self.cur + 1:]
        elif key == curses.KEY_LEFT:
            self.cur = max(0, self.cur - 1)
        elif key == curses.KEY_RIGHT:
            self.cur = min(len(self.buf), self.cur + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            self.cur = 0
        elif key in (curses.KEY_END, "\x05"):
            if self.cur == len(self.buf):
                self.scroll = self.unseen = 0
            self.cur = len(self.buf)
        elif key == "\x15":
            self.buf, self.cur = self.buf[self.cur:], 0
        elif key == "\x17":
            head = self.buf[:self.cur].rstrip()
            cut = head.rfind(" ") + 1
            self.buf, self.cur = self.buf[:cut] + self.buf[self.cur:], cut
        elif key == "\x0c":
            self.scr.clear()
        elif key == curses.KEY_UP:
            self.history_step(-1)
        elif key == curses.KEY_DOWN:
            self.history_step(1)
        elif key == curses.KEY_PPAGE:
            self.scroll += max(1, self.log_rows - 2)
        elif key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - max(1, self.log_rows - 2))
            if not self.scroll:
                self.unseen = 0
        elif key == "\t":
            self.complete()
        elif isinstance(key, str) and key.isprintable():
            if (not self.buf and key in "123" and self.public.get("phase") == "MOVEMENT"):
                self.submit(f"/move {key}")
                return
            self.buf = self.buf[:self.cur] + key + self.buf[self.cur:]
            self.cur += 1

    def history_step(self, step: int) -> None:
        if not self.history:
            return
        i = len(self.history) if self.hist_i is None else self.hist_i
        i = max(0, min(len(self.history), i + step))
        self.hist_i = None if i == len(self.history) else i
        self.buf = "" if self.hist_i is None else self.history[i]
        self.cur = len(self.buf)

    def completions(self, words: list[str], prefix: str) -> list[str]:
        players = self.public.get("players", [])
        others = [clean(p["name"]) for p in players if p["id"] != self.me]
        if len(words) == 0 and prefix.startswith("/"):
            return [c for c in COMMANDS if c.startswith(prefix.lower())]
        cmd = words[0].lower() if words else ""
        cmd = {"/t": "/tell", "/a": "/accept", "/r": "/reject", "/b": "/bid"}.get(cmd, cmd)

        def keyname(i):
            return TILES[i].name.lower().replace(" ", "")
        mine = [keyname(i) for i in next((p["props"] for p in players if p["id"] == self.me), [])]
        if cmd in ("/tell", "/group") and len(words) == 1:
            pool = others
            if cmd == "/group" and "," in prefix:
                head, _, prefix = prefix.rpartition(",")
                return [f"{head},{n}" for n in others if n.lower().startswith(prefix.lower())]
        elif cmd == "/offer":
            pool = others if len(words) == 1 else mine if len(words) == 2 else []
        elif cmd in ("/accept", "/reject", "/canceloffer"):
            pool = list(self.offers)
        elif cmd in ("/buy", "/bid", "/cancelbid") and len(words) == 1:
            auctions = self.public.get("auctions") or []
            pool = [keyname(i) for i in auctions] or [keyname(i) for i in board.PROPERTY_IDS]
        elif cmd == "/board":
            pool = ["list"]
        elif not cmd.startswith("/"):
            pool = others
        else:
            pool = []
        return [x for x in pool if x.lower().startswith(prefix.lower())]

    def complete(self) -> None:
        """Complete the word before the cursor; pressing Tab again cycles the matches."""
        if self.comp:
            start, tail, matches, idx = self.comp
            idx = (idx + 1) % len(matches)
        else:
            head = self.buf[:self.cur]
            start = head.rfind(" ") + 1
            matches = self.completions(head[:start].split(), head[start:])
            if not matches:
                self.alert()
                return
            tail, idx = self.buf[self.cur:], 0
        word = matches[idx] + (" " if len(matches) == 1 else "")
        self.buf = self.buf[:start] + word + tail
        self.cur = start + len(word)
        self.comp = (start, tail, matches, idx) if len(matches) > 1 else None

    # -------------------------------------------------------------- drawing

    def put(self, y: int, x: int, s: str, attr: int = 0, width: Optional[int] = None) -> int:
        """Write clipped text; returns the x after it."""
        H, W = self.scr.getmaxyx()
        if y < 0 or y >= H or x >= W or x < 0:
            return x
        room = W - x - (1 if y == H - 1 else 0)
        if width is not None:
            room = min(room, width)
        s = fit(s, room)
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass
        return x + text_width(s)

    def draw(self) -> None:
        self.dirty = False
        scr = self.scr
        scr.erase()
        H, W = scr.getmaxyx()
        if H < 16 or W < 50:
            self.put(0, 0, f"Terminal too small ({W}x{H}); need at least 50x16.", self.attrs["bad"])
            scr.noutrefresh()
            curses.doupdate()
            return
        self.draw_header(W)
        top = 1
        layout = self.board_layout(H, W) if self.show_board else None
        if layout:
            cw, ch = layout
            bw, bh = 4 * cw + 5, 4 * ch + 5
            self.draw_board(top, 0, cw, ch)
            self.draw_side(top, bw + 2, min(W - bw - 2, 56), bh)
            top += bh
        else:
            rows = min(10, max(5, (H - 6) // 2))
            self.draw_side(top, 1, W - 2, rows)
            top += rows
        self.draw_log(top, H - 2, W)
        self.draw_hint(H - 2, W)
        self.draw_input(H - 1, W)
        if self.overlay:
            self.draw_overlay(H, W)
            curses.curs_set(0)
        scr.noutrefresh()
        curses.doupdate()

    def board_layout(self, H: int, W: int):
        for cw, side in ((13, 28), (12, 25)):
            for ch in (3, 2):
                bw, bh = 4 * cw + 5, 4 * ch + 5
                if W >= bw + 2 + side and H >= 1 + bh + 6 + 2:
                    return cw, ch
        return None

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic()) if self.deadline else 0.0

    def draw_header(self, W: int) -> None:
        a = self.attrs
        bar = curses.A_REVERSE
        self.put(0, 0, " " * W, bar)
        x = self.put(0, 0, f" {self.g['own']} SYNDICATE CITY ", bar | curses.A_BOLD)
        phase = self.public.get("phase", "?")
        rnd, rounds = self.public.get("round", 0), self.public.get("rounds", 0)
        if rnd:
            x = self.put(0, x, f"  Round {rnd}/{rounds}  ", bar)
        desc, key = PHASE_INFO.get(phase, ("", "bold"))
        x = self.put(0, x + 1, f" {phase} ", a.get(key, curses.A_BOLD) | curses.A_REVERSE)
        right = W                      # the countdown (or DISCONNECTED) wins over the description
        if self.lost:
            right = W - 16
            self.put(0, right, " DISCONNECTED ", a["bad"] | curses.A_REVERSE)
        elif phase in ("NEGOTIATION", "MOVEMENT", "AUCTION"):
            left = self.remaining()
            total = max(self.phase_total, left, 1.0)
            urgency = a["bad"] if left <= 5 else a["warn"] if left <= 10 else a["good"]
            n = 16 if W >= 90 else 8
            filled = int(round(n * left / total))
            clock = f" {int(left + 0.999):>3}s "
            right = W - n - len(clock) - 2
            self.put(0, right, clock, bar | curses.A_BOLD)
            self.put(0, right + len(clock), self.g["full"] * filled, urgency)
            self.put(0, right + len(clock) + filled, self.g["empty"] * (n - filled), curses.A_DIM)
        if right - x > 6:
            self.put(0, x, clip(f"  {desc}", right - x - 1, self.g["more"]), bar)

    def draw_board(self, y0: int, x0: int, cw: int, ch: int) -> None:
        from client import RING
        a = self.attrs
        owners = self.public.get("owners", {})
        players = self.public.get("players", [])
        me = next((p for p in players if p["id"] == self.me), None)
        mask: dict[tuple[int, int], int] = {}

        def seg_h(y, xa, xb):
            for x in range(xa, xb + 1):
                m = (L if x > xa else 0) | (R if x < xb else 0)
                mask[y, x] = mask.get((y, x), 0) | m

        def seg_v(x, ya, yb):
            for y in range(ya, yb + 1):
                m = (U if y > ya else 0) | (D if y < yb else 0)
                mask[y, x] = mask.get((y, x), 0) | m

        def box(r, c, rs=1, cs=1):
            ty, lx = y0 + r * (ch + 1), x0 + c * (cw + 1)
            by, rx = ty + rs * (ch + 1), lx + cs * (cw + 1)
            return ty, lx, by, rx

        for (r, c) in RING:
            ty, lx, by, rx = box(r, c)
            seg_h(ty, lx, rx), seg_h(by, lx, rx), seg_v(lx, ty, by), seg_v(rx, ty, by)
        glyphs = UNICODE_BOX if self.unicode else ASCII_BOX

        def paint(points, attr):
            for (y, x) in points:
                m = mask[y, x]
                self.put(y, x, glyphs.get(m, "+"), attr)

        paint(mask, curses.A_DIM)
        # the tile you stand on is outlined in your colour
        cell_of = {v: k for k, v in RING.items()}
        if me:
            ty, lx, by, rx = box(*cell_of[me["pos"]])
            pts = [(y, x) for (y, x) in mask if (y in (ty, by) and lx <= x <= rx)
                   or (x in (lx, rx) and ty <= y <= by)]
            paint(pts, self.pattr(self.me))

        pawns: dict[int, list[dict]] = {}
        for p in players:
            pawns.setdefault(p["pos"], []).append(p)
        for (r, c), t_idx in RING.items():
            ty, lx, _, _ = box(r, c)
            self.draw_tile(ty + 1, lx + 1, cw, ch, TILES[t_idx], owners.get(str(t_idx)),
                           pawns.get(t_idx, []))

        # centre: title and district summary
        ty, lx, by, rx = box(1, 1, 2, 2)
        inner_w = rx - lx - 1
        lines: list[tuple[str, int]] = []
        lines.append(("SYNDICATE CITY", a["accent"]))
        if ch >= 3:
            lines.append(("", 0))
        rows = []
        for dname, tiles in DISTRICTS.items():
            held = {owners.get(str(i)) for i in tiles}
            rows.append((dname, held))
        cy = ty + 1
        for text, attr in lines:
            self.put(cy, lx + 1 + max(0, (inner_w - len(text)) // 2), text, attr)
            cy += 1
        for dname, held in rows:
            x = self.put(cy, lx + 3, self.g["sq"] * 2 + " ", self.district_attr[dname])
            x = self.put(cy, x, f"{dname:<7}", curses.A_NORMAL)
            if len(held) == 1 and None not in held:
                owner = next(iter(held))
                x = self.put(cy, x, fit(self.name(owner), inner_w - 16), self.pattr(owner))
                self.put(cy, x, " " + self.g["star"], a["warn"])
            elif held == {None}:
                self.put(cy, x, "open", curses.A_DIM)
            else:
                self.put(cy, x, "split", curses.A_DIM)
            cy += 1
        if ch >= 3:
            arrow = "clockwise " + self.g["arrow"] if self.unicode else "clockwise ->"
            self.put(cy, lx + 1 + max(0, (inner_w - len(arrow)) // 2), arrow, curses.A_DIM)

    def draw_tile(self, y: int, x: int, cw: int, ch: int, t, owner, here: list[dict]) -> None:
        a = self.attrs
        label = clip(f"{t.index} {t.name}", cw, self.g["more"])
        if t.index in (self.public.get("auctions") or []):
            # up for auction: the name is shown as a solid district-coloured tag
            self.put(y, x, fit(label, cw).ljust(cw), self.district_attr[t.district] | curses.A_REVERSE)
        elif t.is_property:
            self.put(y, x, label, self.district_attr[t.district], cw)
        else:
            self.put(y, x, label, curses.A_BOLD, cw)
        # second line: owner and rent, price, or the special effect
        if t.is_property and owner:
            owner_props = next((p["props"] for p in self.public.get("players", [])
                                if p["id"] == owner), [])
            full = all(i in owner_props for i in DISTRICTS[t.district])
            rent = f"{t.base_rent * (2 if full else 1)}{self.g['star'] if full else ''}"
            if ch < 3:
                rent = ""         # two-line tiles: the pawns need that corner
            name = fit(self.name(owner), cw - len(rent) - 3)
            xx = self.put(y + 1, x, self.g["own"] + " ", self.pattr(owner))
            self.put(y + 1, xx, name, self.pattr(owner) | (curses.A_UNDERLINE
                                                           if owner == self.me else 0))
            self.put(y + 1, x + cw - len(rent), rent, a["warn"] if full else curses.A_DIM)
        elif t.is_property:
            price = f"${t.face_value} r{t.base_rent}" if ch >= 3 else f"${t.face_value}"
            self.put(y + 1, x, price, curses.A_DIM, cw)
        elif t.category == "special":
            self.put(y + 1, x, f"{t.cash_effect:+d} credits",
                     a["good"] if t.cash_effect > 0 else a["bad"], cw)
        else:
            self.put(y + 1, x, f"+{board.START_BONUS} passing", a["good"], cw)
        # pawns: one coloured chip per player standing here
        if here:
            py = y + 2 if ch >= 3 else y + 1
            px = x if ch >= 3 else x + cw - 2 * len(here) + 1
            if ch < 3:
                self.put(py, px - 1, " " * (2 * len(here)))
            for p in here:
                attr = self.pattr(p["id"]) | curses.A_REVERSE
                if not p["connected"]:
                    attr = curses.A_DIM | curses.A_REVERSE
                px = self.put(py, px, self.pnum(p["id"]), attr) + 1

    def worth(self, p: dict) -> int:
        props = set(p["props"])
        bonus = sum(board.DISTRICT_BONUS for tiles in DISTRICTS.values()
                    if all(i in props for i in tiles))
        return p["cash"] + sum(TILES[i].face_value for i in props) + bonus

    def draw_side(self, y0: int, x0: int, w: int, h: int) -> None:
        a = self.attrs
        players = self.public.get("players", [])
        me = next((p for p in players if p["id"] == self.me), None)
        rows: list[list[tuple[str, int]]] = []    # each row = list of (text, attr) spans

        def head(title, extra=""):
            rule = "─" if self.unicode else "-"
            text = f"{title} "
            rows.append([(text, a["accent"]),
                         (extra, curses.A_DIM),
                         (rule * max(0, w - len(text) - len(extra)), curses.A_DIM)])

        if me:
            reserved = sum(self.bids.values())
            free = max(0, me["cash"] - reserved)
            head("YOU", f"{self.name(self.me)} on {TILES[me['pos']].name} ")
            cash_attr = a["bad"] if me["cash"] < 0 else curses.A_BOLD
            line = [("cash ", curses.A_DIM), (f"{me['cash']}", cash_attr),
                    ("  worth ", curses.A_DIM), (f"{self.worth(me)}", curses.A_BOLD)]
            bids = [("bids ", curses.A_DIM), (f"{reserved}", a["AUCTION"]),
                    ("  free ", curses.A_DIM), (f"{free}", curses.A_BOLD)]
            if reserved and w >= 44:
                line = line[:2] + [("  ", 0)] + bids + line[2:]
                rows.append(line)
            else:
                rows.append(line)
                if reserved:
                    rows.append(bids)
            if self.public.get("phase") == "MOVEMENT":
                line = [("move  ", curses.A_DIM)]
                for n in (1, 2, 3):
                    chosen = self.movement == n
                    line += [(f" {n} ", (self.pattr(self.me) | curses.A_REVERSE) if chosen
                              else curses.A_DIM), (" ", 0)]
                line.append(("secret" if self.movement else "press 1-3 (default 1)",
                             curses.A_DIM if self.movement else a["warn"]))
                rows.append(line)

        incoming = [o for o in self.offers.values() if o["buyer"] == self.me]
        outgoing = [o for o in self.offers.values() if o["seller"] == self.me]
        if incoming or outgoing:
            head("OFFERS")
            for o in incoming:
                rows.append([(f"{o['offer_id']} ", a["TRADE"]),
                             (self.name(o["seller"]), self.pattr(o["seller"])),
                             (f" sells you {TILES[o['property_id']].name} for ", 0),
                             (f"{o['price']}", curses.A_BOLD)])
            for o in outgoing:
                rows.append([(f"{o['offer_id']} ", curses.A_DIM),
                             (f"you {self.g['arrow']} ", curses.A_DIM),
                             (self.name(o["buyer"]), self.pattr(o["buyer"])),
                             (f"  {TILES[o['property_id']].name} {o['price']}", curses.A_DIM)])

        auctions = self.public.get("auctions") or []
        if auctions and self.public.get("phase") in ("AUCTION", "SETTLEMENT"):
            head("FOR SALE", "     price  you ")
            for t in auctions:
                tile = TILES[t]
                bid = self.bids.get(t)
                contested, price, _ = self.sale(t)
                rows.append([(f"{t:>2} ", curses.A_BOLD),
                             (f"{tile.name:<10}", self.district_attr[tile.district]),
                             (f" {'>=' if contested else '  '}{price:>3}", curses.A_DIM),
                             (f"  {bid:>3}" if bid else "    -",
                              a["AUCTION"] if bid else curses.A_DIM)])

        if players:
            head("PLAYERS", "      cash  worth ")       # aligned over the columns below
            prio = self.public.get("priority", [])
            for n, p in enumerate(players, 1):
                attr = self.pattr(p["id"])
                if not p["connected"]:
                    attr = curses.A_DIM
                name = fit(clean(p["name"]), 10)
                tag = " you" if p["id"] == self.me else "" if p["connected"] else " off"
                props = f"{self.g['own']}{len(p['props'])}" if p["props"] else ""
                rows.append([(f"{n}", attr | curses.A_REVERSE), (" ", 0),
                             (f"{name:<10}", attr),
                             (f"{p['cash']:>6} {self.worth(p):>6} ", curses.A_NORMAL),
                             (f"{props:<3}", curses.A_DIM), (tag, curses.A_DIM)])
            if prio:
                arrow = " > "
                rows.append([("ties: ", curses.A_DIM),
                             (arrow.join(self.name(x) for x in prio), curses.A_DIM)])

        if me and me["props"]:
            head("PROPERTIES")
            props = set(me["props"])
            for i in sorted(props):
                tile = TILES[i]
                full = all(j in props for j in DISTRICTS[tile.district])
                rows.append([(f"{i:>2} ", curses.A_BOLD),
                             (f"{tile.name:<10}", self.district_attr[tile.district]),
                             (f" ${tile.face_value}", curses.A_DIM),
                             (f"  rent {tile.base_rent * (2 if full else 1)}",
                              a["warn"] if full else curses.A_DIM),
                             (f"  {tile.district} complete" if full else "", a["warn"])])

        for i, spans in enumerate(rows[:h]):
            x = x0
            for text, attr in spans:
                if x >= x0 + w:
                    break
                x = self.put(y0 + i, x, text, attr, x0 + w - x)

    def log_lines(self, entries, width: int) -> list[list[tuple[str, int]]]:
        out = []
        for tag, stamp, text in entries:
            if tag == "INFO":
                for raw in text.split("\n"):
                    for piece in wrap(raw, width):
                        out.append([(piece, curses.A_NORMAL)])
                continue
            label = f"{tag:<7} "
            pre = [(f"{stamp:<7} " if stamp is not None else "", curses.A_DIM),
                   (label, self.attrs.get(tag, 0))]
            indent = len(pre[0][0]) + len(label)
            body = self.attrs["ERROR"] if tag == "ERROR" else \
                curses.A_BOLD if tag in ("TRADE", "PRIVATE") else curses.A_NORMAL
            first = True
            for raw in text.split("\n"):
                for piece in wrap(raw, max(10, width - indent)):
                    if first:
                        out.append(pre + [(piece, body)])
                        first = False
                    else:
                        out.append([(" " * indent, 0), (piece, body)])
        return out

    def draw_log(self, top: int, bottom: int, W: int) -> None:
        a = self.attrs
        split = W >= 110
        chat_w = max(36, int(W * 0.38)) if split else 0
        ev_w = W - chat_w - (1 if split else 0)
        rows = bottom - top - 1
        self.log_rows = rows
        self.log_width = ev_w - 1
        rule = "─" if self.unicode else "-"
        # section rule
        title = " EVENTS " if split else " LOG "
        self.put(top, 0, rule * W, curses.A_DIM)
        self.put(top, 1, title, a["accent"])
        if self.scroll:
            note = (f" scrolled up, {self.unseen} new - End jumps back " if self.unseen
                    else " scrolled up - End jumps back ")
            self.put(top, ev_w - len(note) - 2, note, a["warn"])
        if split:
            self.put(top, ev_w + 1, " CHAT ", a["accent"])
            self.put(top, ev_w + 7, "public, private and group ", curses.A_DIM)
        events = [e for e in self.log if not (split and e[0] in CHAT_TAGS)]
        lines = self.log_lines(events, ev_w - 1)
        max_scroll = max(0, len(lines) - rows)
        self.scroll = min(self.scroll, max_scroll)
        if not self.scroll:
            self.unseen = 0
        end = len(lines) - self.scroll
        view = lines[max(0, end - rows):end]
        for i, spans in enumerate(view):
            x = 0
            for text, attr in spans:
                x = self.put(top + 1 + i, x, text, attr, ev_w - x)
        if not self.log:
            self.put(top + 2, 2, "Events and chat will appear here.", curses.A_DIM)
        if split:
            vbar = "│" if self.unicode else "|"
            for y in range(top + 1, bottom):
                self.put(y, ev_w, vbar, curses.A_DIM)
            chat = [e for e in self.log if e[0] in CHAT_TAGS]
            clines = self.log_lines([(t, None, x) for t, _, x in chat], chat_w - 2)
            view = clines[-rows:]
            for i, spans in enumerate(view):
                x = ev_w + 2
                for text, attr in spans:
                    x = self.put(top + 1 + i, x, text, attr, W - x)
            if not chat:
                self.put(top + 2, ev_w + 3, "No messages yet.", curses.A_DIM)

    def hint(self) -> tuple[str, int]:
        a = self.attrs
        phase = self.public.get("phase")
        if self.lost:
            return f"Disconnected: {self.lost}. Press Enter to exit.", a["bad"]
        incoming = [o for o in self.offers.values() if o["buyer"] == self.me]
        if incoming and phase == "NEGOTIATION":
            o = incoming[-1]
            return (f"{self.name(o['seller'])} offers you {TILES[o['property_id']].name} for "
                    f"{o['price']}:  /accept {o['offer_id']}  or  /reject {o['offer_id']}",
                    a["TRADE"])
        if phase == "MOVEMENT":
            if self.movement:
                return (f"Moving {self.movement} (secret). Press 1, 2 or 3 to change it.",
                        a["PRIVATE"])
            return "Press 1, 2 or 3 to choose your move (secret). No choice = 1.", a["warn"]
        if phase == "AUCTION":
            mine = [t for t in self.public.get("auctions") or [] if self.me in self.sale(t)[2]]
            if mine:
                t = mine[0]
                contested, price, landers = self.sale(t)
                if contested:
                    rivals = ", ".join(self.name(x) for x in landers if x != self.me)
                    return (f"Sealed bid vs {rivals}: /bid {t} <amount> (min {price})"
                            f"   /cancelbid {t} withdraws", a["AUCTION"])
                return (f"Buy {TILES[t].name} for {price}: /buy {t}"
                        f"   /cancelbid {t} withdraws", a["AUCTION"])
            return "Nothing for you to buy this round. Type to chat.", curses.A_DIM
        if phase == "NEGOTIATION":
            return ("Type to chat.  /tell <name> msg   /offer <buyer> <property> <price>   "
                    "Tab completes", curses.A_DIM)
        if phase == "LOBBY":
            return "Waiting for the operator to start the match...", curses.A_DIM
        if phase == "ENDED":
            return "Match over. /quit to leave.", curses.A_DIM
        return "", curses.A_DIM

    def draw_hint(self, y: int, W: int) -> None:
        text, attr = self.hint()
        keys = "  F1 help  PgUp/PgDn scroll "
        if W - len(keys) > text_width(text) + 3:
            self.put(y, W - len(keys), keys, curses.A_DIM)
        self.put(y, 1, clip(text, W - 2, self.g["more"]), attr)

    def draw_input(self, y: int, W: int) -> None:
        prompt = f"{self.g['prompt']} "
        attr = self.pattr(self.me) if self.me else curses.A_BOLD
        x = self.put(y, 0, prompt, attr)
        room = W - x - 1
        if not self.buf:
            ph = {"MOVEMENT": "press 1, 2 or 3",
                  "AUCTION": "/buy <tile>, /bid <tile> <amount>, or type to chat"}.get(
                self.public.get("phase"), "type a message, or / for commands (Tab completes)")
            self.put(y, x, ph, curses.A_DIM, room)
            cx = x
        else:
            start = max(0, self.cur - room + 1)
            self.put(y, x, self.buf[start:start + room], curses.A_NORMAL, room)
            cx = x + text_width(self.buf[start:self.cur])
        try:
            curses.curs_set(1)
            self.scr.move(y, min(cx, W - 2))
        except curses.error:
            pass

    def draw_overlay(self, H: int, W: int) -> None:
        title, lines = self.overlay
        bw = min(W - 4, max(len(title) + 6, max((text_width(s) for s in lines), default=0) + 4))
        bh = min(H - 2, len(lines) + 4)
        y0, x0 = (H - bh) // 2, (W - bw) // 2
        body = bh - 4
        self.overlay_top = max(0, min(self.overlay_top, len(lines) - body))
        g = UNICODE_BOX if self.unicode else {}
        h, v = g.get(L | R, "-"), g.get(U | D, "|")
        tl, tr, bl, br = (g.get(D | R, "+"), g.get(D | L, "+"), g.get(U | R, "+"),
                          g.get(U | L, "+"))
        frame = self.attrs["accent"]
        self.put(y0, x0, tl + h * (bw - 2) + tr, frame)
        self.put(y0, x0 + 2, f" {title} ", frame)
        for i in range(1, bh - 1):
            self.put(y0 + i, x0, v + " " * (bw - 2) + v, frame)
            self.put(y0 + i, x0 + 1, " " * (bw - 2), 0)
        self.put(y0 + bh - 1, x0, bl + h * (bw - 2) + br, frame)
        for i, s in enumerate(lines[self.overlay_top:self.overlay_top + body]):
            heading = s and not s.startswith(" ") and s.split("  ")[0].isupper()
            attr = self.attrs["accent"] if heading else curses.A_NORMAL
            self.put(y0 + 2 + i, x0 + 2, s, attr, bw - 4)
        more = len(lines) > body
        foot = " Esc closes " + (" Up/Down scroll " if more else "")
        self.put(y0 + bh - 1, x0 + bw - len(foot) - 2, foot, curses.A_DIM)

    # ----------------------------------------------------------------- loop

    async def worker(self) -> None:
        while self.running:
            line = await self.queue.get()
            await self.command(line)
            self.dirty = True

    async def pump_events(self) -> None:
        async for ev in self.conn.events():
            self.on_event(ev)
            if not self.running:
                return

    async def main(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_reader(sys.stdin.fileno(), self.on_input)
        try:
            loop.add_signal_handler(signal.SIGINT, lambda: self.submit("/quit"))
        except (NotImplementedError, RuntimeError):
            pass
        tasks = [asyncio.ensure_future(self.pump_events()), asyncio.ensure_future(self.worker())]
        last_second = -1
        try:
            while self.running:
                self.on_input()            # also picks up KEY_RESIZE between keystrokes
                sec = int(self.remaining())
                if self.dirty or sec != last_second:
                    last_second = sec
                    self.draw()
                await asyncio.sleep(0.05)
        finally:
            loop.remove_reader(sys.stdin.fileno())
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError):
                pass
            for t in tasks:
                t.cancel()


class _LogToUI(logging.Handler):
    def __init__(self, ui: TuiUI):
        super().__init__(logging.WARNING)
        self.ui = ui

    def emit(self, record):
        self.ui.out("ERROR", clean(record.getMessage()))


async def run(conn: ipc.ClientIPC, snap: dict, color: bool = True) -> int:
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    scr = curses.initscr()
    ui = None
    root = logging.getLogger()
    handler = None
    try:
        curses.noecho()
        curses.cbreak()
        scr.keypad(True)
        scr.nodelay(True)
        ui = TuiUI(conn, scr, color)
        handler = _LogToUI(ui)
        root.addHandler(handler)
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, ctx: ui.out("ERROR", clean(ctx.get("message", "internal error"))))
        ui.apply_snapshot(snap)
        ui.out("SERVER", f"Joined as {ui.name(ui.me)} ({ui.me}); coordinator "
                         f"{clean(conn.coordinator)}. Press F1 for help.")
        await ui.main()
    finally:
        if handler:
            root.removeHandler(handler)
        try:
            scr.keypad(False)
            curses.nocbreak()
            curses.echo()
        finally:
            curses.endwin()
    await conn.close()
    if ui and ui.final_lines:
        print("Final scoreboard (net worth):")
        print("\n".join(line for line in ui.final_lines[:-1] if line.strip()))
    if ui and ui.lost:
        print(ui.lost)
    return 0
