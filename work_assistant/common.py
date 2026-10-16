"""Small, dependency-free primitives shared by ingestion and the runtime."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

UTC = timezone.utc
JST = timezone(timedelta(hours=9), "Asia/Tokyo")


class AssistantError(Exception):
    """Safe error: code and public message must never contain source contents/secrets."""
    def __init__(self, code: str, message: str = "", retryable: bool = False):
        super().__init__(message or code)
        self.code, self.message, self.retryable = code, message or code, retryable

    def as_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "retryable": self.retryable}


def now_utc() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime | None = None) -> str:
    value = value or now_utc()
    if value.tzinfo is None:
        raise AssistantError("TIMEZONE_REQUIRED")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_time(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 50:
        raise AssistantError("INVALID_TIMESTAMP")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timezone missing")
        return dt.astimezone(UTC)
    except (ValueError, OverflowError):
        raise AssistantError("INVALID_TIMESTAMP", "タイムゾーン付きRFC3339時刻が必要です") from None


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    """Same-directory replace avoids partial reads; sync delivery still needs idempotency."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".partial-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def bounded_text(value: Any, name: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise AssistantError("INVALID_FIELD", f"{name}の形式または長さが不正です")
    return value


def opaque_id(value: Any) -> str:
    value = bounded_text(value, "id", 2048)
    if any(ord(c) < 32 for c in value):
        raise AssistantError("INVALID_ID")
    return value


def safe_code(error: Exception) -> str:
    return error.code if isinstance(error, AssistantError) else "INTERNAL_ERROR"


def require_keys(obj: Any, required: set[str], optional: set[str] | None = None) -> dict:
    if not isinstance(obj, dict) or not required <= obj.keys() or obj.keys() - required - (optional or set()):
        raise AssistantError("INVALID_OBJECT", "必須項目または許可されない項目があります")
    return obj
