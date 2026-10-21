"""Durable quiet-first monitoring. No inference without an actionable candidate."""
from __future__ import annotations

from datetime import datetime, timedelta

from .agent import Agent
from .common import AssistantError, JST, now_utc, parse_time, stamp
from .config import Settings, minute_of_day
from .store import Store
from .tools import active_hours
from .opportunities import assess


class Scheduler:
    def __init__(self, store: Store, cfg: Settings, agent: Agent):
        self.store, self.cfg, self.agent = store, cfg, agent

    def candidates(self, now: datetime, capability=None) -> list[dict]:
        docs = [d for d in self.store.documents(limit=200) if capability is None or capability.source_allowed(d["id"])]
        projects = [p for p in self.store.projects() if capability is None or capability.project_allowed(p["id"])]
        local, candidates = now.astimezone(JST), []
        for doc in docs:
            metadata = doc["metadata"]
            if doc["kind"] == "calendar" and metadata.get("start_at"):
                start = parse_time(metadata["start_at"])
                if now <= start <= now + timedelta(minutes=self.cfg.meeting_lead_minutes):
                    candidates.append({"key": "meeting:" + doc["id"] + ":" + metadata["start_at"], "document_id": doc["id"], "kind": "meeting", "instruction": "この会議の前回記録と関連資料を検索し、未確認事項と準備候補を整理してください。"})
            elif doc["kind"] == "transcript" and parse_time(doc["observed_at"]) >= now - timedelta(days=2):
                candidates.append({"key": "transcript:" + doc["id"] + ":" + doc["content_hash"], "document_id": doc["id"], "kind": "transcript", "instruction": "この会議記録を読んで、原文で確認できる行動・決定だけを候補にしてください。責任者不明の発言をユーザーの義務にしないでください。"})
        for task in self.store.tasks():
            if capability is not None and (not capability.source_allowed(task["document_id"]) or not capability.project_allowed(task["project_id"])): continue
            if task["due_at"] and parse_time(task["due_at"]) <= now + timedelta(days=1):
                candidates.append({"key": "task:" + task["id"] + ":" + local.date().isoformat(), "document_id": task["document_id"], "kind": "deadline", "instruction": "人が承認したタスクの期限が近い、または過ぎています。根拠を確認して次の一手を提示してください。"})
        for doc in docs:
            if doc["kind"] not in ("mail", "teams_message", "capture", "search_result") or parse_time(doc["observed_at"]) < now - timedelta(hours=12):
                continue
            for project in projects:
                if any(k and k.casefold() in doc["title"].casefold() for k in project["keywords"]):
                    candidates.append({"key": "change:" + doc["id"] + ":" + doc["content_hash"] + ":" + project["id"], "document_id": doc["id"], "kind": "project_change", "instruction": "担当案件に関連する新しい情報です。既知の決定からの重要な変化や依頼があるか原文を確認し、重要な場合だけ提案してください。"})
                    break
        minute = local.hour * 60 + local.minute
        for clock in self.cfg.briefing_times:
            if docs and 0 <= minute - minute_of_day(clock) < 60:
                candidates.append({"key": "briefing:" + local.date().isoformat() + ":" + clock, "document_id": docs[0]["id"], "kind": "briefing", "instruction": "現在の案件、今日の予定、承認済みの期限と重要な変化を確認してください。通知に値するものがなければNO_REPLY。"})
        priority = {"meeting": 0, "deadline": 1, "briefing": 2, "transcript": 3, "project_change": 4}
        return sorted(candidates, key=lambda c: priority[c["kind"]])

    def tick(self, now: datetime | None = None, cancel=None) -> dict:
        from .state import DurableState
        now = now or now_utc()
        if not self.cfg.heartbeat_enabled or not self.cfg.bedrock.enabled:
            return {"status": "skipped", "reason": "disabled"}
        if not active_hours(self.cfg, now) or self.store.get_state("focus", False) or (cancel and cancel.is_set()):
            return {"status": "skipped", "reason": "quiet_or_busy"}
        last = self.store.get_state("monitor_last_attempt")
        if last and parse_time(last) > now - timedelta(minutes=self.cfg.heartbeat_interval_minutes):
            return {"status": "skipped", "reason": "cadence"}
        queue = DurableState(self.store)
        capability = queue.profile("monitor")["capability"]
        ranked = [(c, assess(self.store, self.cfg, c, now)) for c in self.candidates(now, capability)]
        for candidate, assessment in sorted(ranked, key=lambda item: -item[1]["score"]):
            with self.store.connection() as cx:
                if cx.execute("SELECT 1 FROM work_jobs WHERE idempotency_key=?", ("monitor:" + candidate["key"],)).fetchone(): continue
            try:
                self.agent.ingestor.validate_live_source(candidate["document_id"])
            except AssistantError:
                continue
            # This is an explainable opportunity filter, not invented user intent.
            # Sources only identify candidates; fixed host rules grant authority.
            self.store.set_state("opportunity:last", {"event_key": candidate["key"], "document_id": candidate["document_id"], "assessment": assessment, "at": stamp(now)})
            if assessment["decision"] != "investigate": continue
            importance = round(assessment["score"] * 100)
            prompt = candidate["instruction"] + "\n対象文書ID: " + candidate["document_id"]
            prompt += "\n変更が重要な場合のみnotifyを使ってください。不要ならNO_REPLY。外部操作は行いません。"
            jid = queue.submit(prompt, agent_id="monitor", document_ids=[candidate["document_id"]],
                idempotency_key="monitor:" + candidate["key"], priority=100-importance,
                dispatch_kind="monitor", event_key=candidate["key"], now=now)
            job = queue.job(jid)
            if job["status"] not in {"queued", "running", "waiting"}: continue
            self.store.set_state("monitor_last_attempt", stamp(now))
            return {"status": "queued", "kind": candidate["kind"], "job_id": jid,
                    "priority": importance, "reason": candidate["kind"], "document_id": candidate["document_id"]}
        return {"status": "skipped", "reason": "no_new_work"}
