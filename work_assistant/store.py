"""SQLite state, Japanese lexical search, provenance, approval and durable budgets."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .common import AssistantError, JST, canonical, digest, now_utc, parse_time, stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, source_url TEXT NOT NULL,
 modified_at TEXT NOT NULL, observed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 content_hash TEXT NOT NULL, metadata TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS chunks (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(tokens, chunk_id UNINDEXED, document_id UNINDEXED);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, received_at TEXT NOT NULL, result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revocations (source_key TEXT PRIMARY KEY, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT NOT NULL, keywords TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, project_id TEXT NOT NULL,
 document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 quote TEXT NOT NULL, confidence REAL NOT NULL, due_at TEXT, status TEXT NOT NULL,
 created_at TEXT NOT NULL, decided_at TEXT, fingerprint TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id) ON DELETE CASCADE,
 title TEXT NOT NULL, due_at TEXT, status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS conversations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, role TEXT NOT NULL,
 text TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_session ON conversations(session,id);
CREATE TABLE IF NOT EXISTS chat_sessions (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 archived INTEGER NOT NULL DEFAULT 0, draft TEXT NOT NULL DEFAULT '',
 document_ids TEXT NOT NULL DEFAULT '[]', cleared_at TEXT
);
CREATE TABLE IF NOT EXISTS chat_details (
 message_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
 status TEXT NOT NULL DEFAULT 'completed', metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS notifications (
 id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, title TEXT NOT NULL, text TEXT NOT NULL,
 document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL, read_at TEXT
);
CREATE TABLE IF NOT EXISTS bridge_requests (
 id TEXT PRIMARY KEY, action TEXT NOT NULL, arguments TEXT NOT NULL, created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budget (
 day TEXT PRIMARY KEY, calls INTEGER NOT NULL, tokens INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, action TEXT NOT NULL, result TEXT NOT NULL
);
"""


def lexical_tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    parts = re.findall(r"[a-z0-9_]+|[\u3040-\u30ff\u3400-\u9fff]+", normalized)
    tokens: list[str] = []
    for part in parts:
        if re.fullmatch(r"[a-z0-9_]+", part) or len(part) == 1:
            tokens.append(part)
        else:
            tokens.extend(part[i:i + 2] for i in range(len(part) - 1))
    return tokens


def split_chunks(text: str, size: int = 1400, overlap: int = 160) -> list[str]:
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size - overlap)]


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as cx:
            version = cx.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise AssistantError("DB_VERSION_UNSUPPORTED")
            cx.executescript(SCHEMA)
            cx.execute("PRAGMA user_version=2")
            cx.execute("INSERT OR IGNORE INTO chat_sessions(id,title,created_at,updated_at) "
                       "SELECT session,'これまでの会話',min(created_at),max(created_at) FROM conversations GROUP BY session")

    def recover_interrupted(self) -> None:
        # Called once by Runtime startup, never by read-only diagnostics opening the DB.
        with self.connection() as cx:
            cx.execute("UPDATE chat_details SET status='interrupted' WHERE status='running'")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        cx.row_factory = sqlite3.Row
        try:
            cx.execute("PRAGMA journal_mode=WAL")
            cx.execute("PRAGMA foreign_keys=ON")
            cx.execute("PRAGMA busy_timeout=10000")
            yield cx
        finally:
            cx.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as cx:
            cx.execute("BEGIN IMMEDIATE")
            try:
                yield cx
                cx.execute("COMMIT")
            except BaseException:
                cx.execute("ROLLBACK")
                raise

    def audit(self, action: str, result: str) -> None:
        # Fixed codes only; do not put exception strings, prompts, filenames or source bodies here.
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", action + ":" + result):
            raise AssistantError("UNSAFE_AUDIT_VALUE")
        with self.connection() as cx:
            cx.execute("INSERT INTO audit(at,action,result) VALUES(?,?,?)", (stamp(), action, result))

    def put_document(self, doc: dict, event_id: str, payload_hash: str) -> str:
        """Document and delivery receipt are committed together. Older revisions never overwrite."""
        with self.transaction() as cx:
            receipt = cx.execute("SELECT payload_hash FROM events WHERE id=?", (event_id,)).fetchone()
            if receipt:
                if receipt[0] != payload_hash:
                    raise AssistantError("EVENT_ID_COLLISION")
                return "duplicate"
            old = cx.execute("SELECT * FROM documents WHERE source_key=?", (doc["source_key"],)).fetchone()
            revoked = cx.execute("SELECT observed_at FROM revocations WHERE source_key=?", (doc["source_key"],)).fetchone()
            if revoked and doc["observed_at"] <= revoked[0]:
                result = "revoked_revision_ignored"
            elif old and (doc["modified_at"] < old["modified_at"] or doc["observed_at"] < old["observed_at"]):
                result = "older_revision_ignored"
            else:
                result = "updated" if old else "created"
                if old and old["content_hash"] == doc["content_hash"] and old["status"] == "active":
                    result = "refreshed"
                if old and old["id"] != doc["id"]:
                    raise AssistantError("SOURCE_ID_MISMATCH")
                columns = ("id", "source_key", "kind", "title", "body", "source_url", "modified_at",
                           "observed_at", "expires_at", "content_hash", "metadata")
                values = tuple(canonical(doc[c]) if c == "metadata" else doc[c] for c in columns)
                cx.execute("INSERT INTO documents(" + ",".join(columns) + ") VALUES(" + ",".join("?" * len(columns)) + ") "
                           "ON CONFLICT(id) DO UPDATE SET " + ",".join(c + "=excluded." + c for c in columns[1:]) + ",status='active'", values)
                if result != "refreshed":
                    cx.execute("DELETE FROM search_index WHERE document_id=?", (doc["id"],))
                    cx.execute("DELETE FROM chunks WHERE document_id=?", (doc["id"],))
                    # Revised source invalidates prior proposed/confirmed extracts; a human may re-confirm.
                    if old and old["content_hash"] != doc["content_hash"]:
                        self._clear_derived(cx, doc["id"])
                    for index, text in enumerate(split_chunks(doc["body"])):
                        cid = f'{doc["id"]}:{index}'
                        cx.execute("INSERT INTO chunks VALUES(?,?,?,?)", (cid, doc["id"], index, text))
                        tokens = " ".join(lexical_tokens(doc["title"] + " " + text))
                        cx.execute("INSERT INTO search_index(tokens,chunk_id,document_id) VALUES(?,?,?)", (tokens, cid, doc["id"]))
            cx.execute("INSERT INTO events VALUES(?,?,?,?)", (event_id, payload_hash, stamp(), result))
            return result

    @staticmethod
    def _clear_derived(cx: sqlite3.Connection, document_id: str) -> None:
        cx.execute("DELETE FROM proposals WHERE document_id=?", (document_id,))
        cx.execute("DELETE FROM notifications WHERE document_id=?", (document_id,))
        # Conservative: summaries may contain paraphrased source text. Clear ALL chat cache.
        cx.execute("DELETE FROM conversations")
        cx.execute("UPDATE chat_sessions SET title='新しい会話',draft='',document_ids='[]',cleared_at=?", (stamp(),))
        cx.execute("DELETE FROM state WHERE key LIKE 'session_sources:%'")

    def invalidate(self, document_id: str, status: str = "revoked") -> None:
        with self.transaction() as cx:
            cx.execute("DELETE FROM search_index WHERE document_id=?", (document_id,))
            cx.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            self._clear_derived(cx, document_id)
            cx.execute("UPDATE documents SET body='',title='',source_url='',metadata='{}',status=? WHERE id=?", (status, document_id))

    def tombstone(self, source_key: str, event_id: str, payload_hash: str, observed_at: str) -> str:
        with self.transaction() as cx:
            old_event = cx.execute("SELECT payload_hash FROM events WHERE id=?", (event_id,)).fetchone()
            if old_event:
                if old_event[0] != payload_hash:
                    raise AssistantError("EVENT_ID_COLLISION")
                return "duplicate"
            doc = cx.execute("SELECT id,observed_at FROM documents WHERE source_key=?", (source_key,)).fetchone()
            result = "older_revision_ignored" if doc and doc["observed_at"] > observed_at else "revoked"
            cx.execute("INSERT INTO revocations VALUES(?,?) ON CONFLICT(source_key) DO UPDATE SET observed_at=max(revocations.observed_at,excluded.observed_at)", (source_key, observed_at))
            if doc and result == "revoked":
                did = doc["id"]
                cx.execute("DELETE FROM search_index WHERE document_id=?", (did,))
                cx.execute("DELETE FROM chunks WHERE document_id=?", (did,))
                self._clear_derived(cx, did)
                cx.execute("UPDATE documents SET body='',title='',source_url='',metadata='{}',status='revoked' WHERE id=?", (did,))
            cx.execute("INSERT INTO events VALUES(?,?,?,?)", (event_id, payload_hash, stamp(), result))
        return result

    def expire(self, now: datetime | None = None) -> int:
        cutoff = stamp(now)
        with self.connection() as cx:
            ids = [r[0] for r in cx.execute("SELECT id FROM documents WHERE status='active' AND expires_at<=?", (cutoff,))]
        for did in ids:
            self.invalidate(did, "expired")
        return len(ids)

    def get_document(self, document_id: str, now: datetime | None = None) -> dict:
        with self.connection() as cx:
            row = cx.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not row or row["status"] != "active":
            raise AssistantError("SOURCE_UNAVAILABLE")
        if row["expires_at"] <= stamp(now):
            self.invalidate(document_id, "expired")
            raise AssistantError("SOURCE_EXPIRED")
        doc = dict(row)
        doc["metadata"] = json.loads(doc["metadata"])
        return doc

    def documents(self, limit: int = 100, *, kind: str = "") -> list[dict]:
        sql = "SELECT id,kind,title,source_url,modified_at,observed_at,expires_at,metadata,status FROM documents WHERE status='active' AND expires_at>?"
        args: list = [stamp()]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        with self.connection() as cx:
            rows = cx.execute(sql + " ORDER BY observed_at DESC LIMIT ?", (*args, max(1, min(limit, 1000)))).fetchall()
        return [{**dict(r), "metadata": json.loads(r["metadata"])} for r in rows]

    def search(self, query: str, limit: int = 8, *, kind: str = "", now: datetime | None = None) -> list[dict]:
        terms = list(dict.fromkeys(lexical_tokens(query)))[:64]
        if not terms:
            return []
        expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        sql = """SELECT c.id AS chunk_id,c.text,d.id,d.title,d.kind,d.source_url,d.modified_at,d.observed_at,d.expires_at,
                 bm25(search_index) AS rank FROM search_index
                 JOIN chunks c ON c.id=search_index.chunk_id JOIN documents d ON d.id=c.document_id
                 WHERE search_index MATCH ? AND d.status='active' AND d.expires_at>?"""
        args: list = [expression, stamp(now)]
        if kind:
            sql += " AND d.kind=?"
            args.append(kind)
        with self.connection() as cx:
            rows = cx.execute(sql + " ORDER BY rank LIMIT ?", (*args, max(1, min(limit, 20)))).fetchall()
        return [dict(r) for r in rows]

    def add_project(self, name: str, goal: str, keywords: list[str]) -> str:
        pid = str(uuid.uuid4())
        with self.connection() as cx:
            cx.execute("INSERT INTO projects VALUES(?,?,?,?,?)", (pid, name[:200], goal[:4000], canonical(keywords[:30]), stamp()))
        return pid

    def projects(self) -> list[dict]:
        with self.connection() as cx:
            return [{**dict(r), "keywords": json.loads(r["keywords"])} for r in cx.execute("SELECT * FROM projects ORDER BY updated_at DESC LIMIT 50")]

    def propose(self, *, kind: str, title: str, document_id: str, quote: str, confidence: float,
                project_id: str = "", due_at: str | None = None) -> str:
        doc = self.get_document(document_id)
        if kind not in ("action", "decision", "memory") or not quote.strip() or quote not in doc["body"]:
            raise AssistantError("PROVENANCE_INVALID", "候補には原文と一致する引用が必要です")
        if len(title) > 500 or len(quote) > 3000 or not title.strip() or type(confidence) not in (int, float) or not 0 <= confidence <= 1:
            raise AssistantError("PROPOSAL_INVALID")
        if project_id and project_id not in {p["id"] for p in self.projects()}:
            raise AssistantError("PROJECT_NOT_FOUND")
        due_at = stamp(parse_time(due_at)) if due_at else None
        fingerprint = digest(canonical([kind, document_id, title.strip(), project_id, due_at]))
        pid = str(uuid.uuid4())
        with self.transaction() as cx:
            found = cx.execute("SELECT id FROM proposals WHERE fingerprint=?", (fingerprint,)).fetchone()
            if found:
                return found[0]
            cx.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, kind, title.strip(), project_id, document_id, quote, confidence, due_at, "pending", stamp(), None, fingerprint))
        return pid

    def decide_proposal(self, proposal_id: str, approve: bool) -> None:
        """Only called by UI/CLI; deliberately absent from all model tool schemas."""
        with self.transaction() as cx:
            row = cx.execute("SELECT p.*,d.expires_at,d.status AS source_status FROM proposals p JOIN documents d ON d.id=p.document_id WHERE p.id=?", (proposal_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise AssistantError("PROPOSAL_NOT_PENDING")
            if row["source_status"] != "active" or row["expires_at"] <= stamp():
                raise AssistantError("SOURCE_EXPIRED")
            cx.execute("UPDATE proposals SET status=?,decided_at=? WHERE id=?", ("approved" if approve else "rejected", stamp(), proposal_id))
            if approve and row["kind"] == "action":
                cx.execute("INSERT INTO tasks(id,proposal_id,title,due_at) VALUES(?,?,?,?)", (str(uuid.uuid4()), proposal_id, row["title"], row["due_at"]))

    def proposals(self, status: str = "pending") -> list[dict]:
        with self.connection() as cx:
            return [dict(r) for r in cx.execute("SELECT p.* FROM proposals p JOIN documents d ON d.id=p.document_id WHERE p.status=? AND d.status='active' AND d.expires_at>? ORDER BY p.created_at DESC LIMIT 100", (status, stamp()))]

    def tasks(self) -> list[dict]:
        with self.connection() as cx:
            return [dict(r) for r in cx.execute("SELECT t.*,p.document_id,p.quote,p.project_id FROM tasks t JOIN proposals p ON p.id=t.proposal_id JOIN documents d ON d.id=p.document_id WHERE t.status='open' AND d.status='active' AND d.expires_at>? ORDER BY t.due_at LIMIT 100", (stamp(),))]

    def complete_task(self, task_id: str) -> None:
        with self.connection() as cx:
            cx.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))

    @staticmethod
    def _session_id(session: str) -> str:
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session):
            raise AssistantError("SESSION_INVALID")
        return session

    def ensure_session(self, session: str = "main") -> str:
        self._session_id(session)
        with self.connection() as cx:
            cx.execute("INSERT OR IGNORE INTO chat_sessions(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                       (session, "新しい会話", stamp(), stamp()))
        return session

    def new_session(self) -> str:
        return self.ensure_session(str(uuid.uuid4()))

    def sessions(self, query: str = "", *, archived: bool = False) -> list[dict]:
        with self.connection() as cx:
            return [dict(r) for r in cx.execute(
                "SELECT s.*, (SELECT count(*) FROM conversations c WHERE c.session=s.id) AS message_count "
                "FROM chat_sessions s WHERE archived=? AND (instr(lower(title),lower(?))>0 OR EXISTS "
                "(SELECT 1 FROM conversations c WHERE c.session=s.id AND instr(lower(c.text),lower(?))>0)) "
                "ORDER BY updated_at DESC,id LIMIT 200", (int(archived), query[:200], query[:200]))]

    def session(self, session: str) -> dict:
        self.ensure_session(session)
        with self.connection() as cx:
            row = cx.execute("SELECT * FROM chat_sessions WHERE id=?", (session,)).fetchone()
        return {**dict(row), "document_ids": json.loads(row["document_ids"])}

    def rename_session(self, session: str, title: str) -> None:
        if not isinstance(title, str) or not title.strip() or len(title) > 100 or any(ord(c) < 32 for c in title):
            raise AssistantError("SESSION_TITLE_INVALID")
        self.ensure_session(session)
        with self.connection() as cx:
            cx.execute("UPDATE chat_sessions SET title=?,updated_at=? WHERE id=?", (title.strip(), stamp(), session))

    def archive_session(self, session: str, archived: bool = True) -> None:
        self._session_id(session)
        with self.connection() as cx:
            cx.execute("UPDATE chat_sessions SET archived=?,updated_at=? WHERE id=?", (int(archived), stamp(), session))

    def save_draft(self, session: str, text: str, document_ids: list[str], *, expected_cleared_at: str | None = None) -> None:
        if not isinstance(text, str) or len(text) > 50000:
            raise AssistantError("PROMPT_INVALID")
        if len(document_ids) > 6 or any(not isinstance(d, str) or len(d) > 80 for d in document_ids):
            raise AssistantError("ATTACHMENTS_INVALID")
        self.ensure_session(session)
        with self.transaction() as cx:
            revision = cx.execute("SELECT cleared_at FROM chat_sessions WHERE id=?", (session,)).fetchone()[0] or ""
            if expected_cleared_at is not None and revision != expected_cleared_at:
                raise AssistantError("CHAT_CHANGED")
            cx.execute("UPDATE chat_sessions SET draft=?,document_ids=? WHERE id=?", (text, canonical(document_ids), session))

    def clear_session(self, session: str) -> None:
        self._session_id(session)
        with self.transaction() as cx:
            cx.execute("DELETE FROM conversations WHERE session=?", (session,))
            cx.execute("UPDATE chat_sessions SET title='新しい会話',draft='',document_ids='[]',cleared_at=? WHERE id=?", (stamp(), session))
            cx.execute("DELETE FROM state WHERE key=?", ("session_sources:" + session,))

    def remember_turn(self, session: str, role: str, text: str, *, status: str = "completed", metadata: dict | None = None) -> int:
        if role not in ("user", "assistant"):
            raise AssistantError("INVALID_ROLE")
        self.ensure_session(session)
        with self.transaction() as cx:
            cursor = cx.execute("INSERT INTO conversations(session,role,text,created_at) VALUES(?,?,?,?)", (session, role, text[:30000], stamp()))
            mid = cursor.lastrowid
            cx.execute("INSERT INTO chat_details VALUES(?,?,?)", (mid, status, canonical(metadata or {})))
            title = " ".join(text.split())[:48] if role == "user" else "新しい会話"
            cx.execute("UPDATE chat_sessions SET updated_at=?,title=CASE WHEN title='新しい会話' THEN ? ELSE title END WHERE id=?", (stamp(), title, session))
        return mid

    def finish_message(self, message_id: int, status: str, metadata: dict | None = None) -> None:
        with self.connection() as cx:
            if metadata is None:
                cx.execute("UPDATE chat_details SET status=? WHERE message_id=?", (status, message_id))
            else:
                cx.execute("UPDATE chat_details SET status=?,metadata=? WHERE message_id=?", (status, canonical(metadata), message_id))

    def retry_message(self, session: str, message_id: int, prompt: str) -> None:
        with self.transaction() as cx:
            row = cx.execute("SELECT c.*,d.status FROM conversations c JOIN chat_details d ON d.message_id=c.id WHERE c.id=? AND c.session=? AND c.role='user'", (message_id, session)).fetchone()
            latest = cx.execute("SELECT max(id) FROM conversations WHERE session=?", (session,)).fetchone()[0]
            if not row or row["status"] not in ("failed", "cancelled", "interrupted") or row["text"] != prompt or latest != message_id:
                raise AssistantError("RETRY_NOT_AVAILABLE")
            cx.execute("UPDATE chat_details SET status='running' WHERE message_id=?", (message_id,))

    def messages(self, session: str, limit: int = 200) -> list[dict]:
        with self.connection() as cx:
            rows = cx.execute("SELECT c.*,coalesce(d.status,'completed') AS status,coalesce(d.metadata,'{}') AS metadata "
                              "FROM conversations c LEFT JOIN chat_details d ON d.message_id=c.id WHERE c.session=? ORDER BY c.id DESC LIMIT ?", (session, max(1, min(limit, 500)))).fetchall()
        return [{**dict(r), "metadata": json.loads(r["metadata"])} for r in reversed(rows)]

    def history(self, session: str, limit: int = 12) -> list[dict]:
        with self.connection() as cx:
            rows = cx.execute("SELECT c.role,c.text FROM conversations c LEFT JOIN chat_details d ON d.message_id=c.id "
                              "WHERE c.session=? AND coalesce(d.status,'completed')='completed' ORDER BY c.id DESC LIMIT ?", (session, limit)).fetchall()
        return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]

    def search_conversations(self, query: str, session: str) -> list[dict]:
        # Literal substring search, not arbitrary SQL/FTS syntax; deliberately session-scoped.
        with self.connection() as cx:
            return [dict(r) for r in cx.execute("SELECT c.role,c.text,c.created_at FROM conversations c LEFT JOIN chat_details d ON d.message_id=c.id "
                                              "WHERE c.session=? AND coalesce(d.status,'completed')='completed' AND instr(lower(c.text),lower(?))>0 ORDER BY c.id DESC LIMIT 5", (session, query))]

    def get_state(self, key: str, default: Any = None) -> Any:
        with self.connection() as cx:
            row = cx.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self.connection() as cx:
            cx.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, canonical(value)))

    def notify(self, fingerprint: str, title: str, text: str, document_id: str, *, daily_limit: int,
               cooldown_minutes: int, now: datetime | None = None) -> bool:
        now = now or now_utc()
        self.get_document(document_id, now)
        day_start = now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.transaction() as cx:
            if cx.execute("SELECT 1 FROM notifications WHERE fingerprint=?", (fingerprint,)).fetchone():
                return False
            count = cx.execute("SELECT COUNT(*) FROM notifications WHERE created_at>=?", (stamp(day_start),)).fetchone()[0]
            last = cx.execute("SELECT MAX(created_at) FROM notifications").fetchone()[0]
            if count >= daily_limit or (last and parse_time(last) > now - timedelta(minutes=cooldown_minutes)):
                return False
            cx.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?,NULL)", (str(uuid.uuid4()), fingerprint, title[:200], text[:3000], document_id, stamp(now)))
        return True

    def notifications(self, unread: bool = False) -> list[dict]:
        sql = "SELECT n.* FROM notifications n JOIN documents d ON d.id=n.document_id WHERE d.status='active' AND d.expires_at>?"
        if unread:
            sql += " AND n.read_at IS NULL"
        with self.connection() as cx:
            return [dict(r) for r in cx.execute(sql + " ORDER BY n.created_at DESC LIMIT 100", (stamp(),))]

    def mark_read(self, notification_id: str) -> None:
        with self.connection() as cx:
            cx.execute("UPDATE notifications SET read_at=? WHERE id=?", (stamp(), notification_id))

    def reserve_budget(self, token_estimate: int, max_calls: int, max_tokens: int, now: datetime | None = None) -> str:
        day = (now or now_utc()).astimezone(JST).date().isoformat()
        with self.transaction() as cx:
            cx.execute("INSERT OR IGNORE INTO budget VALUES(?,0,0)", (day,))
            row = cx.execute("SELECT * FROM budget WHERE day=?", (day,)).fetchone()
            if row["calls"] >= max_calls or row["tokens"] + token_estimate > max_tokens:
                raise AssistantError("DAILY_BUDGET_EXCEEDED")
            cx.execute("UPDATE budget SET calls=calls+1,tokens=tokens+? WHERE day=?", (token_estimate, day))
        return day

    def settle_budget(self, day: str, reserved: int, actual: int) -> None:
        with self.connection() as cx:
            cx.execute("UPDATE budget SET tokens=max(0,tokens+?) WHERE day=?", (max(0, actual) - reserved, day))

    def stats(self) -> dict:
        with self.connection() as cx:
            return {name: cx.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    for name in ("documents", "chunks", "events", "proposals", "tasks", "notifications")}
