"""Pipe two explicitly exported browser requests into `ttdm import --stdin`."""

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("inbox", type=Path)
parser.add_argument("socket", type=Path)
args = parser.parse_args()
for path in (args.inbox, args.socket):
    if path.is_symlink() or path.stat().st_mode & 0o077:
        parser.error("Captures contain credentials. Run chmod 600 on both files first.")
print(json.dumps({"inbox": args.inbox.read_text(), "socket": args.socket.read_text()}))
