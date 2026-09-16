#!/usr/bin/env python3
"""Drive the running alert setup from a local terminal, with a resumable transcript."""

import argparse
import json
import os
import re
import socket
import stat
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.alert_diagnosis import public_text  # noqa: E402
from src.core.alert_operator import peer_uid, proof_key, signature  # noqa: E402
from src.core.base import resolve_vault_key_file  # noqa: E402


def exchange(path, key, body):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("Expected a private operator socket owned by this user")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(180)
        client.connect(str(path))
        if peer_uid(client) != os.getuid():
            raise PermissionError("Server peer user mismatch")
        with client.makefile("rb") as reader:
            greeting = json.loads(reader.readline(1024))
            challenge = greeting.get("challenge", "") if isinstance(greeting, dict) else ""
            if not isinstance(challenge, str) or not re.fullmatch(r"[0-9a-f]{64}", challenge):
                raise ValueError("Invalid operator challenge")
            client.sendall(json.dumps({"body": body, "proof": signature(key, challenge, body)}).encode() + b"\n")
            reply = json.loads(reader.readline(131072))
    if not isinstance(reply, dict):
        raise ValueError("Invalid operator response")
    if reply.get("ok") is not True:
        raise ValueError(public_text(reply.get("error", "Operator request was not confirmed")))
    return reply


def write_journal(path, data, *, fresh=False):
    raw = (json.dumps(data, indent=2) + "\n").encode()
    if fresh:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("Expected an owned private journal")
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def input_text(text):
    return public_text(re.sub(r"(https?://)[^/\s]+@", r"\1[credentials redacted]@", text), 2000)


def run(args):
    # This is proof of machine-owner access, not a second vault instance.
    key = proof_key(args.key_file)
    if args.journal.exists():
        info = args.journal.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("Expected an owned private journal")
        journal = json.loads(args.journal.read_text())
        if (args.parent and args.parent != journal["parent"]) or (args.account and args.account != journal["account"]):
            raise ValueError("The journal is bound to a different parent or account")
    else:
        if args.action != "run" or not args.parent or not args.account:
            raise ValueError("A new rehearsal needs --parent and --account")
        journal = {"session": str(uuid.uuid4()), "parent": args.parent, "account": args.account, "transcript": []}
        write_journal(args.journal, journal, fresh=True)  # Intent precedes any remote request.
    session = journal["session"]
    if args.action != "run":
        result = exchange(args.socket, key, {"operation": args.action, "session": session})
        journal["last_status"] = result
        write_journal(args.journal, journal)
        print(json.dumps(result, indent=2))
        return
    if not args.inputs:
        raise ValueError("A scripted run needs --inputs pointing to a JSON array of setup answers")
    with args.inputs.open("rb") as stream:
        raw_inputs = stream.read(131073)
    if len(raw_inputs) > 131072:
        raise ValueError("Inputs file exceeds 128 KiB")
    inputs = json.loads(raw_inputs)
    if not isinstance(inputs, list) or len(inputs) > 32 or any(not isinstance(x, str) or len(x) > 2000 for x in inputs):
        raise ValueError("Inputs must be at most 32 strings of at most 2000 characters")
    result = exchange(
        args.socket,
        key,
        {
            "operation": "start",
            "session": session,
            "parent": journal["parent"],
            "account": journal["account"],
        },
    )
    print("LOCAL OPERATOR REHEARSAL " + session)
    print("BOT: " + (result.get("reply") or "Inspect the existing step journal before continuing."))
    for sequence, text in enumerate(inputs):
        print("OPERATOR: " + input_text(text), flush=True)
        result = exchange(
            args.socket, key, {"operation": "answer", "session": session, "sequence": sequence, "text": text}
        )
        entry = {
            "sequence": sequence,
            "input": input_text(text),
            "reply": result.get("reply"),
            "state": result["source_state"],
        }
        prior = [x for x in journal["transcript"] if x["sequence"] != sequence]
        journal["transcript"] = sorted([*prior, entry], key=lambda x: x["sequence"])
        journal["last_status"] = result
        write_journal(args.journal, journal)
        print("BOT: " + (result.get("reply") or "No reply was confirmed."), flush=True)
    print("Registration: " + result["source_id"] + ". State: " + result["source_state"])
    print(
        "Use --action status with this journal to read activation or held notices. "
        "A completed script is not activation."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--key-file", type=Path, default=resolve_vault_key_file())
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--parent", help="Full UUID of an active registration; it is never altered")
    parser.add_argument("--account", help="That registration's bot account")
    parser.add_argument("--inputs", type=Path, help="JSON array of literal setup answers, including CREATE if intended")
    parser.add_argument("--action", choices=("run", "status", "resume", "stop"), default="run")
    args = parser.parse_args()
    try:
        run(args)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Rehearsal not confirmed: " + public_text(str(error)), file=sys.stderr)
        print(
            "Keep the journal. Inspect status before resuming; do not create a fresh session to retry.", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
