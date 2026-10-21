"""Schema for durable execution, scoped memory and automation (SQLite v3)."""

RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_profiles (
 id TEXT PRIMARY KEY,name TEXT NOT NULL,instructions TEXT NOT NULL,capability TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_leases (
 lane TEXT PRIMARY KEY,token TEXT NOT NULL UNIQUE,kind TEXT NOT NULL,expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_jobs (
 id TEXT PRIMARY KEY,agent_id TEXT NOT NULL REFERENCES agent_profiles(id),session TEXT NOT NULL,
 goal TEXT NOT NULL,status TEXT NOT NULL,priority INTEGER NOT NULL DEFAULT 50,parent_id TEXT REFERENCES work_jobs(id),
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,run_after TEXT NOT NULL,deadline TEXT NOT NULL,
 wait_spec TEXT NOT NULL DEFAULT '{}',attempts INTEGER NOT NULL DEFAULT 0,max_attempts INTEGER NOT NULL DEFAULT 3,
 token TEXT,lease_expires TEXT,epoch INTEGER NOT NULL,profile_version INTEGER NOT NULL,
 capability TEXT NOT NULL,document_ids TEXT NOT NULL DEFAULT '[]',checkpoint TEXT NOT NULL DEFAULT '{}',
 result TEXT NOT NULL DEFAULT '{}',error_code TEXT NOT NULL DEFAULT '',
 idempotency_key TEXT UNIQUE,input_hash TEXT NOT NULL,dispatch_kind TEXT NOT NULL DEFAULT 'work',event_key TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS work_ready ON work_jobs(status,run_after,priority);
CREATE INDEX IF NOT EXISTS work_children ON work_jobs(parent_id);
CREATE TABLE IF NOT EXISTS work_runs (
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES work_jobs(id) ON DELETE CASCADE,
 started_at TEXT NOT NULL,ended_at TEXT,status TEXT NOT NULL,error_code TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS work_steps (
 job_id TEXT NOT NULL REFERENCES work_jobs(id) ON DELETE CASCADE,seq INTEGER NOT NULL,
 kind TEXT NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(job_id,seq)
);
CREATE TABLE IF NOT EXISTS work_tool_receipts (
 job_id TEXT NOT NULL REFERENCES work_jobs(id) ON DELETE CASCADE,call_id TEXT NOT NULL,
 signature TEXT NOT NULL,status TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(job_id,call_id)
);
CREATE TABLE IF NOT EXISTS work_artifacts (
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES work_jobs(id) ON DELETE CASCADE,
 name TEXT NOT NULL,version INTEGER NOT NULL,content TEXT NOT NULL,citations TEXT NOT NULL,
 created_at TEXT NOT NULL,UNIQUE(job_id,name,version),UNIQUE(job_id,id)
);
CREATE TABLE IF NOT EXISTS runtime_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,event_key TEXT NOT NULL,
 payload TEXT NOT NULL,occurred_at TEXT NOT NULL,available_at TEXT NOT NULL,expires_at TEXT NOT NULL,revision INTEGER NOT NULL,
 UNIQUE(kind,event_key)
);
CREATE TABLE IF NOT EXISTS routines (
 id TEXT PRIMARY KEY,agent_id TEXT NOT NULL REFERENCES agent_profiles(id),name TEXT NOT NULL,
 instructions TEXT NOT NULL,trigger_kind TEXT NOT NULL,trigger_value TEXT NOT NULL,
 interval_seconds INTEGER NOT NULL,next_at TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_items (
 id TEXT PRIMARY KEY,namespace TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,
 source_kind TEXT NOT NULL,source_id TEXT NOT NULL,source_hash TEXT NOT NULL,document_id TEXT,
 importance REAL NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,expires_at TEXT NOT NULL,
 embedding TEXT,embedding_model TEXT NOT NULL DEFAULT '',UNIQUE(namespace,source_kind,source_id)
);
CREATE INDEX IF NOT EXISTS memory_namespace ON memory_items(namespace,expires_at);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_index USING fts5(tokens,item_id UNINDEXED);
CREATE TABLE IF NOT EXISTS session_summaries (
 session TEXT PRIMARY KEY,through_id INTEGER NOT NULL,summary TEXT NOT NULL,
 source_ids TEXT NOT NULL,epoch INTEGER NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quality_feedback (
 job_id TEXT PRIMARY KEY REFERENCES work_jobs(id) ON DELETE CASCADE,
 helpful INTEGER NOT NULL,intervened INTEGER NOT NULL,retrieval_relevant INTEGER,
 retrieval_total INTEGER,recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_usage (
 job_id TEXT PRIMARY KEY REFERENCES work_jobs(id) ON DELETE CASCADE,calls INTEGER NOT NULL DEFAULT 0,tokens INTEGER NOT NULL DEFAULT 0
);
"""


def invalidate_runtime(cx, document_id: str, *, revised=False, await_revision=False) -> None:
    """Paraphrases/checkpoints are derived data too. Fence first, then erase them."""
    from .common import stamp, canonical
    cx.execute("INSERT INTO state VALUES('data_epoch','1') ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)")
    cx.execute("DELETE FROM session_summaries")
    cx.execute("DELETE FROM memory_index WHERE item_id IN (SELECT id FROM memory_items WHERE source_kind!='user' OR document_id=?)", (document_id,))
    cx.execute("DELETE FROM memory_items WHERE source_kind!='user' OR document_id=?", (document_id,))
    cx.execute("DELETE FROM work_steps")
    cx.execute("DELETE FROM work_tool_receipts")
    cx.execute("DELETE FROM work_artifacts")
    cx.execute("UPDATE work_runs SET status='invalidated',ended_at=? WHERE status='running'", (stamp(),))
    cx.execute("DELETE FROM run_leases")
    # A root goal is an explicit, durable user instruction, not a derived cache.
    # A revision restarts active root work from that goal with fresh source data.
    # Generated child goals are derived data and must not survive invalidation.
    sequence = cx.execute("SELECT value FROM state WHERE key='event_sequence'").fetchone()
    cx.execute("UPDATE work_jobs SET goal=CASE WHEN parent_id IS NULL THEN goal ELSE '資料の更新・失効により終了しました。' END,"
               "checkpoint='{}',result='{}',status=CASE WHEN status='cancelled' THEN status "
               "WHEN ? AND parent_id IS NULL AND status IN ('queued','running','waiting') THEN 'waiting' "
               "WHEN ? AND parent_id IS NULL AND status IN ('queued','running','waiting') THEN 'queued' ELSE 'failed' END,"
               "wait_spec=CASE WHEN ? AND parent_id IS NULL AND status IN ('queued','running','waiting') THEN ? ELSE wait_spec END,"
               "error_code='SOURCE_CHANGED_DURING_TURN',token=NULL,lease_expires=NULL,epoch=(SELECT CAST(value AS INTEGER) FROM state WHERE key='data_epoch'),updated_at=?",
               (int(await_revision), int(revised), int(await_revision), canonical({"kind": "event", "event_kind": "document", "key": document_id, "after_revision": int(sequence[0]) if sequence else 0}), stamp()))
