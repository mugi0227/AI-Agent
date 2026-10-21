"""Transactional task queue, event inbox, leases, profiles and schedules.

There is no model in this module. Every state transition can be tested without
inference. Lease tokens fence stale writers across processes, not only threads.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import timedelta

from .common import AssistantError, bounded_text, canonical, digest, now_utc, parse_time, stamp
from .policy import ALL_TOOLS, WORK_TOOLS, BASE_TOOLS, Capability, execution_guard

TERMINAL = {"completed", "failed", "cancelled"}


class DurableState:
    def __init__(self, store):
        self.store = store
        with store.transaction() as cx:
            for aid, name, tools in (("main", "業務アシスタント", WORK_TOOLS), ("monitor", "定期確認", BASE_TOOLS | {"notify"})):
                cap = Capability(aid, frozenset(tools))
                cx.execute("INSERT OR IGNORE INTO agent_profiles VALUES(?,?,?,?,1,?)", (aid, name, "", canonical(cap.as_dict()), stamp()))

    @staticmethod
    def epoch(cx) -> int:
        row = cx.execute("SELECT value FROM state WHERE key='data_epoch'").fetchone()
        return int(row[0]) if row else 0

    def profile(self, agent_id="main") -> dict:
        with self.store.connection() as cx:
            row = cx.execute("SELECT * FROM agent_profiles WHERE id=?", (agent_id,)).fetchone()
        if not row:
            raise AssistantError("AGENT_NOT_FOUND")
        return {**dict(row), "capability": Capability.from_dict(json.loads(row["capability"]))}

    def profiles(self) -> list[dict]:
        with self.store.connection() as cx:
            ids = [r[0] for r in cx.execute("SELECT id FROM agent_profiles ORDER BY id")]
        return [self.profile(a) for a in ids]

    def set_profile(self, agent_id, name, instructions="", *, tools=None, document_ids=None, project_ids=None) -> None:
        self.store._session_id(agent_id)
        bounded_text(name, "name", 100); bounded_text(instructions, "instructions", 4000, empty=True)
        selected = frozenset(BASE_TOOLS if tools is None else tools)
        if not selected <= ALL_TOOLS:
            raise AssistantError("TOOL_POLICY_DENIED")
        for ids in (document_ids, project_ids):
            if ids is not None and (not isinstance(ids, list) or len(ids) > 200 or any(not isinstance(i, str) or len(i) > 80 for i in ids)):
                raise AssistantError("SCOPE_INVALID")
        cap = Capability(agent_id, selected, None if document_ids is None else frozenset(document_ids),
                         None if project_ids is None else frozenset(project_ids))
        with self.store.transaction() as cx:
            old = cx.execute("SELECT * FROM agent_profiles WHERE id=?", (agent_id,)).fetchone()
            changed = old and (old["capability"] != canonical(cap.as_dict()) or old["instructions"] != instructions)
            if old and not changed:
                cx.execute("UPDATE agent_profiles SET name=?,updated_at=? WHERE id=?", (name, stamp(), agent_id))
                return
            cx.execute("INSERT INTO agent_profiles VALUES(?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                       "instructions=excluded.instructions,capability=excluded.capability,version=agent_profiles.version+1,updated_at=excluded.updated_at",
                       (agent_id, name, instructions, canonical(cap.as_dict()), stamp()))
            if changed:
                ids = [r[0] for r in cx.execute("SELECT id FROM work_jobs WHERE agent_id=?", (agent_id,))]
                for jid in ids:
                    ids.extend(r[0] for r in cx.execute("SELECT id FROM work_jobs WHERE parent_id=?", (jid,)) if r[0] not in ids)
                    job = cx.execute("SELECT token FROM work_jobs WHERE id=?", (jid,)).fetchone()
                    cx.execute("DELETE FROM run_leases WHERE token=?", (job[0],))
                    cx.execute("UPDATE work_runs SET status='failed',error_code='POLICY_CHANGED',ended_at=? WHERE id=? AND status='running'", (stamp(), job[0]))
                    for table in ("work_steps", "work_tool_receipts", "work_artifacts"):
                        cx.execute(f"DELETE FROM {table} WHERE job_id=?", (jid,))
                    cx.execute("UPDATE work_jobs SET status=CASE WHEN status='cancelled' THEN status ELSE 'failed' END,token=NULL,lease_expires=NULL,"
                               "checkpoint='{}',result='{}',error_code='POLICY_CHANGED',goal=CASE WHEN parent_id IS NULL THEN goal ELSE '権限設定の変更により終了しました。' END,updated_at=? WHERE id=?", (stamp(), jid))
                if agent_id == "main":
                    cx.execute("DELETE FROM conversations")
                    cx.execute("DELETE FROM session_summaries")
                    cx.execute("DELETE FROM state WHERE key LIKE 'session_sources:%'")
                    cx.execute("UPDATE chat_sessions SET title='新しい会話',draft='',document_ids='[]',cleared_at=?", (stamp(),))

    def submit(self, goal, *, agent_id="main", document_ids=None, parent_id=None, priority=50,
               run_after=None, deadline=None, idempotency_key=None, capability=None,
               dispatch_kind="work", event_key="", now=None) -> str:
        bounded_text(goal, "goal", 10000)
        now = now or now_utc()
        due = parse_time(deadline) if deadline else now + timedelta(days=7)
        after = parse_time(run_after) if run_after else now
        if due <= now or due > now + timedelta(days=30) or after >= due or type(priority) is not int or not 0 <= priority <= 100:
            raise AssistantError("JOB_SCHEDULE_INVALID")
        if dispatch_kind not in ("work", "monitor"):
            raise AssistantError("JOB_KIND_INVALID")
        docs = list(dict.fromkeys(document_ids or []))
        if len(docs) > 6 or any(not isinstance(d, str) or len(d) > 80 for d in docs):
            raise AssistantError("ATTACHMENTS_INVALID")
        profile = self.profile(agent_id)
        cap = profile["capability"] if capability is None else profile["capability"].attenuate(capability)
        for did in docs:
            if not cap.source_allowed(did): raise AssistantError("SOURCE_SCOPE_DENIED")
            self.store.get_document(did, now)
        jid = str(uuid.uuid4())
        signature = digest(canonical([goal, agent_id, docs, parent_id, dispatch_kind, event_key]))
        if idempotency_key is not None:
            bounded_text(idempotency_key, "idempotency_key", 200)
        with self.store.transaction() as cx:
            if idempotency_key:
                row = cx.execute("SELECT id,input_hash FROM work_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if row:
                    if row["input_hash"] != signature: raise AssistantError("IDEMPOTENCY_CONFLICT")
                    return row["id"]
            if parent_id:
                parent = cx.execute("SELECT * FROM work_jobs WHERE id=?", (parent_id,)).fetchone()
                if not parent or parent["status"] != "running": raise AssistantError("PARENT_NOT_RUNNING")
                if cx.execute("SELECT count(*) FROM work_jobs WHERE parent_id=?", (parent_id,)).fetchone()[0] >= 6:
                    raise AssistantError("DELEGATION_LIMIT")
                depth, cursor = 0, parent
                while cursor["parent_id"]:
                    depth += 1
                    cursor = cx.execute("SELECT * FROM work_jobs WHERE id=?", (cursor["parent_id"],)).fetchone()
                if depth >= 2: raise AssistantError("DELEGATION_LIMIT")
                cap = Capability.from_dict(json.loads(parent["capability"])).attenuate(cap)
                if any(not cap.source_allowed(d) for d in docs): raise AssistantError("SOURCE_SCOPE_DENIED")
                due = min(due, parse_time(parent["deadline"]))
            cx.execute("INSERT INTO work_jobs(id,agent_id,session,goal,status,priority,parent_id,created_at,updated_at,run_after,deadline,"
                       "epoch,profile_version,capability,document_ids,idempotency_key,input_hash,dispatch_kind,event_key) "
                       "VALUES(?,?,?,?, 'queued',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (jid, agent_id, "work-" + jid, goal, priority, parent_id, stamp(now), stamp(now), stamp(after), stamp(due),
                        self.epoch(cx), profile["version"], canonical(cap.as_dict()), canonical(docs), idempotency_key, signature, dispatch_kind, event_key))
        return jid

    @staticmethod
    def _job(row) -> dict:
        if not row: raise AssistantError("JOB_NOT_FOUND")
        result = dict(row)
        for key in ("wait_spec", "capability", "document_ids", "checkpoint", "result"):
            if key in result: result[key] = json.loads(result[key])
        return result

    def job(self, job_id) -> dict:
        with self.store.connection() as cx:
            return self._job(cx.execute("SELECT * FROM work_jobs WHERE id=?", (job_id,)).fetchone())

    def jobs(self, limit=100) -> list[dict]:
        with self.store.connection() as cx:
            return [dict(r) for r in cx.execute("SELECT id,agent_id,parent_id,goal,status,attempts,max_attempts,updated_at,created_at,"
                    "error_code,run_after,deadline,dispatch_kind FROM work_jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),))]

    def publish(self, kind, key, payload=None, *, debounce_seconds=0, now=None) -> None:
        if kind not in ("document", "bridge", "user", "work_completed"):
            raise AssistantError("EVENT_KIND_INVALID")
        bounded_text(key, "event_key", 200)
        data = canonical(payload or {})
        if len(data) > 4000: raise AssistantError("EVENT_TOO_LARGE")
        now = now or now_utc()
        with self.store.transaction() as cx:
            self._publish(cx, kind, key, data, now, debounce_seconds)

    @staticmethod
    def _publish(cx, kind, key, data, now, debounce=0):
        existing = cx.execute("SELECT payload FROM runtime_events WHERE kind=? AND event_key=?", (kind, key)).fetchone()
        if existing and existing[0] == data: return
        cx.execute("INSERT INTO state VALUES('event_sequence','1') ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)")
        revision = int(cx.execute("SELECT value FROM state WHERE key='event_sequence'").fetchone()[0])
        cx.execute("INSERT INTO runtime_events(kind,event_key,payload,occurred_at,available_at,expires_at,revision) VALUES(?,?,?,?,?,?,?) "
                   "ON CONFLICT(kind,event_key) DO UPDATE SET payload=excluded.payload,occurred_at=excluded.occurred_at,"
                   "available_at=excluded.available_at,expires_at=excluded.expires_at,revision=excluded.revision",
                   (kind, key, data, stamp(now), stamp(now + timedelta(seconds=debounce)), stamp(now + timedelta(days=7)), revision))

    def _recover(self, cx, now):
        cutoff = stamp(now)
        expired = cx.execute("SELECT id,token,attempts,max_attempts FROM work_jobs WHERE status='running' AND lease_expires<=?", (cutoff,)).fetchall()
        for row in expired:
            cx.execute("UPDATE work_runs SET status='interrupted',ended_at=?,error_code='LEASE_EXPIRED' WHERE id=?", (cutoff, row["token"]))
            cx.execute("UPDATE work_jobs SET status=?,token=NULL,lease_expires=NULL,error_code='LEASE_EXPIRED',updated_at=? WHERE id=?",
                       ("queued" if row["attempts"] < row["max_attempts"] else "failed", cutoff, row["id"]))
        cx.execute("DELETE FROM run_leases WHERE expires_at<=?", (cutoff,))
        cx.execute("DELETE FROM runtime_events WHERE expires_at<=?", (cutoff,))
        overdue = cx.execute("SELECT token FROM work_jobs WHERE status='running' AND deadline<=?", (cutoff,)).fetchall()
        for row in overdue:
            cx.execute("DELETE FROM run_leases WHERE token=?", (row["token"],))
            cx.execute("UPDATE work_runs SET status='failed',error_code='JOB_DEADLINE',ended_at=? WHERE id=?", (cutoff, row["token"]))
        cx.execute("UPDATE work_jobs SET status='failed',error_code='JOB_DEADLINE',checkpoint='{}',token=NULL,lease_expires=NULL,updated_at=? "
                   "WHERE status IN ('queued','waiting','running') AND deadline<=?", (cutoff, cutoff))
        for row in cx.execute("SELECT id,wait_spec FROM work_jobs WHERE status='waiting'").fetchall():
            wait = json.loads(row["wait_spec"])
            ready = wait.get("kind") == "timer" and wait["at"] <= cutoff
            if wait.get("kind") == "children":
                ids = wait["ids"]
                states = [cx.execute("SELECT status FROM work_jobs WHERE id=? AND parent_id=?", (i, row["id"])).fetchone() for i in ids]
                ready = bool(states) and all(r and r[0] in TERMINAL for r in states)
            elif wait.get("kind") == "bridge":
                req = cx.execute("SELECT status,expires_at FROM bridge_requests WHERE id=?", (wait["request_id"],)).fetchone()
                ready = bool(req and (req["status"] != "pending" or req["expires_at"] <= cutoff))
            elif wait.get("kind") == "approval":
                proposal = cx.execute("SELECT status FROM proposals WHERE id=?", (wait["proposal_id"],)).fetchone()
                ready = proposal is None or proposal[0] != "pending"
            elif wait.get("kind") == "event":
                if "after_revision" in wait:
                    ready = cx.execute("SELECT 1 FROM runtime_events WHERE kind=? AND event_key=? AND available_at<=? AND revision>?",
                                       (wait["event_kind"], wait["key"], cutoff, wait["after_revision"])).fetchone() is not None
                else:
                    ready = cx.execute("SELECT 1 FROM runtime_events WHERE kind=? AND event_key=? AND available_at<=? AND occurred_at>?",
                                       (wait["event_kind"], wait["key"], cutoff, wait["after"])).fetchone() is not None
            if ready:
                cx.execute("UPDATE work_jobs SET status='queued',run_after=?,updated_at=? WHERE id=?", (cutoff, cutoff, row["id"]))

    def claim(self, *, max_workers=2, lease_seconds=45, now=None, job_id=None, allowed_kinds=None) -> dict | None:
        now = now or now_utc(); cutoff = stamp(now)
        with self.store.transaction() as cx:
            self._recover(cx, now)
            if cx.execute("SELECT 1 FROM run_leases WHERE kind='foreground'").fetchone(): return None
            if cx.execute("SELECT count(*) FROM run_leases WHERE kind='work'").fetchone()[0] >= max_workers: return None
            if cx.execute("SELECT count(*) FROM run_leases").fetchone()[0] >= max_workers + 1: return None
            query = "SELECT * FROM work_jobs WHERE status='queued' AND run_after<=?"
            args = [cutoff]
            if job_id: query += " AND id=?"; args.append(job_id)
            for row in cx.execute(query + " ORDER BY priority,created_at LIMIT 100", args).fetchall():
                if allowed_kinds is not None and row["dispatch_kind"] not in allowed_kinds: continue
                lane = row["agent_id"] + ":" + row["session"]
                if cx.execute("SELECT 1 FROM run_leases WHERE lane=?", (lane,)).fetchone(): continue
                profile = cx.execute("SELECT version FROM agent_profiles WHERE id=?", (row["agent_id"],)).fetchone()
                if profile[0] != row["profile_version"]:
                    cx.execute("UPDATE work_jobs SET status='failed',error_code='POLICY_CHANGED',updated_at=? WHERE id=?", (cutoff, row["id"]))
                    continue
                token, expiry = uuid.uuid4().hex, stamp(now + timedelta(seconds=lease_seconds))
                cx.execute("INSERT INTO run_leases VALUES(?,?, 'work',?)", (lane, token, expiry))
                cx.execute("UPDATE work_jobs SET status='running',attempts=attempts+1,token=?,lease_expires=?,updated_at=? WHERE id=?", (token, expiry, cutoff, row["id"]))
                cx.execute("INSERT INTO work_runs(id,job_id,started_at,status) VALUES(?,?,?,'running')", (token, row["id"], cutoff))
                return self._job(cx.execute("SELECT * FROM work_jobs WHERE id=?", (row["id"],)).fetchone())
        return None

    def assert_job(self, cx, job_id, token, *, now=None):
        row = cx.execute("SELECT * FROM work_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "running" or row["token"] != token or row["lease_expires"] <= stamp(now):
            raise AssistantError("STALE_RUN")
        if row["epoch"] != self.epoch(cx): raise AssistantError("SOURCE_CHANGED_DURING_TURN")
        if row["deadline"] <= stamp(now): raise AssistantError("JOB_DEADLINE")
        profile = cx.execute("SELECT version FROM agent_profiles WHERE id=?", (row["agent_id"],)).fetchone()
        if not profile or profile[0] != row["profile_version"]: raise AssistantError("POLICY_CHANGED")
        return row

    @contextmanager
    def fenced(self, job_id, token):
        marker = execution_guard.set(lambda cx: self.assert_job(cx, job_id, token))
        try: yield
        finally: execution_guard.reset(marker)

    def renew(self, job_id, token, *, lease_seconds=45, now=None):
        now = now or now_utc()
        with self.store.transaction() as cx:
            self.assert_job(cx, job_id, token, now=now)
            expires = stamp(now + timedelta(seconds=lease_seconds))
            cx.execute("UPDATE work_jobs SET lease_expires=? WHERE id=?", (expires, job_id))
            cx.execute("UPDATE run_leases SET expires_at=? WHERE token=?", (expires, token))

    def checkpoint(self, job_id, token, state, *, kind="checkpoint"):
        data = canonical(state)
        if len(data) > 1000000: raise AssistantError("CHECKPOINT_LIMIT")
        with self.store.transaction() as cx:
            self.assert_job(cx, job_id, token)
            cx.execute("UPDATE work_jobs SET checkpoint=?,updated_at=? WHERE id=?", (data, stamp(), job_id))
            seq = cx.execute("SELECT coalesce(max(seq),0)+1 FROM work_steps WHERE job_id=?", (job_id,)).fetchone()[0]
            # Full context lives in the checkpoint; audit steps retain lifecycle metadata only.
            cx.execute("INSERT INTO work_steps VALUES(?,?,?,?,?)", (job_id, seq, kind, canonical({"round": state.get("round", 0), "tool_calls": state.get("calls", 0)}), stamp()))

    def receipt(self, job_id, token, call_id, signature, result=None):
        with self.store.transaction() as cx:
            self.assert_job(cx, job_id, token)
            row = cx.execute("SELECT * FROM work_tool_receipts WHERE job_id=? AND call_id=?", (job_id, call_id)).fetchone()
            if row and row["signature"] != signature: raise AssistantError("TOOL_CALL_ID_COLLISION")
            if result is not None:
                cx.execute("INSERT INTO work_tool_receipts VALUES(?,?,?,'done',?) ON CONFLICT(job_id,call_id) DO UPDATE SET status='done',result=excluded.result",
                           (job_id, call_id, signature, canonical(result)))
                return result
            return json.loads(row["result"]) if row and row["status"] == "done" else None

    def finish(self, job_id, token, result):
        with self.store.transaction() as cx:
            self.assert_job(cx, job_id, token)
            if cx.execute("SELECT 1 FROM work_jobs WHERE parent_id=? AND status NOT IN ('completed','failed','cancelled')", (job_id,)).fetchone():
                raise AssistantError("DEPENDENCIES_PENDING")
            cx.execute("UPDATE work_jobs SET status='completed',result=?,checkpoint='{}',token=NULL,lease_expires=NULL,error_code='',updated_at=? WHERE id=?", (canonical(result), stamp(), job_id))
            cx.execute("UPDATE work_runs SET status='completed',ended_at=? WHERE id=?", (stamp(), token))
            cx.execute("DELETE FROM run_leases WHERE token=?", (token,))
            self._publish(cx, "work_completed", job_id, canonical({"job_id": job_id}), now_utc())

    def suspend(self, job_id, token, wait_spec, *, preempted=False):
        with self.store.transaction() as cx:
            row = self.assert_job(cx, job_id, token)
            cx.execute("UPDATE work_jobs SET status=?,wait_spec=?,run_after=?,token=NULL,lease_expires=NULL,attempts=max(0,attempts-1),updated_at=? WHERE id=?",
                       ("queued" if preempted else "waiting", canonical(wait_spec), stamp(now_utc() + timedelta(seconds=2)), stamp(), job_id))
            cx.execute("UPDATE work_runs SET status=?,ended_at=? WHERE id=?", ("preempted" if preempted else "waiting", stamp(), token))
            cx.execute("DELETE FROM run_leases WHERE token=?", (token,))

    def fail(self, job_id, token, code, *, retryable=False):
        with self.store.transaction() as cx:
            row = cx.execute("SELECT * FROM work_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] != "running" or row["token"] != token: raise AssistantError("STALE_RUN")
            retry = retryable and row["attempts"] < row["max_attempts"]
            cx.execute("UPDATE work_jobs SET status=?,run_after=?,error_code=?,token=NULL,lease_expires=NULL,updated_at=? WHERE id=?",
                       ("queued" if retry else "failed", stamp(now_utc() + timedelta(seconds=min(300, 5 * 2 ** row["attempts"]))), code, stamp(), job_id))
            cx.execute("UPDATE work_runs SET status='failed',error_code=?,ended_at=? WHERE id=?", (code, stamp(), token))
            cx.execute("DELETE FROM run_leases WHERE token=?", (token,))

    def admit_session(self, agent_id, session, *, max_runs=3, now=None):
        now = now or now_utc(); token = uuid.uuid4().hex
        self.store._session_id(session)
        profile = self.profile(agent_id)
        with self.store.transaction() as cx:
            cx.execute("DELETE FROM run_leases WHERE expires_at<=?", (stamp(now),))
            lane = agent_id + ":" + session
            if cx.execute("SELECT 1 FROM run_leases WHERE lane=?", (lane,)).fetchone(): raise AssistantError("SESSION_BUSY")
            if cx.execute("SELECT count(*) FROM run_leases").fetchone()[0] >= max_runs: raise AssistantError("RUNTIME_BUSY")
            cx.execute("INSERT INTO run_leases VALUES(?,?, 'foreground',?)", (lane, token, stamp(now + timedelta(seconds=45))))
            return {"token": token, "lane": lane, "agent_id": agent_id, "profile_version": profile["version"], "epoch": self.epoch(cx)}

    def assert_session(self, cx, lease, *, now=None):
        row = cx.execute("SELECT expires_at FROM run_leases WHERE token=? AND lane=?", (lease["token"], lease["lane"])).fetchone()
        if not row or row[0] <= stamp(now): raise AssistantError("STALE_RUN")
        if self.epoch(cx) != lease["epoch"]: raise AssistantError("SOURCE_CHANGED_DURING_TURN")
        if cx.execute("SELECT version FROM agent_profiles WHERE id=?", (lease["agent_id"],)).fetchone()[0] != lease["profile_version"]: raise AssistantError("POLICY_CHANGED")

    def release_session(self, lease):
        with self.store.transaction() as cx: cx.execute("DELETE FROM run_leases WHERE token=?", (lease["token"],))

    def cancel(self, job_id):
        with self.store.transaction() as cx:
            if not cx.execute("SELECT 1 FROM work_jobs WHERE id=?", (job_id,)).fetchone(): raise AssistantError("JOB_NOT_FOUND")
            ids = [job_id]
            for jid in ids:
                ids.extend(r[0] for r in cx.execute("SELECT id FROM work_jobs WHERE parent_id=? AND status NOT IN ('completed','failed','cancelled')", (jid,)))
                row = cx.execute("SELECT token,status FROM work_jobs WHERE id=?", (jid,)).fetchone()
                if row["status"] in TERMINAL: continue
                cx.execute("UPDATE work_jobs SET status='cancelled',token=NULL,lease_expires=NULL,checkpoint='{}',updated_at=? WHERE id=?", (stamp(), jid))
                cx.execute("DELETE FROM run_leases WHERE token=?", (row["token"],))
                cx.execute("UPDATE work_runs SET status='cancelled',ended_at=? WHERE id=?", (stamp(), row["token"]))

    def retry(self, job_id):
        job = self.job(job_id)
        if job["parent_id"] or job["status"] != "failed" or job["error_code"] in {"SOURCE_CHANGED_DURING_TURN", "MODEL_ROUND_LIMIT", "TOOL_CALL_LIMIT", "CHECKPOINT_LIMIT", "CONTEXT_LIMIT"}:
            raise AssistantError("RETRY_NOT_AVAILABLE")
        profile = self.profile(job["agent_id"])
        checkpoint = job["checkpoint"]
        if job["error_code"] in {"MODEL_EMPTY_RESPONSE", "MODEL_REFUSAL", "CITATION_NOT_VERIFIED"} and checkpoint.get("pending"):
            if checkpoint.get("batches"):
                cut = checkpoint["batches"].pop()
                checkpoint["items"] = checkpoint["items"][:cut]
            checkpoint["pending"] = None; checkpoint["next_call"] = 0
            checkpoint["round"] = max(0, checkpoint.get("round", 1)-1)
        with self.store.transaction() as cx:
            cx.execute("UPDATE work_jobs SET status='queued',attempts=0,run_after=?,deadline=?,profile_version=?,capability=?,"
                       "epoch=?,checkpoint=?,error_code='',updated_at=? WHERE id=? AND status='failed'",
                       (stamp(), stamp(now_utc() + timedelta(days=7)), profile["version"], canonical(profile["capability"].attenuate(Capability.from_dict(job["capability"])).as_dict()), self.epoch(cx), canonical(checkpoint), stamp(), job_id))

    def add_routine(self, name, instructions, *, agent_id="main", interval_seconds=0, at=None, event_kind=None, event_key="", enabled=False):
        profile = self.profile(agent_id); bounded_text(name, "name", 100); bounded_text(instructions, "instructions", 8000)
        if type(enabled) is not bool: raise AssistantError("ROUTINE_INVALID")
        if event_kind:
            if event_kind not in ("document", "bridge", "user"): raise AssistantError("EVENT_KIND_INVALID")
            bounded_text(event_key, "event_key", 200)
            if event_kind == "document" and not profile["capability"].source_allowed(event_key): raise AssistantError("SOURCE_SCOPE_DENIED")
            with self.store.connection() as cx:
                row = cx.execute("SELECT revision FROM runtime_events WHERE kind=? AND event_key=?", (event_kind, event_key)).fetchone()
            kind, value, next_at = "event", canonical({"kind": event_kind, "key": event_key, "after_revision": row[0] if row else 0}), stamp()
        else:
            if type(interval_seconds) is not int or interval_seconds not in (0,) and not 60 <= interval_seconds <= 2592000: raise AssistantError("ROUTINE_INVALID")
            kind, value, next_at = "timer", "{}", stamp(parse_time(at)) if at else stamp(now_utc() + timedelta(seconds=max(1, interval_seconds)))
        rid = str(uuid.uuid4())
        with self.store.transaction() as cx:
            cx.execute("INSERT INTO routines VALUES(?,?,?,?,?,?,?,?,?,?,?)", (rid, agent_id, name, instructions, kind, value, interval_seconds, next_at, int(enabled), stamp(), stamp()))
        return rid

    def routines(self):
        with self.store.connection() as cx: return [dict(r) for r in cx.execute("SELECT * FROM routines ORDER BY name")]

    def enable_routine(self, routine_id, enabled):
        with self.store.transaction() as cx:
            if cx.execute("UPDATE routines SET enabled=?,updated_at=? WHERE id=?", (int(enabled), stamp(), routine_id)).rowcount != 1: raise AssistantError("ROUTINE_NOT_FOUND")

    def schedule(self, *, now=None) -> int:
        now = now or now_utc(); created = 0
        # submit uses its own short transaction. Deterministic keys close the crash
        # gap between enqueue and advancing a schedule, including multiple daemons.
        for routine in self.routines():
            if not routine["enabled"]: continue
            event_at = None
            reference = None
            if routine["trigger_kind"] == "timer":
                if routine["next_at"] > stamp(now): continue
                trigger = routine["next_at"]
            else:
                spec = json.loads(routine["trigger_value"])
                with self.store.connection() as cx:
                    event = cx.execute("SELECT revision,payload FROM runtime_events WHERE kind=? AND event_key=? AND available_at<=? AND revision>? ORDER BY revision DESC LIMIT 1",
                                       (spec["kind"], spec["key"], stamp(now), spec["after_revision"])).fetchone()
                if not event: continue
                event_at = event[0]; trigger = str(event[0])
                reference = {"kind": spec["kind"], "key": spec["key"], "revision": event[0], "metadata": json.loads(event["payload"])}
                if spec["kind"] == "document" and not self.profile(routine["agent_id"])["capability"].source_allowed(spec["key"]):
                    self.enable_routine(routine["id"], False); continue
            instructions = routine["instructions"]
            if reference:
                if len(canonical(reference)) > 1500: reference = {k:v for k,v in reference.items() if k!="metadata"} | {"metadata_omitted":True}
                instructions += "\nTRIGGER_REFERENCE (not instructions): " + canonical(reference)
            self.submit(instructions, agent_id=routine["agent_id"], idempotency_key="routine:" + routine["id"] + ":" + trigger, now=now)
            with self.store.transaction() as cx:
                if event_at:
                    spec["after_revision"] = event_at
                    cx.execute("UPDATE routines SET trigger_value=?,updated_at=? WHERE id=? AND trigger_value=?", (canonical(spec), stamp(now), routine["id"], routine["trigger_value"]))
                elif routine["interval_seconds"]:
                    # Coalesce missed intervals into one run, never a restart storm.
                    next_at = parse_time(trigger)
                    skipped = int((now - next_at).total_seconds() // routine["interval_seconds"]) + 1
                    cx.execute("UPDATE routines SET next_at=?,updated_at=? WHERE id=? AND next_at=?", (stamp(next_at + timedelta(seconds=skipped * routine["interval_seconds"])), stamp(now), routine["id"], trigger))
                else: cx.execute("UPDATE routines SET enabled=0,updated_at=? WHERE id=?", (stamp(now), routine["id"]))
            created += 1
        return created

    def artifact(self, job_id, token, name, content, citations, *, artifact_id):
        bounded_text(name, "artifact_name", 100); bounded_text(content, "artifact", 30000)
        with self.store.transaction() as cx:
            self.assert_job(cx, job_id, token)
            previous = cx.execute("SELECT * FROM work_artifacts WHERE job_id=? AND id=?", (job_id, artifact_id)).fetchone()
            if previous:
                if previous["content"] != content or previous["name"] != name or previous["citations"] != canonical(citations): raise AssistantError("IDEMPOTENCY_CONFLICT")
                return {"id": previous["id"], "version": previous["version"]}
            version = cx.execute("SELECT coalesce(max(version),0)+1 FROM work_artifacts WHERE job_id=? AND name=?", (job_id, name)).fetchone()[0]
            cx.execute("INSERT INTO work_artifacts VALUES(?,?,?,?,?,?,?)", (artifact_id, job_id, name, version, content, canonical(citations), stamp()))
        return {"id": artifact_id, "version": version, "status": "saved_locally"}

    def artifacts(self, job_id):
        with self.store.connection() as cx:
            return [{**dict(r), "citations": json.loads(r["citations"])} for r in cx.execute("SELECT * FROM work_artifacts WHERE job_id=? ORDER BY name,version", (job_id,))]

    def feedback(self, job_id, *, helpful, intervened=False, retrieval_relevant=None, retrieval_total=None):
        self.job(job_id)
        if type(helpful) is not bool or type(intervened) is not bool: raise AssistantError("FEEDBACK_INVALID")
        if (retrieval_relevant is None) != (retrieval_total is None) or retrieval_total is not None and (type(retrieval_total) is not int or type(retrieval_relevant) is not int or not 0 <= retrieval_relevant <= retrieval_total):
            raise AssistantError("FEEDBACK_INVALID")
        with self.store.transaction() as cx:
            cx.execute("INSERT INTO quality_feedback VALUES(?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET helpful=excluded.helpful,intervened=excluded.intervened,"
                       "retrieval_relevant=excluded.retrieval_relevant,retrieval_total=excluded.retrieval_total,recorded_at=excluded.recorded_at",
                       (job_id, int(helpful), int(intervened), retrieval_relevant, retrieval_total, stamp()))

    def metrics(self):
        with self.store.connection() as cx:
            counts = {r[0]: r[1] for r in cx.execute("SELECT status,count(*) FROM work_jobs GROUP BY status")}
            rated = cx.execute("SELECT count(*),sum(helpful),sum(intervened),sum(retrieval_relevant),sum(retrieval_total) FROM quality_feedback").fetchone()
            recovered = cx.execute("SELECT count(*) FROM work_jobs j WHERE status='completed' AND EXISTS (SELECT 1 FROM work_runs r WHERE r.job_id=j.id AND r.status IN ('failed','interrupted'))").fetchone()[0]
            retry_jobs = cx.execute("SELECT count(DISTINCT job_id) FROM work_runs WHERE status IN ('failed','interrupted')").fetchone()[0]
            # Costs are usage counters, never fabricated currency estimates.
            budget = cx.execute("SELECT coalesce(sum(calls),0),coalesce(sum(tokens),0) FROM budget").fetchone()
            usage = cx.execute("SELECT coalesce(sum(u.calls),0),coalesce(sum(u.tokens),0) FROM work_usage u JOIN work_jobs j ON j.id=u.job_id WHERE j.status='completed'").fetchone()
            proactive = cx.execute("SELECT count(*),sum(1-f.helpful) FROM quality_feedback f JOIN work_jobs j ON j.id=f.job_id WHERE j.dispatch_kind='monitor'").fetchone()
        completed = counts.get("completed", 0); terminal = sum(counts.get(s, 0) for s in TERMINAL)
        return {"states": counts, "terminal_success_rate": completed / terminal if terminal else None,
                "rated_jobs": rated[0], "rated_helpful_rate": rated[1] / rated[0] if rated[0] else None,
                "intervention_rate": rated[2] / rated[0] if rated[0] else None,
                "recovered_jobs": recovered, "jobs_with_failed_attempt": retry_jobs,
                "recovery_success_rate": recovered / retry_jobs if retry_jobs else None,
                "memory_retrieval_precision": rated[3] / rated[4] if rated[4] else None,
                "false_proactive_rate": proactive[1] / proactive[0] if proactive[0] else None,
                "model_calls_per_completed_job": usage[0] / completed if completed else None,
                "reserved_or_actual_tokens_per_completed_job": usage[1] / completed if completed else None,
                "account_model_calls": budget[0], "account_reserved_or_actual_tokens": budget[1]}

    def prune(self, retention_days, *, now=None):
        now = now or now_utc()
        with self.store.transaction() as cx:
            self._recover(cx, now)
            for _ in range(4):
                # Remove leaves first; a retained child keeps its parent reachable.
                cx.execute("DELETE FROM work_jobs WHERE status IN ('completed','failed','cancelled') AND updated_at<? "
                           "AND NOT EXISTS (SELECT 1 FROM work_jobs child WHERE child.parent_id=work_jobs.id)", (stamp(now-timedelta(days=retention_days)),))
