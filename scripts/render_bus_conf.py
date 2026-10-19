#!/usr/bin/env python3
"""Render config/bus.conf.in into a concrete dbus-daemon configuration.

    render_bus_conf.py --socket /run/syndicate-city/bus.sock \
        --server-user syndicate-server --player syndicate-p1 ... --out bus.conf

--debug-monitor lets root connect and call BecomeMonitor, so
``sudo dbus-monitor --address ...`` sees every message on the bus, private ones
included. It is for debugging only and breaks the privacy guarantees.
"""

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "config", "bus.conf.in")
SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")

DEBUG_MONITOR_POLICY = """\
  <!-- DEBUG BUILD (debug-monitor option): root may connect and become a bus monitor,
       which shows it every message, private ones included. Never use in play. -->
  <policy user="root">
    <allow send_destination="org.freedesktop.DBus"
           send_interface="org.freedesktop.DBus.Monitoring"/>
  </policy>"""


def render(socket: str, server_user: str, players: list[str], max_conn: int = 3,
           debug_monitor: bool = False) -> str:
    for name in [server_user, *players]:
        if not SAFE.match(name):
            raise ValueError(f"unsafe user name {name!r}")
    if not socket.startswith("/") or any(c in socket for c in "<>&\"',;="):
        raise ValueError("socket must be an absolute path without XML/address metacharacters")
    users = list(dict.fromkeys([server_user, *players, *(["root"] if debug_monitor else [])]))
    rules = "\n".join(f'    <allow user="{u}"/>' for u in users)
    with open(TEMPLATE, encoding="utf-8") as fh:
        text = fh.read()
    return (text.replace("@SOCKET@", socket).replace("@SERVER_USER@", server_user)
                .replace("@CONNECT_RULES@", rules).replace("@MAX_CONN_PER_USER@", str(max_conn))
                .replace("@DEBUG_MONITOR@", DEBUG_MONITOR_POLICY if debug_monitor else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--server-user", required=True)
    ap.add_argument("--player", action="append", default=[])
    ap.add_argument("--max-connections-per-user", type=int, default=3)
    ap.add_argument("--debug-monitor", action="store_true",
                    help="let root watch all bus traffic with dbus-monitor (debugging only)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    text = render(a.socket, a.server_user, a.player, a.max_connections_per_user,
                  a.debug_monitor)
    fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
