"""Allowlisted file scans and versioned cloud envelopes with bounded, idempotent import."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .common import AssistantError, bounded_text, canonical, digest, now_utc, opaque_id, parse_time, require_keys, safe_code, stamp
from .config import Settings
from .extract import extract_bytes, html_text, vtt_text
from .security import ALLOWED_SUFFIXES, DENIED_PARTS, allowed_path, expanded, is_link_or_junction, require_file
from .store import Store

KINDS = {"mail", "teams_message", "transcript", "calendar", "document", "capture", "search_result", "tombstone"}
ENVELOPE_REQUIRED = {"schema_version", "event_id", "kind", "source_id", "title", "content", "source_url", "source_modified_at", "observed_at", "expires_at", "metadata"}


def validate_envelope(raw: dict, cfg: Settings, now: datetime | None = None) -> dict:
    now = now or now_utc()
    require_keys(raw, ENVELOPE_REQUIRED, {"content_type"})
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1 or not isinstance(raw["kind"], str) or raw["kind"] not in KINDS:
        raise AssistantError("ENVELOPE_VERSION_OR_KIND")
    try:
        uuid.UUID(raw["event_id"])
    except (ValueError, TypeError, AttributeError):
        raise AssistantError("EVENT_ID_INVALID") from None
    opaque_id(raw["source_id"])
    bounded_text(raw["title"], "title", 1000, empty=raw["kind"] == "tombstone")
    bounded_text(raw["content"], "content", cfg.max_document_chars, empty=raw["kind"] == "tombstone")
    bounded_text(raw["source_url"], "source_url", 4000, empty=True)
    if not isinstance(raw["metadata"], dict) or len(canonical(raw["metadata"])) > 12000:
        raise AssistantError("METADATA_INVALID")
    # Untrusted envelopes cannot impersonate a locally verified filesystem source.
    if {"origin", "local_path", "root", "file_mtime_ns", "file_size"} & raw["metadata"].keys():
        raise AssistantError("RESERVED_METADATA")
    modified, observed, expires = [parse_time(raw[k]) for k in ("source_modified_at", "observed_at", "expires_at")]
    if modified > now + timedelta(minutes=5) or observed > now + timedelta(minutes=5) or expires <= observed:
        raise AssistantError("ENVELOPE_TIME_INVALID")
    if observed < modified - timedelta(minutes=5):
        raise AssistantError("ENVELOPE_TIME_INVALID")
    if expires <= now and raw["kind"] != "tombstone":
        raise AssistantError("ENVELOPE_EXPIRED")
    if raw["kind"] == "calendar":
        for key in ("start_at", "end_at"):
            if key not in raw["metadata"]:
                raise AssistantError("CALENDAR_TIME_REQUIRED")
            parse_time(raw["metadata"][key])
        if parse_time(raw["metadata"]["end_at"]) <= parse_time(raw["metadata"]["start_at"]):
            raise AssistantError("CALENDAR_RANGE_INVALID")
    return raw


class Ingestor:
    def __init__(self, store: Store, cfg: Settings, home: Path):
        self.store, self.cfg, self.home = store, cfg, home

    def envelope(self, raw: dict, *, namespace: str = "m365", now: datetime | None = None) -> str:
        if not self.cfg.local_data_approved:
            raise AssistantError("LOCAL_DATA_NOT_APPROVED")
        now = now or now_utc()
        raw = validate_envelope(raw, self.cfg, now)
        source_key = namespace + ":" + raw["source_id"]
        payload_hash = digest(canonical(raw))
        if raw["kind"] == "tombstone":
            return self.store.tombstone(source_key, raw["event_id"], payload_hash, stamp(parse_time(raw["observed_at"])))
        content, metadata = raw["content"], dict(raw["metadata"])
        ctype = raw.get("content_type", "text/plain")
        if ctype == "text/html":
            content = html_text(content)
        elif ctype == "text/vtt":
            content, extra = vtt_text(content)
            metadata.update(extra)
        elif ctype != "text/plain":
            raise AssistantError("CONTENT_TYPE_UNSUPPORTED")
        if not content.strip():
            raise AssistantError("DOCUMENT_EMPTY")
        expires = min(parse_time(raw["expires_at"]), parse_time(raw["observed_at"]) + timedelta(hours=self.cfg.cloud_lease_hours), now + timedelta(days=self.cfg.retention_days))
        if expires <= now:
            raise AssistantError("SOURCE_LEASE_EXPIRED")
        doc = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, source_key)), "source_key": source_key,
               "kind": raw["kind"], "title": raw["title"], "body": content,
               "source_url": raw["source_url"], "modified_at": stamp(parse_time(raw["source_modified_at"])),
               "observed_at": stamp(parse_time(raw["observed_at"])), "expires_at": stamp(expires),
               "content_hash": digest(canonical([raw["title"], content, metadata])), "metadata": metadata}
        return self.store.put_document(doc, raw["event_id"], payload_hash)

    def file(self, path: Path, *, return_id: bool = False) -> str:
        path = require_file(path, self.cfg, self.home)
        before = path.stat()
        with path.open("rb") as handle:
            data = handle.read(self.cfg.max_file_bytes + 1)
        after = path.stat()
        if len(data) > self.cfg.max_file_bytes:
            raise AssistantError("FILE_TOO_LARGE")
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise AssistantError("FILE_CHANGED_DURING_READ", retryable=True)
        # Recheck containment after reading, against accidental filesystem changes.
        require_file(path, self.cfg, self.home)
        text, metadata = extract_bytes(data, path.suffix.lower())
        truncated = len(text) > self.cfg.max_document_chars
        text = text[:self.cfg.max_document_chars]
        now = now_utc()
        source_key = "file:" + os.path.normcase(str(path))
        metadata.update({"origin": "local", "local_path": str(path), "file_size": after.st_size,
                         "file_mtime_ns": after.st_mtime_ns, "truncated": truncated,
                         "root": next(r for r in self.cfg.roots if path.is_relative_to(expanded(r).resolve()))})
        doc = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, source_key)), "source_key": source_key,
               "kind": "transcript" if path.suffix.lower() == ".vtt" else "document",
               "title": path.name, "body": text, "source_url": path.as_uri(),
               "modified_at": stamp(datetime.fromtimestamp(after.st_mtime, tz=now.tzinfo)),
               "observed_at": stamp(now), "expires_at": stamp(now + timedelta(days=self.cfg.retention_days)),
               "content_hash": digest(canonical([path.name, text, metadata])), "metadata": metadata}
        result = self.store.put_document(doc, str(uuid.uuid4()), digest(canonical(doc)))
        if return_id:
            self.validate_live_source(doc["id"])
            return doc["id"]
        return result

    def validate_live_source(self, document_id: str) -> dict:
        doc = self.store.get_document(document_id)
        metadata = doc["metadata"]
        if metadata.get("origin") == "local":
            try:
                path = require_file(Path(metadata["local_path"]), self.cfg, self.home)
                info = path.stat()
                if info.st_mtime_ns != metadata["file_mtime_ns"] or info.st_size != metadata["file_size"]:
                    raise AssistantError("SOURCE_CHANGED", "原本が変更されました。再取り込みが必要です")
            except (AssistantError, OSError) as error:
                self.store.invalidate(document_id, "changed" if isinstance(error, AssistantError) and error.code == "SOURCE_CHANGED" else "unavailable")
                raise AssistantError("SOURCE_UNAVAILABLE", "原本を現在確認できないため利用を停止しました") from None
        return doc

    def scan_files(self) -> dict:
        if not self.cfg.local_data_approved:
            return {"status": "disabled", "reason": "LOCAL_DATA_NOT_APPROVED"}
        candidates: list[Path] = []
        errors: dict[str, int] = {}
        for raw_root in self.cfg.roots:
            root = expanded(raw_root)
            try:
                if not root.is_dir() or is_link_or_junction(root):
                    raise AssistantError("ROOT_UNAVAILABLE")
                walked = 0
                for folder, dirs, files in os.walk(root, followlinks=False):
                    walked += 1
                    if walked > 1000 or len(candidates) >= 10000:
                        errors["SCAN_LIMIT"] = 1
                        break
                    dirs[:] = [d for d in dirs if d.lower() not in DENIED_PARTS and not is_link_or_junction(Path(folder) / d)]
                    candidates.extend(Path(folder) / f for f in sorted(files) if Path(f).suffix.lower() in ALLOWED_SUFFIXES)
            except (OSError, AssistantError) as error:
                errors[safe_code(error)] = errors.get(safe_code(error), 0) + 1
        # Rotate bounded batches, so a large directory does not starve later documents.
        candidates.sort(key=str)
        offset = self.store.get_state("scan_offset", 0) % max(1, len(candidates))
        selected = (candidates[offset:] + candidates[:offset])[:100]
        self.store.set_state("scan_offset", offset + len(selected))
        count = 0
        for path in selected:
            try:
                require_file(path, self.cfg, self.home)
                info = path.stat()
                signature = [info.st_size, info.st_mtime_ns]
                key = "file_seen:" + digest(str(path))
                previous = self.store.get_state(key, {})
                current = {"signature": signature, "imported_at": previous.get("imported_at", "") if previous.get("signature") == signature else ""}
                self.store.set_state(key, current)
                if previous.get("signature") != signature:
                    continue  # At least two stable observations.
                if current["imported_at"] and parse_time(current["imported_at"]) > now_utc() - timedelta(hours=self.cfg.cloud_lease_hours):
                    continue
                self.file(path)
                self.store.set_state(key, {"signature": signature, "imported_at": stamp()})
                count += 1
            except (AssistantError, OSError, ValueError) as error:
                code = safe_code(error)
                errors[code] = errors.get(code, 0) + 1
        # Fail closed for inaccessible roots/deleted or changed local sources before reuse.
        for doc in self.store.documents(limit=1000):
            if doc["metadata"].get("origin") == "local":
                try:
                    self.validate_live_source(doc["id"])
                except AssistantError:
                    pass
        return {"imported": count, "candidates": len(candidates), "errors": errors}

    def scan_drops(self, response_handler=None) -> dict:
        if not self.cfg.local_data_approved:
            return {"status": "disabled"}
        imported, errors = 0, {}
        folders = [(expanded(p), "m365") for p in self.cfg.drop_folders]
        folders.append((self.home / "captures", "capture"))
        for folder, namespace in folders:
            if not folder.is_dir():
                continue
            paths = sorted(folder.glob("*.json"))
            cursor_key = "drop_cursor:" + digest(str(folder))
            offset = self.store.get_state(cursor_key, 0) % max(1, len(paths))
            selected = (paths[offset:] + paths[:offset])[:500]
            self.store.set_state(cursor_key, offset + len(selected))
            for path in selected:
                key, sig = "drop_seen:" + digest(str(path)), None
                try:
                    allowed_path(path, [str(folder)])
                    info = path.stat()
                    if info.st_size > 1_000_000:
                        raise AssistantError("ENVELOPE_FILE_TOO_LARGE")
                    sig = [info.st_size, info.st_mtime_ns]
                    key = "drop_seen:" + digest(str(path))
                    prior = self.store.get_state(key, {})
                    if prior.get("signature") != sig:
                        self.store.set_state(key, {"signature": sig})
                        continue
                    if prior.get("processed"):
                        continue
                    data = path.read_bytes()
                    final = path.stat()
                    if [final.st_size, final.st_mtime_ns] != sig:
                        continue
                    raw = json.loads(data.decode("utf-8-sig"))
                    if isinstance(raw, dict) and raw.get("kind") == "bridge_response":
                        if response_handler is None:
                            raise AssistantError("BRIDGE_HANDLER_UNAVAILABLE")
                        response_handler(raw)
                    else:
                        self.envelope(raw, namespace=namespace)
                    self.store.set_state(key, {"signature": sig, "processed": True})
                    imported += 1
                except (OSError, ValueError, TypeError, RecursionError, AssistantError) as error:
                    code = safe_code(error)
                    errors[code] = errors.get(code, 0) + 1
                    if sig is not None and not isinstance(error, OSError) and not getattr(error, "retryable", False):
                        # Keep the original file, quarantine by signature; a corrected file is retried.
                        self.store.set_state(key, {"signature": sig, "processed": True, "error": code})
        return {"imported": imported, "errors": errors}
