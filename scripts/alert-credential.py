#!/usr/bin/env python3
"""Enter an alert credential from a terminal into the running engine vault."""

import argparse
import getpass
import json
import os
import socket
import stat
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--key", required=True, help="Vault reference, not a secret value")
    parser.add_argument("--host", required=True, help="API host allowed by the configured service adapter")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("Run interactively in your own terminal; values are never command arguments")
    info = args.socket.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        parser.error("Expected a private socket owned by this user")
    secret = getpass.getpass("API key (hidden): ")
    request = {"key": args.key, "host": args.host, "value": secret}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(str(args.socket))
            client.sendall(json.dumps(request).encode() + b"\n")
            with client.makefile("rb") as reply:
                result = json.loads(reply.readline(1024))
        if result != {"ok": True}:
            raise ValueError("Not stored")
    except Exception:
        print("Credential was not confirmed stored. Check the running service.", file=sys.stderr)
        return 1
    print("Credential stored in the running vault. Return to setup with its reference only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
