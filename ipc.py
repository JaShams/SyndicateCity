"""Syndicate City communications library -- the entire IPC layer in one file.

Every D-Bus interaction of the game goes through this module; ``server.py`` and
``client.py`` never import ``dbus_next`` themselves. The library knows nothing
about game rules. It provides:

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
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Iterable, Optional

from dbus_next import BusType, Message, MessageType
from dbus_next.aio import MessageBus
from dbus_next.constants import NameFlag, RequestNameReply
from dbus_next.errors import DBusError

__all__ = [
    "BUS_NAME", "OBJECT_PATH", "INTERFACE", "PROTOCOL_VERSION",
    "MAX_REQUEST_BYTES", "MAX_RESPONSE_BYTES", "MAX_EVENT_BYTES",
    "IPCError", "RequestError", "Caller", "Request",
    "GameIPC", "ServerIPC", "ClientIPC", "read_bus_address",
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


# ============================================================ coordinator side

RequestHandler = Callable[[Caller, Request], Awaitable[dict]]
DisconnectHandler = Callable[[str], Awaitable[None]]
UidResolver = Callable[[str], Awaitable[int]]


class ServerIPC:
    """Coordinator endpoint. Create with :meth:`GameIPC.start_server`."""

    def __init__(self, bus: MessageBus, roster: dict[int, str],
                 allowed_actions: frozenset, uid_resolver: Optional[UidResolver],
                 join_action: str, leave_action: str):
        self._bus = bus
        self._roster = dict(roster)                       # uid -> player_id
        self._allowed = allowed_actions | {join_action, leave_action}
        self._join_action = join_action
        self._leave_action = leave_action
        self._resolve_uid = uid_resolver or self._uid_from_bus
        self._handler: Optional[RequestHandler] = None
        self._on_disconnect: Optional[DisconnectHandler] = None
        self._by_name: dict[str, str] = {}                # unique name -> player_id
        self._by_player: dict[str, str] = {}              # player_id -> unique name
        self._uids = {pid: uid for uid, pid in self._roster.items()}
        self._seq = 0
        self._pseq = 0
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self.dropped_events = 0
        self.unique_name = bus.unique_name

    # -- setup -----------------------------------------------------------------

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

    def on_request(self, handler: RequestHandler) -> None:
        """Register ``async handler(caller, request) -> result_dict``.

        The handler raises :class:`RequestError` to reject. ``caller.player_id``
        is the trusted, bus-authenticated slot (set for every action; for the
        join action it is the slot the UID maps to, not yet bound).
        """
        self._handler = handler

    def on_disconnect(self, handler: DisconnectHandler) -> None:
        """Register ``async handler(player_id)`` called when a bound connection vanishes."""
        self._on_disconnect = handler

    # -- identity ----------------------------------------------------------------

    async def _uid_from_bus(self, unique_name: str) -> int:
        body = await _driver_call(self._bus, "GetConnectionUnixUser", "s", [unique_name])
        return int(body[0])

    def player_for(self, unique_name: str) -> Optional[str]:
        return self._by_name.get(unique_name)

    def is_connected(self, player_id: str) -> bool:
        return player_id in self._by_player

    def unbind(self, player_id: str) -> None:
        name = self._by_player.pop(player_id, None)
        if name is not None:
            self._by_name.pop(name, None)

    async def _still_alive(self, unique_name: str) -> bool:
        try:
            return bool((await _driver_call(self._bus, "NameHasOwner", "s", [unique_name]))[0])
        except Exception:  # noqa: BLE001
            return False

    async def _bind(self, unique_name: str, player_id: str) -> bool:
        self._by_name[unique_name] = player_id
        self._by_player[player_id] = unique_name
        # Close the race where the caller vanished while we were authenticating.
        alive = await self._still_alive(unique_name)
        if not alive and self._by_player.get(player_id) == unique_name:
            self.unbind(player_id)
            return False
        return True

    # -- inbound -----------------------------------------------------------------

    def _on_message(self, msg: Message):
        if msg.message_type == MessageType.SIGNAL:
            if (msg.sender == _DBUS and msg.interface == _DBUS
                    and msg.member == "NameOwnerChanged" and msg.signature == "sss"):
                name, _old, new = msg.body
                if not new and name in self._by_name:
                    pid = self._by_name.get(name)
                    self.unbind(pid)
                    if self._on_disconnect:
                        self._spawn(self._on_disconnect(pid))
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
        sender = msg.sender
        rid = None
        try:
            req = parse_request(msg.body[0], self._allowed)
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
        if not self._closed and self._bus.connected:
            self._bus.send(Message.new_method_return(msg, "s", [text]))

    async def _handle_join(self, sender: str, req: Request) -> dict:
        try:
            uid = await self._resolve_uid(sender)
        except Exception:  # noqa: BLE001
            raise RequestError("AUTH_FAILED", "could not determine caller credentials") from None
        pid = self._roster.get(uid)
        if pid is None:
            raise RequestError("UNKNOWN_IDENTITY", f"UID {uid} is not on the roster")
        current = self._by_player.get(pid)
        if current is not None and current != sender and not await self._still_alive(current):
            # The old process died but its NameOwnerChanged has not been handled yet.
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

    def broadcast(self, etype: str, data: dict, round_no: int = 0) -> None:
        """Public signal with no destination. Only ever pass public data."""
        text = self._make_event(etype, data, "public", round_no)
        if text is not None:
            self._emit(None, text)

    def send_to(self, player_id: str, etype: str, data: dict, round_no: int = 0) -> bool:
        """Unicast signal addressed to the player's *currently bound* unique name.

        Returns False (and sends nothing) if the player has no live session.
        """
        name = self._by_player.get(player_id)
        if name is None:
            return False
        text = self._make_event(etype, data, "private", round_no)
        return text is not None and self._emit(name, text)

    def multicast(self, player_ids: Iterable[str], etype: str, data: dict,
                  round_no: int = 0) -> list[str]:
        """Group delivery = fan-out of individually addressed signals."""
        return [pid for pid in dict.fromkeys(player_ids)
                if self.send_to(pid, etype, data, round_no)]

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        try:
            await self._bus.release_name(BUS_NAME)
        except Exception:  # noqa: BLE001
            pass
        self._bus.disconnect()


# ================================================================ player side

class ClientIPC:
    """Player endpoint. Create with :meth:`GameIPC.connect_client`."""

    def __init__(self, bus: MessageBus):
        self._bus = bus
        self._coordinator: Optional[str] = None
        self._started = False
        self._events: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_EVENT_QUEUE)
        self._session = secrets.token_hex(4)
        self._counter = 0
        self._last_pseq: Optional[int] = None
        self.unique_name = bus.unique_name
        self.forged_signals = 0
        self.dropped_events = 0

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

    def _push_local(self, etype: str, data: dict) -> None:
        """Library-generated event (never from the network); type starts with '_'."""
        try:
            self._events.put_nowait({"v": PROTOCOL_VERSION, "type": "_" + etype,
                                     "visibility": "local", "data": data})
        except asyncio.QueueFull:
            self.dropped_events += 1

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
        raw = msg.body[0]
        if len(raw.encode("utf-8", "surrogatepass")) > MAX_EVENT_BYTES:
            return True
        try:
            event = json.loads(raw)
        except ValueError:
            return True
        if not isinstance(event, dict) or event.get("v") != PROTOCOL_VERSION \
                or not isinstance(event.get("type"), str) or not isinstance(event.get("data"), dict):
            return True
        # Visibility is derived from the transport, not the payload's claim.
        event["visibility"] = "private" if msg.destination else "public"
        if event["visibility"] == "public" and isinstance(event.get("pseq"), int):
            if self._last_pseq is not None and event["pseq"] != self._last_pseq + 1:
                event["missed_public"] = event["pseq"] - self._last_pseq - 1
            self._last_pseq = event["pseq"]
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events += 1
        return True

    def new_request_id(self) -> str:
        self._counter += 1
        return f"{self._session}-{self._counter}"

    async def request(self, action: str, payload: Optional[dict] = None,
                      request_id: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
        """Send one request; return its ``result`` or raise :class:`RequestError`/:class:`IPCError`."""
        rid = request_id or self.new_request_id()
        text = _dumps({"v": PROTOCOL_VERSION, "id": rid, "action": action,
                       "payload": payload or {}})
        if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise RequestError("TOO_LARGE", f"request exceeds {MAX_REQUEST_BYTES} bytes")
        resp = await self.request_raw(text, timeout)
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

    async def join(self, display_name: Optional[str] = None) -> dict:
        payload = {"display_name": display_name} if display_name else {}
        return await self.request("Join", payload)

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

    @property
    def connected(self) -> bool:
        return self._bus.connected

    async def close(self) -> None:
        if self._bus.connected:
            self._bus.disconnect()


# ======================================================================= facade

class GameIPC:
    """Entry points matching the PRD's illustrative API."""

    @staticmethod
    async def start_server(bus_address: str, roster: dict[int, str],
                           allowed_actions: Iterable[str],
                           join_action: str = "Join", leave_action: str = "Leave",
                           uid_resolver: Optional[UidResolver] = None) -> ServerIPC:
        """Connect, claim ``org.syndicate.City`` exclusively and serve requests.

        ``roster`` maps Linux UID -> player id. ``uid_resolver`` exists only for
        single-UID unit tests; production callers leave it ``None`` so identity
        comes from ``org.freedesktop.DBus.GetConnectionUnixUser``.
        """
        bus = await _connect(bus_address)
        server = ServerIPC(bus, roster, frozenset(allowed_actions), uid_resolver,
                           join_action, leave_action)
        await server._start()
        return server

    @staticmethod
    async def connect_client(bus_address: str, timeout: float = DEFAULT_TIMEOUT) -> ClientIPC:
        bus = await _connect(bus_address)
        client = ClientIPC(bus)
        await client._start(timeout)
        return client


def default_bus_address_file() -> str:
    return os.environ.get("SYNDICATE_BUS_ADDRESS_FILE", "/run/syndicate-city/bus.address")
