"""Asynchronous file mailbox. Never pretends sync latency is an interactive API."""
from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from pathlib import Path

from .common import AssistantError, atomic_json, bounded_text, canonical, digest, now_utc, opaque_id, parse_time, require_keys, stamp
from .config import Settings
from .ingest import Ingestor, validate_envelope
from .security import expanded, is_link_or_junction
from .store import Store

ACTIONS = {"search_mail", "get_calendar", "get_transcript", "search_sharepoint"}


def validate_arguments(action: str, arguments: dict) -> dict:
    if action not in ACTIONS:
        raise AssistantError("BRIDGE_ACTION_BLOCKED")
    if action in ("search_mail", "search_sharepoint"):
        require_keys(arguments, {"query", "limit"})
        bounded_text(arguments["query"], "query", 500)
        if type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= 20:
            raise AssistantError("BRIDGE_LIMIT")
    elif action == "get_calendar":
        require_keys(arguments, {"start", "end"})
        start, end = parse_time(arguments["start"]), parse_time(arguments["end"])
        if not timedelta(0) < end - start <= timedelta(days=14):
            raise AssistantError("BRIDGE_DATE_RANGE")
    else:
        require_keys(arguments, {"meeting_id"})
        opaque_id(arguments["meeting_id"])
    return arguments


class Bridge:
    def __init__(self, store: Store, cfg: Settings, ingestor: Ingestor):
        self.store, self.cfg, self.ingestor = store, cfg, ingestor

    def _folder(self) -> Path:
        if not self.cfg.local_data_approved or not self.cfg.bridge_enabled or not self.cfg.outbox:
            raise AssistantError("BRIDGE_DISABLED", "非同期取得の連携は無効です")
        folder = expanded(self.cfg.outbox)
        if not folder.is_dir():
            raise AssistantError("OUTBOX_UNAVAILABLE")
        for p in (folder, *folder.parents):
            if p.exists() and is_link_or_junction(p):
                raise AssistantError("OUTBOX_LINK_BLOCKED")
        return folder

    def request(self, action: str, arguments: dict, *, idempotency_key: str | None = None) -> dict:
        folder = self._folder()
        arguments = validate_arguments(action, arguments)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key)) if idempotency_key else str(uuid.uuid4())
        now = now_utc()
        expires_at = stamp(now + timedelta(minutes=15))
        with self.store.transaction() as cx:
            if idempotency_key:
                existing = cx.execute("SELECT * FROM bridge_requests WHERE id=?", (request_id,)).fetchone()
                if existing:
                    if existing["action"] != action or existing["arguments"] != canonical(arguments): raise AssistantError("IDEMPOTENCY_CONFLICT")
                    return {"request_id": request_id, "status": existing["status"], "expires_at": existing["expires_at"]}
            previous = cx.execute("SELECT id,expires_at FROM bridge_requests WHERE action=? AND arguments=? AND status='pending' AND expires_at>?", (action, canonical(arguments), stamp(now))).fetchone()
            if previous:
                request_id, expires_at = previous["id"], previous["expires_at"]
            else:
                cx.execute("INSERT INTO bridge_requests(id,action,arguments,created_at,expires_at,status) VALUES(?,?,?,?,?,'pending')", (request_id, action, canonical(arguments), stamp(now), expires_at))
        self.flush()
        return {"request_id": request_id, "status": "pending", "expires_at": expires_at,
                "message": "追加取得を依頼しました。同期を待たず、取得済み情報で回答してください。"}

    def flush(self) -> int:
        folder = self._folder()
        now = stamp()
        with self.store.connection() as cx:
            cx.execute("UPDATE bridge_requests SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
            rows = cx.execute("SELECT * FROM bridge_requests WHERE status='pending' ORDER BY created_at LIMIT 100").fetchall()
        count = 0
        for row in rows:
            target = folder / (row["id"] + ".request.json")
            if not target.exists():
                atomic_json(target, {"schema_version": 1, "request_id": row["id"], "action": row["action"],
                                     "arguments": json.loads(row["arguments"]), "created_at": row["created_at"],
                                     "expires_at": row["expires_at"]})
                count += 1
        return count

    def receive(self, raw: dict) -> str:
        require_keys(raw, {"schema_version", "kind", "request_id", "action", "status", "documents", "error_code", "coverage"})
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1 or raw["kind"] != "bridge_response" or raw["status"] not in ("ok", "error"):
            raise AssistantError("BRIDGE_RESPONSE_INVALID")
        if raw["coverage"] not in ("partial", "complete_for_query", "unknown"):
            raise AssistantError("BRIDGE_COVERAGE_INVALID")
        if not isinstance(raw["documents"], list) or len(raw["documents"]) > 20:
            raise AssistantError("BRIDGE_RESPONSE_LIMIT")
        if not isinstance(raw["error_code"], str) or (raw["error_code"] and not re.fullmatch(r"[A-Z0-9_]{1,80}", raw["error_code"])):
            raise AssistantError("BRIDGE_ERROR_INVALID")
        with self.store.connection() as cx:
            row = cx.execute("SELECT * FROM bridge_requests WHERE id=?", (raw["request_id"],)).fetchone()
        if not row or row["action"] != raw["action"]:
            raise AssistantError("BRIDGE_CORRELATION_FAILED")
        if row["status"] in ("completed", "failed"):
            if json.loads(row["result"]).get("response_hash") != digest(canonical(raw)):
                raise AssistantError("BRIDGE_RESPONSE_COLLISION")
            return "duplicate"
        if row["status"] != "pending" or parse_time(row["expires_at"]) <= now_utc():
            raise AssistantError("BRIDGE_RESPONSE_EXPIRED")
        if (raw["status"] == "ok" and raw["error_code"]) or (raw["status"] == "error" and (raw["documents"] or not raw["error_code"])):
            raise AssistantError("BRIDGE_ERROR_INVALID")
        for doc in raw["documents"]:
            validate_envelope(doc, self.cfg)
            if doc["kind"] == "tombstone":
                raise AssistantError("BRIDGE_TOMBSTONE_BLOCKED")
        for doc in raw["documents"]:
            self.ingestor.envelope(doc)
        with self.store.transaction() as cx:
            cx.execute("UPDATE bridge_requests SET status=?,result=? WHERE id=?", ("completed" if raw["status"] == "ok" else "failed", canonical({"response_hash": digest(canonical(raw)), "coverage": raw["coverage"], "error_code": raw["error_code"], "document_count": len(raw["documents"])}), row["id"]))
            from .state import DurableState
            DurableState._publish(cx, "bridge", row["id"], canonical({"request_id": row["id"]}), now_utc())
        return raw["status"]

    def status(self, request_id: str) -> dict:
        with self.store.connection() as cx:
            row = cx.execute("SELECT id,action,status,expires_at,result FROM bridge_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise AssistantError("BRIDGE_REQUEST_NOT_FOUND")
        result = dict(row)
        details = json.loads(result.pop("result"))
        result["coverage"] = details.get("coverage", "unknown")
        result["error_code"] = details.get("error_code", "")
        if result["status"] == "pending" and parse_time(result["expires_at"]) <= now_utc():
            result["status"] = "expired"
        return result
