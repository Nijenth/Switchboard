#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

GENESIS = {"chain_seq": 0, "row_hash": "GENESIS"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify(url, token, checkpoint):
    endpoint = urlsplit(url)
    if (
        endpoint.username is not None or endpoint.password is not None
        or endpoint.query or endpoint.fragment or not endpoint.hostname
        or (endpoint.scheme != "https" and not (
            endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "::1"}
        ))
    ):
        raise ValueError("Use HTTPS or an explicit local loopback audit URL")
    request = Request(
        url, data=json.dumps(checkpoint).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    with build_opener(NoRedirect).open(request, timeout=10) as response:
        return json.loads(response.read(4097))


def valid(checkpoint):
    if not isinstance(checkpoint, dict) or checkpoint.keys() != GENESIS.keys():
        return False
    seq, row_hash = checkpoint["chain_seq"], checkpoint["row_hash"]
    return type(seq) is int and (
        (seq == 0 and row_hash == "GENESIS")
        or (0 < seq < 2**63 and isinstance(row_hash, str) and len(row_hash) == 64
            and all(char in "0123456789abcdef" for char in row_hash))
    )


def advance(path, url, token, initialize=False):
    # Serialize independent scheduler invocations with an exclusive lock file.
    lock = path.with_suffix(path.suffix + ".lock")
    handle = lock.open("x")
    try:
        with handle:
            if initialize and path.exists():
                raise ValueError("Checkpoint already exists")
            previous = GENESIS
            if not initialize:
                history = path.read_bytes()
                if not history.endswith(b"\n"):
                    raise ValueError("Checkpoint history is empty or incomplete")
                previous = json.loads(history.splitlines()[-1])
                if not valid(previous):
                    raise ValueError("Stored checkpoint is invalid")
            head = verify(url, token, previous)
            if not valid(head) or head["chain_seq"] < previous["chain_seq"]:
                raise ValueError("Invalid or regressed audit head")
            if head["chain_seq"] == previous["chain_seq"] and head != previous:
                raise ValueError("Audit hash changed without advancing the sequence")
            if initialize or head != previous:
                flags = os.O_WRONLY | os.O_APPEND | (os.O_CREAT | os.O_EXCL if initialize else 0)
                with os.fdopen(os.open(path, flags, 0o600), "a", encoding="utf-8", newline="\n") as output:
                    output.write(json.dumps(head, separators=(",", ":")) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
            return head["chain_seq"]
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description="Verify and advance a separately stored audit checkpoint")
    parser.add_argument("history", type=Path)
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    try:
        url, token = os.environ["SWITCHBOARD_AUDIT_URL"], os.environ["SWITCHBOARD_AUDIT_TOKEN"]
        seq = advance(args.history, url, token, args.initialize)
    except HTTPError as error:
        parser.exit(1, f"Audit verification returned HTTP {error.code}\n")
    except (OSError, URLError, ValueError, KeyError):
        parser.exit(1, "Audit checkpoint could not be verified or advanced\n")
    print(f"Verified audit sequence {seq}")


if __name__ == "__main__":
    main()
