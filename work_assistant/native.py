"""One explicit Edge capture per process; no server, credentials, or arbitrary execution."""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO

from .common import AssistantError, atomic_json, bounded_text, digest, now_utc, require_keys, safe_code, stamp
from .config import Settings, default_home, load
from .security import safe_web_url

MAX_FRAME = 900_000


def read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = stream.read(size - len(chunks))
        if not part:
            raise AssistantError("NATIVE_TRUNCATED_FRAME")
        chunks.extend(part)
    return bytes(chunks)


def read_frame(stream: BinaryIO) -> dict:
    length = struct.unpack("=I", read_exact(stream, 4))[0]
    if not 0 < length <= MAX_FRAME:
        raise AssistantError("NATIVE_FRAME_LIMIT")
    try:
        value = json.loads(read_exact(stream, length).decode("utf-8"))
    except (ValueError, UnicodeError):
        raise AssistantError("NATIVE_INVALID_JSON") from None
    if not isinstance(value, dict):
        raise AssistantError("NATIVE_INVALID_JSON")
    return value


def write_frame(stream: BinaryIO, value: dict) -> None:
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise AssistantError("NATIVE_FRAME_LIMIT")
    stream.write(struct.pack("=I", len(data)) + data)
    stream.flush()


def capture(raw: dict, cfg: Settings, home: Path, origin: str) -> dict:
    if origin.rstrip("/") not in {"chrome-extension://" + x for x in cfg.extension_ids}:
        raise AssistantError("NATIVE_ORIGIN_BLOCKED")
    if raw.get("operation") == "ping":
        require_keys(raw, {"operation"})
        return {"status": "ok", "protocol": 1, "capture_enabled": cfg.local_data_approved}
    require_keys(raw, {"operation", "url", "title", "text", "scope"})
    if raw["operation"] != "capture" or raw["scope"] not in ("selection", "visible_page"):
        raise AssistantError("NATIVE_OPERATION_BLOCKED")
    if not cfg.local_data_approved:
        raise AssistantError("LOCAL_DATA_NOT_APPROVED")
    url = safe_web_url(raw["url"], cfg.allowed_web_hosts)
    title = bounded_text(raw["title"], "title", 1000)
    text = bounded_text(raw["text"], "text", min(60000, cfg.max_document_chars))
    now, event_id = now_utc(), str(uuid.uuid4())
    envelope = {"schema_version": 1, "event_id": event_id, "kind": "capture",
                "source_id": "edge:" + digest(url + "\n" + text), "title": title, "content": text,
                "content_type": "text/plain", "source_url": url, "source_modified_at": stamp(now),
                "observed_at": stamp(now), "expires_at": stamp(now + timedelta(hours=cfg.cloud_lease_hours)),
                "metadata": {"coverage": raw["scope"], "modified_time_is_capture_time": True,
                             "user_initiated": True}}
    atomic_json(home / "captures" / (event_id + ".json"), envelope)
    return {"status": "saved_locally", "event_id": event_id, "scope": raw["scope"],
            "message": "ローカル受信箱へ保存しました。常駐アプリが取り込みます。"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=default_home())
    parser.add_argument("origin")
    parser.add_argument("--parent-window")
    args = parser.parse_args(argv)
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    try:
        answer = capture(read_frame(sys.stdin.buffer), load(args.home), args.home, args.origin)
    except (AssistantError, OSError, ValueError) as error:
        answer = {"status": "error", "error": safe_code(error)}
    write_frame(sys.stdout.buffer, answer)
    return 0 if answer["status"] != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
