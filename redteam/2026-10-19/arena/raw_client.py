"""Speak one NDJSON exchange to the arena broker, as whatever user runs this.

Red-team attackers and graders use this to hit the control socket directly, bypassing the
`envh` client, so they can send malformed/typed/oversized messages the CLI would never send.
Stdlib only, so it runs under `sudo -u rt1 <arena-venv>/bin/python raw_client.py ...`.

Usage:
  raw_client.py '{"op":"status"}'            # send one JSON object
  echo '{"op":"status"}' | raw_client.py -   # read the JSON object from stdin
  raw_client.py --raw '<arbitrary bytes>'    # send exactly these bytes + newline (fuzzing)

Reads ENVH_SOCKET (or --socket PATH). Prints each reply line it receives until the broker
closes the connection or --timeout (default 5s) elapses. Exit code 0 on any clean exchange.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("message", help="JSON object to send, or '-' to read it from stdin")
    parser.add_argument("--socket", default=os.environ.get("ENVH_SOCKET"), help="socket path (default $ENVH_SOCKET)")
    parser.add_argument("--raw", action="store_true", help="send the message bytes verbatim instead of as a JSON line")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--read-replies", type=int, default=8, help="max reply lines to print")
    args = parser.parse_args()

    if not args.socket:
        print("no socket: pass --socket or set ENVH_SOCKET", file=sys.stderr)
        return 2
    payload = sys.stdin.read() if args.message == "-" else args.message
    data = payload.encode() if args.raw else (payload.strip().encode() + b"\n")

    deadline = time.monotonic() + args.timeout
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(args.timeout)
        sock.connect(args.socket)
        sock.sendall(data)
        buffer = b""
        printed = 0
        while printed < args.read_replies and time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer and printed < args.read_replies:
                line, buffer = buffer.split(b"\n", 1)
                sys.stdout.write(line.decode("utf-8", "replace") + "\n")
                sys.stdout.flush()
                printed += 1
        if buffer:  # trailing partial line
            sys.stdout.write(buffer.decode("utf-8", "replace") + "\n")
    except Exception as error:
        print(f"raw_client error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
