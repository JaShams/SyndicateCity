"""Syndicate City communications library -- the entire IPC layer in one file.

Every D-Bus and network interaction of the game goes through this module;
``server.py`` and ``client.py`` never import ``dbus_next`` or ``websockets``
themselves. The library knows nothing about game rules.

Two transports share one envelope format, one request pipeline and one API;
the address picks between them:

* ``unix:path=...`` -- the dedicated local D-Bus bus (identity = Unix UID that
  the bus daemon verified with SO_PEERCRED);
* ``wss://host:port[,wss://host2:port...]`` -- WebSocket over TLS for players on
  other machines (identity = HMAC-signed token naming a roster slot), with
  client fail-over across replicated coordinators.

For the D-Bus transport it provides:

* a dedicated-bus connection helper (never the session or system bus);
* a coordinator endpoint that owns ``org.syndicate.City`` exclusively, accepts
  one versioned JSON ``Request(s) -> s`` method, and binds each caller's
  bus-assigned unique name to an authenticated Unix UID and roster slot;
* event delivery in three shapes:
    - ``broadcast``  -- one signal without destination (public data only),
    - ``send_to``    -- one signal *addressed* to one player's unique name,
    - ``multicast``  -- fan-out of individually addressed signals (groups);
* a player endpoint that issues requests with timeouts and yields only events
  whose D-Bus sender is the verified coordinator connection.

Wire format
-----------
Request  (string arg of ``org.syndicate.City1.Request``)::

    {"v": 1, "id": "<1-64 chars>", "action": "SubmitBid", "payload": {...}}

Response (string return value)::

    {"v": 1, "id": "...", "ok": true,  "result": {...}}
    {"v": 1, "id": "...", "ok": false, "error": {"code": "...", "message": "..."}}

Event    (string arg of the ``org.syndicate.City1.GameEvent`` signal)::

    {"v": 1, "seq": 117, "pseq": 80, "type": "AuctionClosed",
     "visibility": "public", "round": 3, "data": {...}}

``seq`` increases for every event the coordinator emits; ``pseq`` is present on
public events only and increases by exactly one per broadcast, so a client can
detect missed public updates.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import ssl
import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Iterable, Optional
from urllib.parse import urlparse

from dbus_next import BusType, Message, MessageType
from dbus_next.aio import MessageBus
from dbus_next.constants import NameFlag, RequestNameReply
from dbus_next.errors import DBusError
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve

__all__ = [
    "BUS_NAME", "OBJECT_PATH", "INTERFACE", "PROTOCOL_VERSION",
    "MAX_REQUEST_BYTES", "MAX_RESPONSE_BYTES", "MAX_EVENT_BYTES",
    "IPCError", "RequestError", "Caller", "Request",
    "GameIPC", "ServerIPC", "ClientIPC", "read_bus_address",
    "DBusServerIPC", "DBusClientIPC", "NetServerIPC", "NetClientIPC",
    "mint_token", "verify_token", "read_private_file", "is_network_address",
]

BUS_NAME = "org.syndicate.City"
OBJECT_PATH = "/org/syndicate/City"
INTERFACE = "org.syndicate.City1"
REQUEST_METHOD = "Request"
EVENT_SIGNAL = "GameEvent"
PROTOCOL_VERSION = 1

MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 4096
MAX_EVENT_BYTES = 16384
MAX_ID_CHARS = 64
MAX_ACTION_CHARS = 64
DEFAULT_TIMEOUT = 5.0
CLIENT_EVENT_QUEUE = 1000
SERVER_OUTBOX = 5000

_DBUS = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")

log = logging.getLogger("syndicate.ipc")


# --------------------------------------------------------------------- errors

class IPCError(Exception):
    """Transport-level failure: cannot connect, timeout, coordinator gone."""


class RequestError(Exception):
    """An application-level rejection carried in an ``ok: false`` response.

    Coordinator handlers raise it; :meth:`ClientIPC.request` re-raises it.
    """

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ----------------------------------------------------------------- data types

@dataclass(frozen=True)
class Caller:
    """Identity of a request's sender, established by the bus, never the payload."""
    unique_name: str
    uid: int
    player_id: Optional[str]


@dataclass(frozen=True)
class Request:
    id: str
    action: str
    payload: dict
    fingerprint: str          # canonical JSON of (action, payload), for replay detection


def _dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def _encode_bounded(obj, limit: int) -> Optional[str]:
    text = _dumps(obj)
    return text if len(text.encode("utf-8")) <= limit else None


def parse_request(raw, allowed_actions: Optional[frozenset] = None) -> Request:
    """Strictly validate a request envelope. Raises :class:`RequestError`."""
    if not isinstance(raw, str):
        raise RequestError("BAD_REQUEST", "request must be a string")
    if len(raw.encode("utf-8", "surrogatepass")) > MAX_REQUEST_BYTES:
        raise RequestError("TOO_LARGE", f"request exceeds {MAX_REQUEST_BYTES} bytes")
    try:
        obj = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise RequestError("BAD_JSON", f"malformed JSON: {exc.__class__.__name__}") from None
    if not isinstance(obj, dict):
        raise RequestError("BAD_REQUEST", "request must be a JSON object")
    if set(obj) != {"v", "id", "action", "payload"}:
        raise RequestError("BAD_REQUEST", "request must have exactly v, id, action, payload")
    if type(obj["v"]) is not int or obj["v"] != PROTOCOL_VERSION:
        raise RequestError("BAD_VERSION", f"unsupported protocol version (expected {PROTOCOL_VERSION})")
    rid = obj["id"]
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        raise RequestError("BAD_REQUEST", "id must be 1-64 chars of [A-Za-z0-9._:-]")
    action = obj["action"]
    if not isinstance(action, str) or not _ACTION_RE.match(action):
        raise RequestError("BAD_REQUEST", "malformed action name")
    if allowed_actions is not None and action not in allowed_actions:
        raise RequestError("UNKNOWN_ACTION", f"action {action!r} is not allowed")
    if not isinstance(obj["payload"], dict):
        raise RequestError("BAD_REQUEST", "payload must be a JSON object")
    return Request(rid, action, obj["payload"], _dumps([action, obj["payload"]]))


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant {name}")


def _error_response(rid, code: str, message: str) -> str:
    return _dumps({"v": PROTOCOL_VERSION, "id": rid, "ok": False,
                   "error": {"code": code, "message": message[:300]}})


def read_bus_address(path: str) -> str:
    """Read the dedicated bus address written by the launch script."""
    with open(path, "r", encoding="utf-8") as fh:
        addr = fh.read().strip()
    if not addr.startswith("unix:"):
        raise IPCError(f"{path} does not contain a local unix: D-Bus address")
    return addr


async def _connect(bus_address: str) -> MessageBus:
    if not bus_address or not bus_address.startswith("unix:"):
        # Refuse to fall back to the session/system bus or a TCP transport.
        raise IPCError("a dedicated local unix: bus address is required")
    try:
        return await MessageBus(bus_address=bus_address, bus_type=BusType.SESSION).connect()
    except Exception as exc:  # noqa: BLE001 -- surface any connect failure uniformly
        raise IPCError(f"cannot connect to game bus: {exc}") from exc


async def _driver_call(bus: MessageBus, member: str, signature: str = "", body=(),
                       timeout: float = DEFAULT_TIMEOUT):
    reply = await asyncio.wait_for(bus.call(Message(
        destination=_DBUS, path=_DBUS_PATH, interface=_DBUS, member=member,
        signature=signature, body=list(body))), timeout)
    if reply.message_type == MessageType.ERROR:
        raise DBusError(reply.error_name, reply.body[0] if reply.body else "")
    return reply.body




# ============================================================== shared tokens
#
# Network players authenticate with a bearer token: base64url(JSON claims) "."
# base64url(HMAC-SHA256(secret, claims)). The coordinator holds the secret; a
# token names exactly one roster slot and expires. Anyone who copies a token can
# act as that player until it expires, so token files must stay private (the
# client refuses a token file that other accounts can read).

def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint_token(secret: bytes, player_id: str, ttl_seconds: float,
               now: Optional[float] = None) -> str:
    """Return a signed token for ``player_id`` valid for ``ttl_seconds``."""
    if len(secret) < 32:
        raise ValueError("token secret must be at least 32 bytes")
    claims = _b64e(_dumps({"p": player_id, "e": int((now or time.time()) + ttl_seconds)})
                   .encode("utf-8"))
    sig = _b64e(hmac.new(secret, claims.encode("ascii"), hashlib.sha256).digest())
    return f"{claims}.{sig}"


def verify_token(secret: bytes, token, now: Optional[float] = None) -> str:
    """Return the player id a valid token names. Raises :class:`RequestError`."""
    bad = RequestError("AUTH_FAILED", "invalid or expired token")
    if not isinstance(token, str) or len(token) > 512 or token.count(".") != 1:
        raise bad
    claims, sig = token.split(".")
    want = _b64e(hmac.new(secret, claims.encode("ascii", "replace"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, want):
        raise bad
    try:
        obj = json.loads(_b64d(claims))
    except (ValueError, UnicodeDecodeError):
        raise bad from None
    if not isinstance(obj, dict) or not isinstance(obj.get("p"), str) \
            or type(obj.get("e")) is not int or obj["e"] <= (now or time.time()):
        raise bad
    return obj["p"]


def read_private_file(path: str) -> bytes:
    """Read a secret (token or key material), refusing files other accounts can read."""
    st = os.stat(path)
    if st.st_mode & 0o077:
        raise IPCError(f"{path} is readable by other accounts; chmod 600 it")
    with open(path, "rb") as fh:
        return fh.read().strip()


def is_network_address(address: str) -> bool:
    return bool(address) and address.split(",")[0].strip().startswith("wss://")


# ============================================================ coordinator side

RequestHandler = Callable[[Caller, Request], Awaitable[dict]]
DisconnectHandler = Callable[[str], Awaitable[None]]
UidResolver = Callable[[str], Awaitable[int]]


class ServerIPC:
    """Coordinator endpoint, independent of transport. Create with :meth:`GameIPC.start_server`.

    Holds the roster, the ``connection <-> slot`` binding, request processing and
    event numbering. :class:`DBusServerIPC` (dedicated bus) and
    :class:`NetServerIPC` (WebSocket over TLS) supply how a caller is identified,
    whether a connection is still alive, and how bytes reach a connection.
    """

    transport = "abstract"

    def __init__(self, roster: dict[int, str], allowed_actions: frozenset,
                 join_action: str, leave_action: str):
        self._roster = dict(roster)                       # uid -> player_id
        self._allowed = allowed_actions | {join_action, leave_action}
        self._join_action = join_action
        self._leave_action = leave_action
        self._handler: Optional[RequestHandler] = None
        self._on_disconnect: Optional[DisconnectHandler] = None
        self._by_name: dict[str, str] = {}                # connection name -> player_id
        self._by_player: dict[str, str] = {}              # player_id -> connection name
        self._uids = {pid: uid for uid, pid in self._roster.items()}
        self._seq = 0
        self._pseq = 0
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self.dropped_events = 0
        self.unique_name = ""

    def on_request(self, handler: RequestHandler) -> None:
        """Register ``async handler(caller, request) -> result_dict``.

        The handler raises :class:`RequestError` to reject. ``caller.player_id``
        is the trusted, transport-authenticated slot (set for every action; for
        the join action it is the slot the identity maps to, not yet bound).
        """
        self._handler = handler

    def on_disconnect(self, handler: DisconnectHandler) -> None:
        """Register ``async handler(player_id)`` called when a bound connection vanishes."""
        self._on_disconnect = handler

    # -- transport hooks ---------------------------------------------------------

    async def _identify(self, name: str) -> int:
        """UID (roster key) of the connection called ``name``."""
        raise NotImplementedError

    async def _still_alive(self, name: str) -> bool:
        raise NotImplementedError

    def _emit(self, destination: Optional[str], text: str) -> bool:
        """Deliver an encoded event to one connection, or to all when ``None``."""
        raise NotImplementedError

    # -- identity ----------------------------------------------------------------

    def player_for(self, name: str) -> Optional[str]:
        return self._by_name.get(name)

    def is_connected(self, player_id: str) -> bool:
        return player_id in self._by_player

    def unbind(self, player_id: str) -> None:
        name = self._by_player.pop(player_id, None)
        if name is not None:
            self._by_name.pop(name, None)

    def _vanished(self, name: str) -> None:
        """A connection is gone: clear its binding and tell the coordinator."""
        pid = self._by_name.get(name)
        if pid is None:
            return
        self.unbind(pid)
        if self._on_disconnect:
            self._spawn(self._on_disconnect(pid))

    async def _bind(self, name: str, player_id: str) -> bool:
        self._by_name[name] = player_id
        self._by_player[player_id] = name
        # Close the race where the caller vanished while we were authenticating.
        alive = await self._still_alive(name)
        if not alive and self._by_player.get(player_id) == name:
            self.unbind(player_id)
            return False
        return True

    # -- requests ----------------------------------------------------------------

    async def _respond(self, sender: str, raw) -> str:
        """Process one request from connection ``sender``; return the response text."""
        rid = None
        try:
            req = parse_request(raw, self._allowed)
            rid = req.id
            if self._handler is None:
                raise RequestError("UNAVAILABLE", "coordinator not ready")
            if req.action == self._join_action:
                result = await self._handle_join(sender, req)
            else:
                pid = self._by_name.get(sender)
                if pid is None:
                    raise RequestError("NOT_JOINED", "call Join first")
                caller = Caller(sender, self._uids[pid], pid)
                result = await self._handler(caller, req)
                if req.action == self._leave_action and self._by_name.get(sender) == pid:
                    self.unbind(pid)
            text = _encode_bounded({"v": PROTOCOL_VERSION, "id": rid, "ok": True,
                                    "result": result}, MAX_RESPONSE_BYTES)
            if text is None:
                text = _error_response(rid, "RESPONSE_TOO_LARGE", "response exceeded size limit")
        except RequestError as exc:
            text = _error_response(rid, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 -- never leak tracebacks to callers
            log.exception("request handler crashed")
            text = _error_response(rid, "INTERNAL", f"internal error ({exc.__class__.__name__})")
        return text

    async def _handle_join(self, sender: str, req: Request) -> dict:
        try:
            uid = await self._identify(sender)
        except RequestError:
            raise
        except Exception:  # noqa: BLE001
            raise RequestError("AUTH_FAILED", "could not determine caller credentials") from None
        pid = self._roster.get(uid)
        if pid is None:
            raise RequestError("UNKNOWN_IDENTITY", f"UID {uid} is not on the roster")
        current = self._by_player.get(pid)
        if current is not None and current != sender and not await self._still_alive(current):
            # The old connection died but its disconnect has not been handled yet.
            self.unbind(pid)
            if self._on_disconnect:
                await self._on_disconnect(pid)
            current = None
        if current is not None and current != sender:
            raise RequestError("DUPLICATE_SESSION", "this player already has an active session")
        bound = self._by_name.get(sender)
        if bound is not None and bound != pid:
            raise RequestError("DUPLICATE_SESSION", "connection already bound to another slot")
        result = await self._handler(Caller(sender, uid, pid), req)
        # Re-check: a concurrent Join from another connection may have won.
        current = self._by_player.get(pid)
        if current is not None and current != sender:
            raise RequestError("DUPLICATE_SESSION", "this player already has an active session")
        if not await self._bind(sender, pid):
            raise RequestError("DISCONNECTED", "caller disconnected during join")
        return result

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- outbound ----------------------------------------------------------------

    def _make_event(self, etype: str, data: dict, visibility: str, round_no: int) -> Optional[str]:
        self._seq += 1
        event = {"v": PROTOCOL_VERSION, "seq": self._seq, "type": etype,
                 "visibility": visibility, "round": round_no, "data": data}
        if visibility == "public":
            self._pseq += 1
            event["pseq"] = self._pseq
        text = _encode_bounded(event, MAX_EVENT_BYTES)
        if text is None:
            log.error("event %s exceeds %d bytes; dropped", etype, MAX_EVENT_BYTES)
        return text

    def broadcast(self, etype: str, data: dict, round_no: int = 0) -> None:
        """Public event to every connection. Only ever pass public data."""
        text = self._make_event(etype, data, "public", round_no)
        if text is not None:
            self._emit(None, text)

    def send_to(self, player_id: str, etype: str, data: dict, round_no: int = 0) -> bool:
        """Event addressed to the player's *currently bound* connection only.

        Returns False (and sends nothing) if the player has no live session.
        """
        name = self._by_player.get(player_id)
        if name is None:
            return False
        text = self._make_event(etype, data, "private", round_no)
        return text is not None and self._emit(name, text)

    def multicast(self, player_ids: Iterable[str], etype: str, data: dict,
                  round_no: int = 0) -> list[str]:
        """Group delivery = fan-out of individually addressed events."""
        return [pid for pid in dict.fromkeys(player_ids)
                if self.send_to(pid, etype, data, round_no)]

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()


class DBusServerIPC(ServerIPC):
    """Coordinator on the dedicated D-Bus bus: identity = bus-verified Unix UID."""

    transport = "dbus"

    def __init__(self, bus: MessageBus, roster: dict[int, str],
                 allowed_actions: frozenset, uid_resolver: Optional[UidResolver],
                 join_action: str, leave_action: str):
        super().__init__(roster, allowed_actions, join_action, leave_action)
        self._bus = bus
        self._resolve_uid = uid_resolver or self._uid_from_bus
        self.unique_name = bus.unique_name

    async def _start(self) -> None:
        self._bus.add_message_handler(self._on_message)
        # Watch the bus driver's NameOwnerChanged to notice vanished players.
        await _driver_call(self._bus, "AddMatch", "s", [
            f"type='signal',sender='{_DBUS}',path='{_DBUS_PATH}',"
            f"interface='{_DBUS}',member='NameOwnerChanged'"])
        reply = await self._bus.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE)
        if reply != RequestNameReply.PRIMARY_OWNER:
            self._bus.disconnect()
            raise IPCError(f"could not acquire {BUS_NAME} exclusively ({reply.name}); "
                           "is another coordinator running?")

    async def _uid_from_bus(self, unique_name: str) -> int:
        body = await _driver_call(self._bus, "GetConnectionUnixUser", "s", [unique_name])
        return int(body[0])

    async def _identify(self, name: str) -> int:
        return await self._resolve_uid(name)

    async def _still_alive(self, unique_name: str) -> bool:
        try:
            return bool((await _driver_call(self._bus, "NameHasOwner", "s", [unique_name]))[0])
        except Exception:  # noqa: BLE001
            return False

    def _on_message(self, msg: Message):
        if msg.message_type == MessageType.SIGNAL:
            if (msg.sender == _DBUS and msg.interface == _DBUS
                    and msg.member == "NameOwnerChanged" and msg.signature == "sss"):
                name, _old, new = msg.body
                if not new:
                    self._vanished(name)
            return False
        if msg.message_type != MessageType.METHOD_CALL:
            return False
        if msg.path != OBJECT_PATH or msg.interface != INTERFACE:
            return False      # let dbus-next answer Introspect/Ping/unknown methods
        if msg.member != REQUEST_METHOD or msg.signature != "s":
            return Message.new_error(msg, "org.syndicate.City1.Error.UnknownMethod",
                                     f"only {REQUEST_METHOD}(s) is supported")
        self._spawn(self._process(msg))
        return True

    async def _process(self, msg: Message) -> None:
        text = await self._respond(msg.sender, msg.body[0])
        if not self._closed and self._bus.connected:
            self._bus.send(Message.new_method_return(msg, "s", [text]))

    def _emit(self, destination: Optional[str], text: str) -> bool:
        if self._closed or not self._bus.connected:
            return False
        writer = getattr(self._bus, "_writer", None)
        if writer is not None and writer.messages.qsize() > SERVER_OUTBOX:   # bounded outbox
            self.dropped_events += 1
            log.warning("outbound queue full; dropping event")
            return False
        self._bus.send(Message(message_type=MessageType.SIGNAL, destination=destination,
                               path=OBJECT_PATH, interface=INTERFACE, member=EVENT_SIGNAL,
                               signature="s", body=[text]))
        return True

    async def close(self) -> None:
        await super().close()
        try:
            await self._bus.release_name(BUS_NAME)
        except Exception:  # noqa: BLE001
            pass
        self._bus.disconnect()


# ------------------------------------------------------- network coordinator
#
# Frames are JSON text messages on one WebSocket per player, inside TLS:
#
#   client -> server  {"t": "hello", "v": 1, "token": "..."}           (first frame)
#   server -> client  {"t": "welcome", "v": 1, "server": "<id>", "name": "net:p1:..."}
#                     {"t": "refused", "code": "AUTH_FAILED|NOT_LEADER", "message": "..."}
#   client -> server  {"t": "req", "c": <int>, "body": "<request envelope>"}
#   server -> client  {"t": "resp", "c": <int>, "body": "<response envelope>"}
#                     {"t": "event", "scope": "public|private", "body": "<event>"}
#
# The envelopes inside ``body`` are byte-for-byte the D-Bus ones, so validation,
# size limits and the coordinator are shared. ``scope`` plays the role of the
# D-Bus destination header: "private" frames are written only to the bound
# connection of the addressed player, never to anyone else.

HELLO_TIMEOUT = 5.0
NET_CONN_OUTBOX = 1000
NET_INFLIGHT_PER_CONN = 32
_FRAME_SLACK = 1024


class _NetConn:
    def __init__(self, ws, name: str, player_id: str):
        self.ws = ws
        self.name = name
        self.player_id = player_id
        self.outbox: asyncio.Queue = asyncio.Queue(maxsize=NET_CONN_OUTBOX)
        self.inflight = asyncio.Semaphore(NET_INFLIGHT_PER_CONN)

    def push(self, frame: dict) -> bool:
        try:
            self.outbox.put_nowait(_dumps(frame))
            return True
        except asyncio.QueueFull:
            return False


class NetServerIPC(ServerIPC):
    """Coordinator over WebSocket + TLS: identity = HMAC token naming a roster slot.

    ``accepting`` lets a replicated coordinator refuse clients while it is not
    the leader; :meth:`drop_all` disconnects everyone when it steps down.
    """

    transport = "wss"

    def __init__(self, host: str, port: int, ssl_context: ssl.SSLContext,
                 token_secret: bytes, roster: dict[int, str], allowed_actions: frozenset,
                 join_action: str, leave_action: str, server_id: Optional[str] = None):
        super().__init__(roster, allowed_actions, join_action, leave_action)
        self._host, self._port = host, port
        self._ssl = ssl_context
        self._secret = token_secret
        self._conns: dict[str, _NetConn] = {}
        self._server = None
        self.accepting = True
        self.refusal = "this coordinator is not accepting players"
        self.unique_name = server_id or f"wss:{host}:{port}:{secrets.token_hex(3)}"

    async def _start(self) -> None:
        try:
            self._server = await ws_serve(
                self._serve, self._host, self._port, ssl=self._ssl,
                max_size=MAX_REQUEST_BYTES + _FRAME_SLACK, ping_interval=5, ping_timeout=10,
                close_timeout=2)
        except OSError as exc:
            raise IPCError(f"cannot listen on {self._host}:{self._port}: {exc}") from exc
        if self._port == 0:
            self._port = self._server.sockets[0].getsockname()[1]

    @property
    def port(self) -> int:
        return self._port

    async def _identify(self, name: str) -> int:
        conn = self._conns.get(name)
        if conn is None:
            raise RequestError("DISCONNECTED", "connection closed")
        return self._uids[conn.player_id]

    async def _still_alive(self, name: str) -> bool:
        return name in self._conns

    async def _hello(self, ws) -> Optional[str]:
        """Authenticate the first frame; return the player id or None (already refused)."""
        async def refuse(code: str, message: str) -> None:
            with contextlib.suppress(Exception):
                await ws.send(_dumps({"t": "refused", "code": code, "message": message}))
                await ws.close(4401 if code == "AUTH_FAILED" else 4503, code)

        try:
            raw = await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT)
            frame = json.loads(raw, parse_constant=_reject_constant)
        except (asyncio.TimeoutError, ValueError, TypeError, RecursionError):
            await refuse("BAD_HELLO", "expected a hello frame")
            return None
        if not isinstance(frame, dict) or frame.get("t") != "hello" \
                or frame.get("v") != PROTOCOL_VERSION:
            await refuse("BAD_HELLO", "expected a hello frame")
            return None
        try:
            pid = verify_token(self._secret, frame.get("token"))
        except RequestError as exc:
            await refuse(exc.code, exc.message)
            return None
        if pid not in self._uids:
            await refuse("AUTH_FAILED", "token names a player who is not on the roster")
            return None
        if not self.accepting or self._closed:
            await refuse("NOT_LEADER", self.refusal)
            return None
        return pid

    async def _serve(self, ws) -> None:
        pid = await self._hello(ws)
        if pid is None:
            return
        name = f"net:{pid}:{secrets.token_hex(4)}"
        conn = _NetConn(ws, name, pid)
        self._conns[name] = conn
        writer = asyncio.ensure_future(self._writer(conn))
        try:
            await ws.send(_dumps({"t": "welcome", "v": PROTOCOL_VERSION,
                                  "server": self.unique_name, "name": name}))
            async for raw in ws:
                try:
                    frame = json.loads(raw, parse_constant=_reject_constant)
                except (ValueError, TypeError, RecursionError):
                    break                      # not our protocol: drop the connection
                if not isinstance(frame, dict) or frame.get("t") != "req" \
                        or type(frame.get("c")) is not int or not isinstance(frame.get("body"), str):
                    break
                await conn.inflight.acquire()  # bounded concurrency per connection
                self._spawn(self._answer(conn, frame["c"], frame["body"]))
        except Exception:  # noqa: BLE001 -- any socket error ends this connection
            pass
        finally:
            self._conns.pop(name, None)
            writer.cancel()
            self._vanished(name)

    async def _answer(self, conn: _NetConn, corr: int, body: str) -> None:
        try:
            text = await self._respond(conn.name, body)
            if not conn.push({"t": "resp", "c": corr, "body": text}):
                self.dropped_events += 1
        finally:
            conn.inflight.release()

    async def _writer(self, conn: _NetConn) -> None:
        with contextlib.suppress(Exception):
            while True:
                await conn.ws.send(await conn.outbox.get())

    def _emit(self, destination: Optional[str], text: str) -> bool:
        if self._closed:
            return False
        if destination is None:
            frame = {"t": "event", "scope": "public", "body": text}
            for conn in list(self._conns.values()):
                if not conn.push(frame):
                    self.dropped_events += 1
            return True
        conn = self._conns.get(destination)
        if conn is None:
            return False
        if not conn.push({"t": "event", "scope": "private", "body": text}):
            self.dropped_events += 1
            return False
        return True

    async def drop_all(self, reason: str = "coordinator stepped down") -> None:
        """Disconnect every player (they reconnect to the new leader)."""
        for conn in list(self._conns.values()):
            with contextlib.suppress(Exception):
                await conn.ws.close(4503, reason[:100])

    async def close(self) -> None:
        await self.drop_all("coordinator shutting down")
        await super().close()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 3)


# ================================================================ player side

class ClientIPC:
    """Player endpoint, independent of transport. Create with :meth:`GameIPC.connect_client`."""

    transport = "abstract"

    def __init__(self):
        self._events: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_EVENT_QUEUE)
        self._session = secrets.token_hex(4)
        self._counter = 0
        self._last_pseq: Optional[int] = None
        self._join_payload: Optional[dict] = None
        self.unique_name = ""
        self.forged_signals = 0
        self.dropped_events = 0

    # -- transport hooks ---------------------------------------------------------

    @property
    def coordinator(self) -> Optional[str]:
        raise NotImplementedError

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    async def request_raw(self, text: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    # -- events ------------------------------------------------------------------

    def _push_local(self, etype: str, data: dict) -> None:
        """Library-generated event (never from the network); type starts with '_'."""
        try:
            self._events.put_nowait({"v": PROTOCOL_VERSION, "type": "_" + etype,
                                     "visibility": "local", "data": data})
        except asyncio.QueueFull:
            self.dropped_events += 1

    def _deliver(self, raw, public: bool) -> None:
        """Validate one event from the verified coordinator and queue it.

        ``public`` comes from the transport (no D-Bus destination / ``scope``),
        never from the payload's own ``visibility`` claim.
        """
        if not isinstance(raw, str) or len(raw.encode("utf-8", "surrogatepass")) > MAX_EVENT_BYTES:
            return
        try:
            event = json.loads(raw)
        except ValueError:
            return
        if not isinstance(event, dict) or event.get("v") != PROTOCOL_VERSION \
                or not isinstance(event.get("type"), str) or not isinstance(event.get("data"), dict):
            return
        event["visibility"] = "public" if public else "private"
        if public and isinstance(event.get("pseq"), int):
            if self._last_pseq is not None and event["pseq"] != self._last_pseq + 1:
                event["missed_public"] = event["pseq"] - self._last_pseq - 1
            self._last_pseq = event["pseq"]
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events += 1

    # -- requests ----------------------------------------------------------------

    def new_request_id(self) -> str:
        self._counter += 1
        return f"{self._session}-{self._counter}"

    def _envelope(self, action: str, payload: Optional[dict], rid: str) -> str:
        text = _dumps({"v": PROTOCOL_VERSION, "id": rid, "action": action,
                       "payload": payload or {}})
        if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise RequestError("TOO_LARGE", f"request exceeds {MAX_REQUEST_BYTES} bytes")
        return text

    @staticmethod
    def _result(resp: str, rid: str) -> dict:
        try:
            obj = json.loads(resp)
        except ValueError:
            raise IPCError("coordinator sent malformed JSON") from None
        if not isinstance(obj, dict) or obj.get("v") != PROTOCOL_VERSION or obj.get("id") != rid:
            raise IPCError("coordinator response does not match request")
        if obj.get("ok") is True:
            return obj.get("result") or {}
        err = obj.get("error") or {}
        raise RequestError(str(err.get("code", "ERROR")), str(err.get("message", "")))

    async def request(self, action: str, payload: Optional[dict] = None,
                      request_id: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
        """Send one request; return its ``result`` or raise :class:`RequestError`/:class:`IPCError`."""
        rid = request_id or self.new_request_id()
        text = self._envelope(action, payload, rid)
        return self._result(await self.request_raw(text, timeout), rid)

    async def join(self, display_name: Optional[str] = None) -> dict:
        payload = {"display_name": display_name} if display_name else {}
        result = await self.request("Join", payload)
        self._join_payload = payload
        return result

    async def send_public(self, text: str) -> dict:
        return await self.request("SendPublic", {"text": text})

    async def send_private(self, to: str, text: str) -> dict:
        return await self.request("SendPrivate", {"to": to, "text": text})

    async def send_group(self, to: list[str], text: str) -> dict:
        return await self.request("SendGroup", {"to": list(to), "text": text})

    async def next_event(self, timeout: Optional[float] = None) -> dict:
        if timeout is None:
            return await self._events.get()
        return await asyncio.wait_for(self._events.get(), timeout)

    async def events(self) -> AsyncIterator[dict]:
        """Yield verified coordinator events (and ``_``-prefixed local notices)."""
        while True:
            event = await self._events.get()
            yield event
            if event["type"] == "_BusDisconnected":
                return


class DBusClientIPC(ClientIPC):
    """Player on the dedicated D-Bus bus."""

    transport = "dbus"

    def __init__(self, bus: MessageBus):
        super().__init__()
        self._bus = bus
        self._coordinator: Optional[str] = None
        self._started = False
        self.unique_name = bus.unique_name

    async def _start(self, timeout: float) -> None:
        self._bus.add_message_handler(self._on_message)
        # Subscribe to coordinator broadcasts. The daemon resolves the well-known
        # sender to its owner; we additionally verify the sender in _on_message.
        await _driver_call(self._bus, "AddMatch", "s", [
            f"type='signal',sender='{BUS_NAME}',path='{OBJECT_PATH}',"
            f"interface='{INTERFACE}',member='{EVENT_SIGNAL}'"], timeout=timeout)
        await _driver_call(self._bus, "AddMatch", "s", [
            f"type='signal',sender='{_DBUS}',interface='{_DBUS}',"
            f"member='NameOwnerChanged',arg0='{BUS_NAME}'"], timeout=timeout)
        try:
            self._coordinator = (await _driver_call(
                self._bus, "GetNameOwner", "s", [BUS_NAME], timeout=timeout))[0]
        except DBusError:
            self._bus.disconnect()
            raise IPCError("the coordinator is not running on this bus") from None
        self._started = True
        asyncio.ensure_future(self._watch_disconnect())

    @property
    def coordinator(self) -> Optional[str]:
        """Unique name of the verified coordinator connection."""
        return self._coordinator

    async def _watch_disconnect(self) -> None:
        try:
            await self._bus.wait_for_disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._push_local("BusDisconnected", {"reason": "connection to game bus lost"})

    def _on_message(self, msg: Message):
        if msg.message_type != MessageType.SIGNAL:
            return False
        if msg.sender == _DBUS and msg.member == "NameOwnerChanged" and msg.signature == "sss":
            name, old, new = msg.body
            if name == BUS_NAME and old == self._coordinator and new != self._coordinator:
                self._coordinator = None
                self._push_local("CoordinatorLost", {"reason": "coordinator left the bus"})
            return True
        if msg.interface != INTERFACE or msg.member != EVENT_SIGNAL:
            return False
        if not self._started:
            # Arrived between AddMatch and GetNameOwner, so it cannot be verified yet.
            # Drop it quietly: Join's snapshot supersedes any pre-join broadcast.
            return True
        # Provenance: only the connection that owned BUS_NAME when we connected.
        if self._coordinator is None or msg.sender != self._coordinator \
                or msg.path != OBJECT_PATH or msg.signature != "s":
            self.forged_signals += 1
            self._push_local("ForgedSignalIgnored", {"sender": str(msg.sender)})
            return True
        if msg.destination not in (None, self.unique_name):
            return True
        # Visibility is derived from the transport, not the payload's claim.
        self._deliver(msg.body[0], public=not msg.destination)
        return True

    async def request_raw(self, text: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """Send an arbitrary string payload (used by adversarial tests)."""
        if self._coordinator is None:
            raise IPCError("coordinator is not available")
        if not self._bus.connected:
            raise IPCError("not connected to the game bus")
        try:
            reply = await asyncio.wait_for(self._bus.call(Message(
                destination=self._coordinator, path=OBJECT_PATH, interface=INTERFACE,
                member=REQUEST_METHOD, signature="s", body=[text])), timeout)
        except asyncio.TimeoutError:
            raise IPCError(f"request timed out after {timeout}s") from None
        except Exception as exc:  # noqa: BLE001
            raise IPCError(f"request failed: {exc}") from None
        if reply.message_type == MessageType.ERROR:
            raise IPCError(f"D-Bus error {reply.error_name}: {reply.body[0] if reply.body else ''}")
        if reply.sender != self._coordinator or reply.signature != "s":
            raise IPCError("reply did not come from the coordinator")
        return reply.body[0]

    @property
    def connected(self) -> bool:
        return self._bus.connected

    async def close(self) -> None:
        if self._bus.connected:
            self._bus.disconnect()


class NetClientIPC(ClientIPC):
    """Player over WebSocket + TLS, with fail-over across several coordinators.

    ``urls`` lists every coordinator replica. If the connection drops, the
    client tries them in turn for up to ``reconnect_window`` seconds, re-joins
    with the same token, and queues ``_Reconnected`` carrying a fresh snapshot.
    A request interrupted by fail-over is re-sent with the *same* request id, so
    the coordinator's idempotency ledger guarantees it takes effect at most once.
    """

    transport = "wss"

    def __init__(self, urls: list[str], token: str, ssl_context: ssl.SSLContext,
                 reconnect_window: float):
        super().__init__()
        self._urls = urls
        self._url_index = 0
        self._token = token
        self._ssl = ssl_context
        self._reconnect_window = reconnect_window
        self._ws = None
        self._server: Optional[str] = None
        self._ready = asyncio.Event()           # connected, welcomed and (re)joined
        self._pending: dict[int, asyncio.Future] = {}
        self._corr = 0
        self._closing = False
        self._reader: Optional[asyncio.Task] = None
        self.reconnects = 0

    # -- connection management ---------------------------------------------------

    async def _open(self, url: str, timeout: float):
        """Connect to one replica and complete the hello. Raises on any failure."""
        ws = await ws_connect(url, ssl=self._ssl, open_timeout=timeout,
                              max_size=MAX_EVENT_BYTES + _FRAME_SLACK,
                              ping_interval=5, ping_timeout=10, close_timeout=2)
        try:
            await ws.send(_dumps({"t": "hello", "v": PROTOCOL_VERSION, "token": self._token}))
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if not isinstance(frame, dict):
                raise IPCError("malformed hello reply")
            if frame.get("t") == "refused":
                raise RequestError(str(frame.get("code", "REFUSED")),
                                   str(frame.get("message", ""))[:300])
            if frame.get("t") != "welcome" or not isinstance(frame.get("server"), str) \
                    or not isinstance(frame.get("name"), str):
                raise IPCError("malformed hello reply")
        except BaseException:
            with contextlib.suppress(Exception):
                await ws.close()
            raise
        return ws, frame["server"], frame["name"]

    async def _connect_any(self, window: float) -> None:
        """Try each replica in turn until one welcomes us or ``window`` expires."""
        loop = asyncio.get_running_loop()
        end = loop.time() + window
        last: Exception = IPCError("no coordinator address")
        delay = 0.1
        while True:
            for _ in range(len(self._urls)):
                url = self._urls[self._url_index]
                remaining = end - loop.time()
                if remaining <= 0:
                    break
                try:
                    ws, server, name = await self._open(url, min(DEFAULT_TIMEOUT, remaining))
                except RequestError as exc:
                    if exc.code == "AUTH_FAILED":
                        raise          # a bad token will not get better elsewhere
                    last = exc
                except Exception as exc:  # noqa: BLE001
                    last = exc
                else:
                    self._ws, self._server, self.unique_name = ws, server, name
                    self._last_pseq = None      # a new coordinator numbers events afresh
                    self._reader = asyncio.ensure_future(self._read(ws))
                    return
                self._url_index = (self._url_index + 1) % len(self._urls)
            if loop.time() + delay >= end:
                raise IPCError(f"no coordinator reachable: {last}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1.0)

    async def _start(self, timeout: float) -> None:
        try:
            await self._connect_any(timeout)
        except RequestError as exc:
            raise IPCError(f"coordinator refused us: {exc.code}: {exc.message}") from None
        self._ready.set()

    async def _read(self, ws) -> None:
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("t") == "resp" and type(frame.get("c")) is int:
                    fut = self._pending.pop(frame["c"], None)
                    if fut is not None and not fut.done() and isinstance(frame.get("body"), str):
                        fut.set_result(frame["body"])
                elif frame.get("t") == "event" and frame.get("scope") in ("public", "private"):
                    self._deliver(frame.get("body"), public=frame["scope"] == "public")
        except Exception:  # noqa: BLE001
            pass
        if ws is self._ws and not self._closing:
            asyncio.ensure_future(self._failover())

    async def _failover(self) -> None:
        self._ready.clear()
        self._ws = None
        lost = ConnectionError("connection lost")
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(lost)
        self._pending.clear()
        self._push_local("Reconnecting", {"reason": f"lost {self._server}; finding a coordinator"})
        loop = asyncio.get_running_loop()
        end = loop.time() + self._reconnect_window
        while not self._closing and loop.time() < end:
            try:
                await self._connect_any(end - loop.time())
                snap = None
                if self._join_payload is not None:
                    rid = self.new_request_id()
                    text = self._envelope("Join", self._join_payload, rid)
                    snap = self._result(await self._send(text, DEFAULT_TIMEOUT), rid)
            except (IPCError, RequestError, ConnectionError, asyncio.TimeoutError) as exc:
                if isinstance(exc, RequestError) and exc.code == "AUTH_FAILED":
                    break
                if self._ws is not None:
                    ws, self._ws = self._ws, None
                    with contextlib.suppress(Exception):
                        await ws.close()
                await asyncio.sleep(0.3)
                continue
            self.reconnects += 1
            self._ready.set()
            self._push_local("Reconnected", {"server": self._server, "snapshot": snap})
            return
        if not self._closing:
            self._push_local("BusDisconnected", {"reason": "no coordinator reachable"})

    # -- requests ----------------------------------------------------------------

    async def _send(self, text: str, timeout: float) -> str:
        """One attempt on the current connection."""
        ws = self._ws
        if ws is None:
            raise ConnectionError("not connected")
        self._corr += 1
        corr = self._corr
        fut = asyncio.get_running_loop().create_future()
        self._pending[corr] = fut
        try:
            await ws.send(_dumps({"t": "req", "c": corr, "body": text}))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001 -- socket closed under us
            raise ConnectionError(str(exc)) from None
        finally:
            self._pending.pop(corr, None)

    async def request_raw(self, text: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """Send one request, re-sending it unchanged across a fail-over."""
        if self._closing:
            raise IPCError("connection closed")
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            remaining = end - loop.time()
            if remaining <= 0:
                raise IPCError(f"request timed out after {timeout}s")
            try:
                await asyncio.wait_for(self._ready.wait(), remaining)
                return await self._send(text, max(0.01, end - loop.time()))
            except asyncio.TimeoutError:
                raise IPCError(f"request timed out after {timeout}s") from None
            except ConnectionError:
                await asyncio.sleep(0.05)      # wait for fail-over, then re-send

    @property
    def coordinator(self) -> Optional[str]:
        return self._server

    @property
    def connected(self) -> bool:
        return not self._closing and self._ws is not None

    async def close(self) -> None:
        self._closing = True
        self._ready.clear()
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if self._reader is not None:
            self._reader.cancel()


# ======================================================================= facade

def _split_wss(address: str) -> list[str]:
    urls = [u.strip() for u in address.split(",") if u.strip()]
    if not urls or any(not u.startswith("wss://") for u in urls):
        raise IPCError("network addresses must be wss:// URLs (TLS is mandatory)")
    return urls


def server_ssl_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_file, key_file)
    return ctx


def client_ssl_context(ca_file: Optional[str]) -> ssl.SSLContext:
    """Verify the coordinator's certificate (and hostname) against ``ca_file`` or system CAs."""
    ctx = ssl.create_default_context(cafile=ca_file)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class GameIPC:
    """Entry points matching the PRD's illustrative API.

    The address picks the transport: ``unix:path=...`` is the dedicated D-Bus
    bus; ``wss://host:port`` (comma-separated for replicas) is the network.
    """

    @staticmethod
    async def start_server(bus_address: str, roster: dict[int, str],
                           allowed_actions: Iterable[str],
                           join_action: str = "Join", leave_action: str = "Leave",
                           uid_resolver: Optional[UidResolver] = None, *,
                           tls_cert: Optional[str] = None, tls_key: Optional[str] = None,
                           token_secret: Optional[bytes] = None,
                           server_id: Optional[str] = None) -> ServerIPC:
        """Start serving requests.

        D-Bus: claim ``org.syndicate.City`` exclusively. ``roster`` maps Linux UID
        -> player id; ``uid_resolver`` exists only for single-UID unit tests.

        Network: listen on ``wss://host:port`` with ``tls_cert``/``tls_key``;
        players present tokens signed with ``token_secret``.
        """
        if is_network_address(bus_address):
            urls = _split_wss(bus_address)
            if len(urls) != 1:
                raise IPCError("a coordinator listens on exactly one wss:// address")
            if not (tls_cert and tls_key and token_secret):
                raise IPCError("network mode needs a TLS certificate, key and token secret")
            if len(token_secret) < 32:
                raise IPCError("token secret must be at least 32 bytes")
            parsed = urlparse(urls[0])
            server = NetServerIPC(parsed.hostname or "0.0.0.0", parsed.port or 7443,
                                  server_ssl_context(tls_cert, tls_key), token_secret,
                                  roster, frozenset(allowed_actions), join_action,
                                  leave_action, server_id)
            await server._start()
            return server
        bus = await _connect(bus_address)
        server = DBusServerIPC(bus, roster, frozenset(allowed_actions), uid_resolver,
                               join_action, leave_action)
        await server._start()
        return server

    @staticmethod
    async def connect_client(bus_address: str, timeout: float = DEFAULT_TIMEOUT, *,
                             token: Optional[str] = None, ca_file: Optional[str] = None,
                             reconnect_window: float = 30.0) -> ClientIPC:
        if is_network_address(bus_address):
            if not token:
                raise IPCError("network mode needs a player token")
            client = NetClientIPC(_split_wss(bus_address), token,
                                  client_ssl_context(ca_file), reconnect_window)
            await client._start(timeout)
            return client
        bus = await _connect(bus_address)
        client = DBusClientIPC(bus)
        await client._start(timeout)
        return client


def default_bus_address_file() -> str:
    return os.environ.get("SYNDICATE_BUS_ADDRESS_FILE", "/run/syndicate-city/bus.address")
