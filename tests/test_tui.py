"""The full-screen client (client.py --tui): text helpers, plus a real match in a pty."""

import asyncio
import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import threading
import time

import ipc
from helpers import ROOT, Stack
from tui import clip, fit, text_width, wrap


def test_text_helpers():
    assert fit("Dockyard", 4) == "Dock"
    assert clip("Supply Drop", 8) == "Supply …"
    assert clip("Midtown", 8) == "Midtown"
    assert text_width("日本") == 4 and fit("日本x", 3) == "日"
    assert wrap("one two three four", 9) == ["one two", "three", "four"]
    assert wrap("abcdefghij", 4) == ["abcd", "efgh", "ij"]


def set_size(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def test_tui_plays_a_match_in_a_pty(private_bus):
    """Alice plays from the TUI: presses 2 in MOVEMENT, survives a resize, quits cleanly."""
    async def body():
        s = Stack(private_bus)
        await s.start(rounds=1, neg=1.5, mov=2.5, auc=1.5)
        pid_to_uid = {}

        async def resolver(name):
            if name in s.fake_uids:
                return s.fake_uids[name]
            reply = await ipc._driver_call(s.server._bus, "GetConnectionUnixProcessID", "s", [name])
            return pid_to_uid[int(reply[0])]
        s.server._resolve_uid = resolver

        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update(TERM="xterm-256color", LC_ALL="C.UTF-8")
            os.execv(sys.executable, [sys.executable, os.path.join(ROOT, "client.py"),
                                      "--bus-address", private_bus.address, "--tui"])
        pid_to_uid[pid] = 50000          # alice's slot
        set_size(fd, 30, 100)
        output = bytearray()

        def pump():
            while True:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                output.extend(data)
        threading.Thread(target=pump, daemon=True).start()

        async def until(pred, timeout=15.0):
            end = time.time() + timeout
            while not pred():
                assert time.time() < end, bytes(output[-3000:]).decode(errors="replace")
                await asyncio.sleep(0.05)

        g = s.coord.game
        for p in ("p2", "p3", "p4"):
            await s.connect(p)
        await until(lambda: g.players["p1"].connected)
        await s.start_match()
        await until(lambda: g.phase.value == "MOVEMENT")
        await asyncio.sleep(0.5)                  # let the client see the phase change
        os.write(fd, b"2")
        set_size(fd, 24, 80)
        os.kill(pid, signal.SIGWINCH)
        await until(lambda: g.phase.value == "ENDED")
        assert g.players["p1"].position == 2      # the keypress reached the coordinator
        await asyncio.sleep(0.5)
        os.write(fd, b"\x1b")                     # close the scoreboard window
        await asyncio.sleep(0.2)
        os.write(fd, b"/quit\r")
        for _ in range(100):
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            await asyncio.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
            raise AssertionError("TUI did not exit after /quit")
        assert os.waitstatus_to_exitcode(status) == 0
        await asyncio.sleep(0.2)
        text = bytes(output).decode(errors="replace")
        assert "SYNDICATE CITY" in text and "MOVEMENT" in text
        assert "Final scoreboard" in text          # printed again after the screen is restored
        assert "Traceback" not in text
        await s.close()

    asyncio.run(asyncio.wait_for(body(), 60))
