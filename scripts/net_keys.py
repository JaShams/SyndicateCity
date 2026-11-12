#!/usr/bin/env python3
"""Key material for network play (wss://) and the replicated coordinator.

    net_keys.py init  --dir DIR --host game.example.org [--host 10.0.0.5 ...]
        DIR/server.crt   self-signed TLS certificate (give this to players: --ca-file)
        DIR/server.key   its private key                        (0600, coordinators only)
        DIR/token.secret HMAC secret that signs player tokens   (0600, coordinators only)
        DIR/cluster.secret password for Raft traffic between coordinators (0600)

    net_keys.py token --dir DIR --player p1 [--hours 24] [--out p1.token]
        A signed token for one roster slot (0600). Hand it to that player only:
        whoever holds it can play as that slot until it expires.

Every replica of a replicated coordinator uses the same DIR contents. The
certificate lists every --host so one file verifies all replicas.
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ipc  # noqa: E402


def _write_private(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


def make_tls(directory: str, hosts: list[str], days: int = 365) -> tuple[str, str]:
    """Write a self-signed EC certificate valid for ``hosts``; return (cert, key) paths."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    sans = []
    for h in hosts:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            sans.append(x509.DNSName(h))
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Syndicate City {hosts[0]}")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path = os.path.join(directory, "server.crt")
    key_path = os.path.join(directory, "server.key")
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    _write_private(key_path, key.private_bytes(serialization.Encoding.PEM,
                                               serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
    return cert_path, key_path


def make_secret(path: str) -> None:
    _write_private(path, secrets.token_hex(32).encode())


def write_token(secret_path: str, player: str, hours: float, out: str) -> None:
    token = ipc.mint_token(ipc.read_private_file(secret_path), player, hours * 3600)
    _write_private(out, token.encode())


def init(directory: str, hosts: list[str]) -> None:
    os.makedirs(directory, mode=0o700, exist_ok=True)
    make_tls(directory, hosts)
    make_secret(os.path.join(directory, "token.secret"))
    make_secret(os.path.join(directory, "cluster.secret"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="create certificate, token secret and cluster secret")
    p_init.add_argument("--dir", required=True)
    p_init.add_argument("--host", action="append", required=True,
                        help="host name or IP players will connect to (repeat for replicas)")
    p_tok = sub.add_parser("token", help="issue a player token")
    p_tok.add_argument("--dir", required=True)
    p_tok.add_argument("--player", required=True, help="roster slot id, e.g. p1")
    p_tok.add_argument("--hours", type=float, default=24.0)
    p_tok.add_argument("--out", help="output file (default DIR/<player>.token)")
    args = ap.parse_args(argv)
    if args.cmd == "init":
        init(args.dir, args.host)
        print(f"wrote {args.dir}/server.crt (public), server.key, token.secret, cluster.secret")
    else:
        out = args.out or os.path.join(args.dir, f"{args.player}.token")
        write_token(os.path.join(args.dir, "token.secret"), args.player, args.hours, out)
        print(f"wrote {out} (valid {args.hours:g} h)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
