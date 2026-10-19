"""Search a scan for copies of the keys in your vault, approved once on the console. Standard library only.

The broker shares each stored key's first and last few characters and its length, as the console shows them, never
the key. When the scan is done, it says once which of the values found, by that search or otherwise, are stored keys.
Both steps use one connection, so only the scan that was approved gets that answer.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from envh.client.transport import ClientError, Connection, waiting_notice
from envh.common import SEARCH_MIN_LENGTH
from envh.tools.scanner import KeyEnds, VaultSearch, plural

REASON = "envh scan: search your files for copies of your vault's keys"


def wants_search() -> bool:
    print("")
    print("Your envh vault can also find every exact copy of your own keys, even ones the scan can't recognize.")
    print("Those copies are the safest to replace, so the explorer starts with them.")
    print("The scan gets only the first and last few characters and the length of each key, never the key.")
    print(f"You approve it once, on the envh console. Keys under {SEARCH_MIN_LENGTH} characters are left out.")
    print("")
    return input("Search for copies of your vault's keys too? [Y/n] ").strip().lower() not in ("n", "no")


def key_ends(reply: dict[str, Any]) -> KeyEnds:
    keys = reply.get("keys")
    if not isinstance(keys, list) or not all(well_formed(key) for key in keys):
        raise ClientError("the broker's answer to the search is malformed")
    return KeyEnds([(key["start"], key["end"], key["length"]) for key in keys])


def well_formed(key: Any) -> bool:
    if not isinstance(key, dict) or not all(isinstance(key.get(part), str) and key[part] for part in ("start", "end")):
        return False
    return isinstance(key.get("length"), int) and key["length"] >= len(key["start"]) + len(key["end"])


def confirm(connection: Connection, ids: list[str]) -> dict[str, list[str]] | None:
    """Which of the found ids are stored keys, or None, said on stderr, when the broker can't be asked."""
    try:
        connection.send(op="match", ids=ids)
        matches = connection.recv_ok().get("matches")
    except (ClientError, OSError) as error:
        print(f"envh scan: couldn't check the found keys against your vault: {error}", file=sys.stderr)
        return None
    if not isinstance(matches, dict):
        print("envh scan: couldn't check the found keys against your vault: the broker's answer is malformed", file=sys.stderr)
        return None
    return matches


@contextmanager
def vault_search(sock: Path) -> Iterator[VaultSearch | None]:
    """Asks first. None when the person says no, or when the broker is not running or the console denies it; the scan
    then goes on without the vault."""
    if not wants_search():
        yield None
        return
    try:
        connection = Connection(sock)
    except ClientError as error:
        print(f"Not searching for your vault's keys: {error}", file=sys.stderr)
        yield None
        return
    with connection:
        try:
            connection.send(op="search", reason=REASON)
            reply = connection.recv_ok()
            with waiting_notice(reply, None):
                reply = connection.recv_ok()
            keys = key_ends(reply)
        except ClientError as error:
            print(f"Not searching for your vault's keys: {error}", file=sys.stderr)
            yield None
            return
        print(f"Searching for copies of {plural(keys.count, 'vault key')} too.", flush=True)
        yield VaultSearch(keys, lambda ids: confirm(connection, ids))
