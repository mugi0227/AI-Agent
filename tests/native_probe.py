"""Drive the compiled native host with byte pipes, as a Chromium browser does."""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path


def main() -> None:
    exe, origin, payload_path = sys.argv[1:]
    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    process = subprocess.run(
        [exe, origin], input=struct.pack("=I", len(body)) + body,
        capture_output=True, timeout=35, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    raw = process.stdout
    if len(raw) < 4 or struct.unpack("=I", raw[:4])[0] != len(raw) - 4:
        raise RuntimeError(f"Invalid native reply frame; exit={process.returncode}")
    reply = json.loads(raw[4:].decode("utf-8"))
    # ASCII output keeps PowerShell 5's text pipeline out of the binary test.
    print(json.dumps(reply, ensure_ascii=True))


if __name__ == "__main__":
    main()
