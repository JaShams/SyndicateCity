"""Command logs: the ordered record of every input that changes game state.

The coordinator never mutates the game directly. Each state change -- a player
request, a disconnect, the operator's ``start``, a phase deadline passing -- is
a *command* (a small JSON-able dict carrying the game time ``now`` at which it
happens). A command log puts commands in one order and calls
``apply(cmd, deliver)`` for each exactly once, in that order, on the event loop.

Because ``game.py`` is deterministic (no randomness, no clock reads, no
iteration over hash-ordered sets), the same commands in the same order rebuild
the same state. That gives:

* :class:`MemoryLog` -- no persistence; the original single-process behaviour.
* :class:`FileLog`   -- append-only JSONL, fsync'd before a command takes effect;
  a restarted coordinator replays it and carries on mid-round.
* :class:`RaftLog`   -- the log replicated over a Raft cluster (pysyncobj); every
  replica applies the same commands, only the leader serves players.

``apply`` returns the command's *outcome* (``("ok", result)`` or
``("err", code, message)``), which ``submit`` hands back to the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Callable, Optional

log = logging.getLogger("syndicate.cmdlog")

Apply = Callable[[dict, bool], tuple]


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _canonical(obj):
    return json.loads(json.dumps(obj, sort_keys=True))


class NotLeader(Exception):
    """This replica cannot order commands right now (it is not the Raft leader)."""


class GameClock:
    """Game time, in seconds, that survives restarts and fail-overs.

    Every command carries the game time at which the leader accepted it. A
    coordinator that takes over (after a restart or an election) continues from
    the last committed value, so the clock stands still while no coordinator is
    running and phase timers resume where they stopped.
    """

    def __init__(self, source: Callable[[], float] = time.monotonic):
        self._source = source
        self._base = 0.0
        self._anchor = source()
        self.last = 0.0
        self.running = True

    def now(self) -> float:
        if not self.running:              # a follower: time moves only with the log
            return self.last
        return max(self.last, self._base + (self._source() - self._anchor))

    def observe(self, t: float) -> None:
        """Note a committed command's time (time never runs backwards)."""
        if t > self.last:
            self.last = t

    def resume(self) -> None:
        """Continue counting from the latest committed time."""
        self._base = self.last
        self._anchor = self._source()
        self.running = True

    def freeze(self) -> None:
        """Stop counting locally (this process no longer orders commands)."""
        self.running = False


class MemoryLog:
    """No persistence: commands are applied immediately (state dies with the process)."""

    kind = "memory"

    def __init__(self):
        self._apply: Optional[Apply] = None

    def bind(self, apply: Apply) -> None:
        self._apply = apply

    def open(self, header: dict) -> tuple[dict, list[dict]]:
        """Return ``(header, history)``: the match header to use and commands to replay."""
        return header, []

    @property
    def is_leader(self) -> bool:
        return True

    async def submit(self, cmd: dict) -> tuple:
        return self._apply(cmd, True)

    def status(self) -> str:
        return "log: in memory only (state is lost if this process dies)"

    async def close(self) -> None:
        pass


class FileLog(MemoryLog):
    """Append-only JSONL on local disk; each command is fsync'd before it is applied.

    The first line is a header naming the match, roster and settings. Opening an
    existing log for a different roster or settings is refused, so a stale file
    can never be replayed into the wrong match. A torn final line (a crash in
    the middle of a write) is discarded: that command never took effect, and its
    caller never got an answer, so it simply retries.
    """

    kind = "file"

    def __init__(self, path: str, fsync: bool = True):
        super().__init__()
        self.path = path
        self._fsync = fsync
        self._fd: Optional[int] = None
        self.count = 0

    def open(self, header: dict) -> tuple[dict, list[dict]]:
        history: list[dict] = []
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            with open(self.path, "rb") as fh:
                data = fh.read()
            good = data.rfind(b"\n") + 1            # bytes up to the last complete line
            if good < len(data):
                log.warning("discarding torn final record in %s", self.path)
            lines = data[:good].splitlines()
            stored = json.loads(lines[0])
            for key in ("roster", "settings"):
                if _canonical(stored.get(key)) != _canonical(header.get(key)):
                    raise ValueError(f"{self.path} belongs to a match with a different {key}; "
                                     "move it away to start a new match")
            history = [json.loads(x) for x in lines[1:]]
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
            if good < len(data):
                os.ftruncate(fd, good)
            header = stored
        else:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            os.write(fd, _line(header))
            os.fsync(fd)
        self._fd = fd
        self.count = len(history)
        return header, history

    async def submit(self, cmd: dict) -> tuple:
        os.write(self._fd, _line(cmd))
        if self._fsync:
            os.fsync(self._fd)          # durable before anyone sees the effect
        self.count += 1
        return self._apply(cmd, True)

    def status(self) -> str:
        return f"log: {self.path} ({self.count} commands, fsync {'on' if self._fsync else 'off'})"

    async def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


COMMIT_TIMEOUT = 4.0          # below the client's 5 s request timeout


class RaftLog(MemoryLog):
    """The command log replicated across coordinators with Raft (pysyncobj).

    Every replica holds the same ordered list of commands (a pysyncobj
    ``ReplList``) and applies new entries, in order, on its asyncio loop -- the
    Raft thread never touches the game. Exactly one replica serves at a time:

    1. pysyncobj elects a leader.
    2. The new leader appends a ``leader`` barrier command and waits for it to be
       applied locally. Raft commits entries in order, so by then every command
       from earlier terms has been applied here too.
    3. Only then does ``on_leader`` run: the coordinator resumes the game clock,
       opens its listener and serves players. Requests become commands that
       commit once a majority of replicas has stored them.
    4. If leadership is lost, ``on_follower`` closes player connections and
       freezes the clock; players fail over to whichever replica wins next.

    Traffic between replicas is pickled by pysyncobj, so a cluster password is
    mandatory: pysyncobj then encrypts and authenticates every message (Fernet)
    and drops anything not sealed with the shared key *before* unpickling it.
    """

    kind = "raft"

    def __init__(self, self_addr: str, partners: list[str], password: str,
                 state_dir: Optional[str] = None, node: Optional[str] = None,
                 now: Callable[[], float] = lambda: 0.0, digest: str = ""):
        super().__init__()
        if not password or len(password) < 16:
            raise ValueError("a cluster password of at least 16 characters is required")
        from pysyncobj import SyncObj, SyncObjConf
        from pysyncobj.batteries import ReplList

        self.node = node or self_addr
        self.self_addr = self_addr
        self.partners = list(partners)
        self._now = now
        self.digest = digest
        self._list = ReplList()
        extra = {}
        if state_dir:
            os.makedirs(state_dir, mode=0o700, exist_ok=True)
            base = os.path.join(state_dir, "raft-" + self_addr.replace(":", "_"))
            extra = {"journalFile": base + ".journal", "fullDumpFile": base + ".dump"}
        conf = SyncObjConf(password=password, dynamicMembershipChange=False,
                           appendEntriesPeriod=0.05, raftMinTimeout=0.4, raftMaxTimeout=1.0,
                           connectionTimeout=1.0, connectionRetryTime=0.5,
                           leaderFallbackTimeout=1.2, commandsWaitLeader=False, **extra)
        self._obj = SyncObj(self_addr, self.partners, conf, consumers=[self._list])
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._pending: dict[str, asyncio.Future] = {}
        self._counter = 0
        self._serving = False
        self._promoting: Optional[asyncio.Task] = None
        self.applied = 0
        self.terms_led = 0
        self.on_leader: Callable[[], "asyncio.Future"] = self._noop
        self.on_follower: Callable[[], "asyncio.Future"] = self._noop

    @staticmethod
    async def _noop() -> None:
        return None

    def start(self) -> None:
        """Begin applying committed entries and watching for leadership (on the running loop)."""
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.ensure_future(self._run())

    @property
    def is_leader(self) -> bool:
        return self._serving

    def leader(self) -> Optional[str]:
        lead = self._obj._getLeader()
        return None if lead is None else str(lead.address if hasattr(lead, "address") else lead)

    # -- ordering ----------------------------------------------------------------

    async def _append(self, cmd: dict) -> tuple:
        self._counter += 1
        cid = f"{self.node}#{self.terms_led}#{self._counter}"
        cmd["cid"] = cid
        fut = self._loop.create_future()
        self._pending[cid] = fut
        loop = self._loop

        def committed(_result, error):       # runs on the pysyncobj thread
            loop.call_soon_threadsafe(self._on_committed, cid, error)

        try:
            self._list.append(cmd, callback=committed)
            return await asyncio.wait_for(fut, COMMIT_TIMEOUT)
        except asyncio.TimeoutError:
            raise NotLeader("no quorum: the command was not committed in time") from None
        finally:
            self._pending.pop(cid, None)

    def _on_committed(self, cid: str, error) -> None:
        from pysyncobj import FAIL_REASON
        if error == FAIL_REASON.SUCCESS:
            self._wake.set()                 # the applier resolves the future
            return
        fut = self._pending.get(cid)
        if fut is not None and not fut.done():
            fut.set_exception(NotLeader(f"command rejected by Raft ({error})"))

    async def submit(self, cmd: dict) -> tuple:
        if not self._serving:
            raise NotLeader(f"not the leader; try {self.leader() or 'another replica'}")
        return await self._append(cmd)

    # -- applying ----------------------------------------------------------------

    def _drain(self) -> None:
        data = self._list.rawData()           # the replicated list; only ever appended to
        n = len(data)
        while self.applied < n:
            cmd = data[self.applied]
            self.applied += 1
            try:
                outcome = self._apply(cmd, self._serving)
            except Exception as exc:  # noqa: BLE001 -- one bad entry must not stop the replica
                log.exception("applying %s failed", cmd.get("k"))
                outcome = ("err", "INTERNAL", f"internal error ({exc.__class__.__name__})")
            fut = self._pending.pop(cmd.get("cid"), None)
            if fut is not None and not fut.done():
                fut.set_result(outcome)

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 0.05)
            self._wake.clear()
            self._drain()
            raft_leader = self._obj._isLeader()
            if raft_leader and not self._serving and self._promoting is None:
                self._promoting = asyncio.ensure_future(self._promote())
            elif not raft_leader and (self._serving or self._promoting is not None):
                await self._demote()

    async def _promote(self) -> None:
        try:
            self.terms_led += 1
            await self._append({"k": "leader", "node": self.node, "digest": self.digest,
                               "now": round(self._now(), 6)})
            self._drain()
            if not self._obj._isLeader():
                return
            self._serving = True
            log.info("%s is now the leader", self.node)
            await self.on_leader()
        except NotLeader:
            pass
        finally:
            self._promoting = None

    async def _demote(self) -> None:
        if self._promoting is not None:
            self._promoting.cancel()
            self._promoting = None
        was_serving, self._serving = self._serving, False
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(NotLeader("leadership lost"))
        self._pending.clear()
        if was_serving:
            log.info("%s stepped down", self.node)
            await self.on_follower()

    def status(self) -> str:
        st = self._obj.getStatus()
        role = {0: "follower", 1: "candidate", 2: "leader"}.get(st.get("state"), "?")
        return (f"raft: node {self.node} {role}{' (serving)' if self._serving else ''}, "
                f"leader {self.leader()}, term {st.get('raft_term')}, "
                f"commit {st.get('commit_idx')}, applied {self.applied} commands, "
                f"partners {len(self.partners)}")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._promoting is not None:
            self._promoting.cancel()
        self._serving = False
        await asyncio.get_running_loop().run_in_executor(None, self._obj.destroy_synchronous)
