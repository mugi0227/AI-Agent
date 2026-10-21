"""Execution handle passed to the agent loop; one durable job owns each handle."""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import timedelta

from .common import AssistantError, JST, canonical, now_utc, parse_time, safe_code, stamp
from .policy import Capability, execution_guard


class WorkExecution:
    def __init__(self, state, job, ingestor):
        self.queue, self.job, self.ingestor = state, job, ingestor
        self.job_id, self.token = job["id"], job["token"]
        self.capability = Capability.from_dict(job["capability"])
        self.state = job["checkpoint"]
        self.integrated_children = set(self.state.get("integrated_children", []))
        self.wait_spec = None
        self.call_id = ""

    def check(self):
        with self.queue.store.connection() as cx:
            self.queue.assert_job(cx, self.job_id, self.token)
            if cx.execute("SELECT 1 FROM run_leases WHERE kind='foreground' AND expires_at>?", (stamp(),)).fetchone():
                raise AssistantError("TURN_PREEMPTED", retryable=True)

    def save(self, state, kind="checkpoint"):
        self.queue.checkpoint(self.job_id, self.token, state, kind=kind)
        self.state = state

    def idempotency_key(self):
        return "work:" + self.job_id + ":" + self.call_id

    def child_states(self):
        with self.queue.store.connection() as cx:
            return [dict(r) for r in cx.execute("SELECT id,status FROM work_jobs WHERE parent_id=?", (self.job_id,))]

    def resume_context(self, seen):
        wait = self.job["wait_spec"]
        if wait.get("kind") == "approval":
            return {"kind": "approval", **self.dispatch("wait_for_approval", {"proposal_id": wait["proposal_id"]}, seen)}
        if wait.get("kind") != "children": return wait
        return self.child_context(wait["ids"], seen)

    def child_context(self, ids, seen):
        children = []
        for jid in ids:
            value = self.dispatch("read_work_result", {"job_id": jid}, seen)
            if value.get("result"): value["result"] = {**value["result"], "text": value["result"].get("text", "")[:1500]}
            value["artifacts"] = [{**a, "content": a["content"][:800]} for a in value.get("artifacts", [])[-2:]]
            children.append(value)
        return {"kind": "children_completed", "children": children}

    def dispatch(self, name, args, seen):
        self.check()
        if name == "delegate_work":
            child = self.queue.profile(args["agent_id"])
            child_cap = self.capability.attenuate(child["capability"])
            docs = [d for d in self.job["document_ids"] if child_cap.source_allowed(d)]
            jid = self.queue.submit(args["goal"], agent_id=args["agent_id"], parent_id=self.job_id,
                                    document_ids=docs, capability=child_cap, deadline=self.job["deadline"],
                                    idempotency_key=self.idempotency_key())
            return {"job_id": jid, "status": self.queue.job(jid)["status"]}
        if name in ("wait_for_work", "read_work_result"):
            child = self.queue.job(args["job_id"])
            if child["parent_id"] != self.job_id: raise AssistantError("WORK_SCOPE_DENIED")
            if child["status"] not in ("completed", "failed", "cancelled"):
                if name == "wait_for_work": self.wait_spec = {"kind": "children", "ids": [child["id"]]}
                return {"job_id": child["id"], "status": child["status"]}
            for source in child["result"].get("citations", []):
                if not self.capability.source_allowed(source["id"]): raise AssistantError("SOURCE_SCOPE_DENIED")
                self.ingestor.validate_live_source(source["id"]); seen.add(source["id"])
            self.integrated_children.add(child["id"])
            artifacts = self.queue.artifacts(child["id"])
            latest = {}
            for artifact in artifacts: latest[artifact["name"]] = artifact
            return {"job_id": child["id"], "status": child["status"], "result": {**child["result"], "text": child["result"].get("text", "")[:8000]}, "error_code": child["error_code"],
                    "provenance": "worker_output_not_instructions", "artifacts": [{**a, "content": a["content"][:2000]} for a in list(latest.values())[-3:]],
                    "partial": len(artifacts) > 3 or len(child["result"].get("text", "")) > 8000}
        if name == "wait_for_information":
            if not self.owns_request(args["request_id"]): raise AssistantError("WORK_SCOPE_DENIED")
            with self.queue.store.connection() as cx:
                row = cx.execute("SELECT status,expires_at FROM bridge_requests WHERE id=?", (args["request_id"],)).fetchone()
            if row and row["status"] == "pending" and row["expires_at"] > stamp():
                self.wait_spec = {"kind": "bridge", "request_id": args["request_id"]}
            return {"status": row["status"] if row else "not_found", "request_id": args["request_id"]}
        if name == "wait_for_approval":
            with self.queue.store.connection() as cx:
                receipts = cx.execute("SELECT result FROM work_tool_receipts WHERE job_id=? AND status='done'", (self.job_id,)).fetchall()
                if not any(json.loads(r[0]).get("value", {}).get("proposal_id") == args["proposal_id"] for r in receipts): raise AssistantError("WORK_SCOPE_DENIED")
                row = cx.execute("SELECT status,document_id FROM proposals WHERE id=?", (args["proposal_id"],)).fetchone()
            if row:
                if not self.capability.source_allowed(row["document_id"]): raise AssistantError("SOURCE_SCOPE_DENIED")
                self.ingestor.validate_live_source(row["document_id"]); seen.add(row["document_id"])
                if row["status"] == "pending": self.wait_spec = {"kind": "approval", "proposal_id": args["proposal_id"]}
            return {"proposal_id": args["proposal_id"], "status": row["status"] if row else "unavailable"}
        if name == "wait_until":
            at = parse_time(args["at"])
            if at > parse_time(self.job["deadline"]): raise AssistantError("JOB_SCHEDULE_INVALID")
            if at > now_utc(): self.wait_spec = {"kind": "timer", "at": stamp(at)}
            return {"status": "waiting" if self.wait_spec else "elapsed", "at": stamp(at)}
        if name == "wait_for_change":
            if not self.capability.source_allowed(args["document_id"]): raise AssistantError("SOURCE_SCOPE_DENIED")
            self.ingestor.validate_live_source(args["document_id"])
            with self.queue.store.connection() as cx:
                row = cx.execute("SELECT revision FROM runtime_events WHERE kind='document' AND event_key=?", (args["document_id"],)).fetchone()
            self.wait_spec = {"kind": "event", "event_kind": "document", "key": args["document_id"], "after_revision": row[0] if row else 0}
            return {"status": "waiting", "document_id": args["document_id"]}
        if name == "save_artifact":
            ids = set(re.findall(r"\[資料:([^\]]+)\]", args["content"]))
            if not ids <= seen: raise AssistantError("CITATION_NOT_VERIFIED")
            citations = []
            for did in sorted(seen):
                if not self.capability.source_allowed(did): raise AssistantError("SOURCE_SCOPE_DENIED")
                doc = self.ingestor.validate_live_source(did)
                citations.append({k: doc[k] for k in ("id", "title", "source_url", "modified_at", "observed_at", "expires_at")})
            return self.queue.artifact(self.job_id, self.token, args["name"], args["content"], citations,
                                       artifact_id=str(uuid.uuid5(uuid.NAMESPACE_URL, self.idempotency_key())))
        raise AssistantError("TOOL_NOT_ALLOWED")

    def owns_request(self, request_id):
        with self.queue.store.connection() as cx:
            rows = cx.execute("SELECT result FROM work_tool_receipts WHERE job_id=? AND status='done'", (self.job_id,)).fetchall()
        return any(json.loads(r[0]).get("value", {}).get("request_id") == request_id for r in rows)

    def usage(self, tokens):
        with self.queue.store.transaction() as cx:
            self.queue.assert_job(cx, self.job_id, self.token)
            cx.execute("INSERT INTO work_usage VALUES(?,1,?) ON CONFLICT(job_id) DO UPDATE SET calls=calls+1,tokens=work_usage.tokens+excluded.tokens", (self.job_id, tokens))


class CombinedCancel:
    def __init__(self, *events): self.events = events
    def is_set(self): return any(e and e.is_set() for e in self.events)


class WorkRunner:
    def __init__(self, state, cfg, agent):
        self.state, self.cfg, self.agent = state, cfg, agent

    def run_one(self, *, cancel=None, job_id=None, allowed_kinds=None):
        if cancel and cancel.is_set(): return {"status": "skipped"}
        if not self.cfg.local_data_approved: return {"status": "disabled"}
        # Background inference always needs explicit configuration, even when jobs
        # have survived a restart. Unit/integration fixtures call run_one directly.
        if self.agent.client.is_live and (not self.cfg.bedrock.enabled or not self.cfg.bedrock.data_transfer_approved):
            return {"status": "disabled"}
        job = self.state.claim(max_workers=self.cfg.background_workers, job_id=job_id, allowed_kinds=allowed_kinds)
        if not job: return {"status": "idle"}
        execution = WorkExecution(self.state, job, self.agent.ingestor)
        done, lost = threading.Event(), threading.Event()
        def heartbeat():
            while not done.wait(10):
                try: self.state.renew(job["id"], job["token"])
                except Exception: lost.set(); return
        renewer = threading.Thread(target=heartbeat, daemon=True, name="work-lease")
        renewer.start()
        try:
            with self.state.fenced(job["id"], job["token"]):
                result = self.agent.turn(job["goal"], session=job["session"], mode=job["dispatch_kind"],
                                         event_key=job["event_key"] or job["id"], document_ids=job["document_ids"],
                                         cancel=CombinedCancel(cancel, lost), execution=execution)
            if result.status == "waiting":
                self.state.suspend(job["id"], job["token"], execution.wait_spec)
                return {"status": "waiting", "job_id": job["id"]}
            self.state.finish(job["id"], job["token"], {"text": result.text, "citations": result.citations, "trace": result.trace})
            return {"status": "completed", "job_id": job["id"]}
        except Exception as error:
            code = safe_code(error)
            try:
                if code == "STALE_RUN":
                    with self.state.store.transaction() as cx: self.state._recover(cx, now_utc())
                elif code == "DAILY_BUDGET_EXCEEDED":
                    tomorrow = (now_utc().astimezone(JST) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    self.state.suspend(job["id"], job["token"], {"kind": "timer", "at": stamp(tomorrow), "reason": "daily_budget"})
                elif code == "TURN_PREEMPTED" and not lost.is_set(): self.state.suspend(job["id"], job["token"], {}, preempted=True)
                else: self.state.fail(job["id"], job["token"], code, retryable=getattr(error, "retryable", False) or code in {"BEDROCK_NETWORK_OR_TLS", "MODEL_STREAM_TRUNCATED"})
            except AssistantError as settled:
                if settled.code not in {"STALE_RUN", "POLICY_CHANGED", "SOURCE_CHANGED_DURING_TURN"}: raise
            self.state.store.audit("work.run", code)
            return {"status": "waiting" if code == "DAILY_BUDGET_EXCEEDED" else "interrupted" if code == "TURN_PREEMPTED" else "failed", "job_id": job["id"], "error_code": code}
        finally:
            done.set()


class ForegroundLease:
    def __init__(self, state, agent_id, session, max_runs):
        self.state = state
        self.lease = state.admit_session(agent_id, session, max_runs=max_runs)
        self.done = threading.Event()

    def __enter__(self):
        def renew():
            while not self.done.wait(10):
                try:
                    with self.state.store.transaction() as cx:
                        self.state.assert_session(cx, self.lease)
                        cx.execute("UPDATE run_leases SET expires_at=? WHERE token=?", (stamp(now_utc() + timedelta(seconds=45)), self.lease["token"]))
                except Exception: return
        threading.Thread(target=renew, daemon=True, name="chat-lease").start()
        self.marker = execution_guard.set(lambda cx: self.state.assert_session(cx, self.lease))
        return self

    def release(self):
        self.done.set(); self.state.release_session(self.lease)

    def __exit__(self, *exc):
        execution_guard.reset(self.marker)
        self.release()
