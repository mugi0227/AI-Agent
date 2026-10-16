"""Durable quiet-first monitoring. No inference without an actionable candidate."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .agent import Agent
from .common import AssistantError, JST, canonical, digest, now_utc, parse_time, safe_code, stamp
from .config import Settings, minute_of_day
from .store import Store
from .tools import active_hours


class Scheduler:
    def __init__(self, store: Store, cfg: Settings, agent: Agent):
        self.store, self.cfg, self.agent = store, cfg, agent

    def candidates(self, now: datetime) -> list[dict]:
        docs = self.store.documents(limit=200)
        local, candidates = now.astimezone(JST), []
        for doc in docs:
            metadata = doc["metadata"]
            if doc["kind"] == "calendar" and metadata.get("start_at"):
                start = parse_time(metadata["start_at"])
                if now <= start <= now + timedelta(minutes=self.cfg.meeting_lead_minutes):
                    candidates.append({"key": "meeting:" + doc["id"] + ":" + metadata["start_at"], "document_id": doc["id"], "kind": "meeting", "instruction": "この会議の前回記録と関連資料を検索し、未確認事項と準備候補を整理してください。"})
            elif doc["kind"] == "transcript" and parse_time(doc["observed_at"]) >= now - timedelta(days=2):
                candidates.append({"key": "transcript:" + doc["id"] + ":" + doc["modified_at"], "document_id": doc["id"], "kind": "transcript", "instruction": "この会議記録を読んで、原文で確認できる行動・決定だけを候補にしてください。責任者不明の発言をユーザーの義務にしないでください。"})
        for task in self.store.tasks():
            if task["due_at"] and parse_time(task["due_at"]) <= now + timedelta(days=1):
                candidates.append({"key": "task:" + task["id"] + ":" + local.date().isoformat(), "document_id": task["document_id"], "kind": "deadline", "instruction": "人が承認したタスクの期限が近い、または過ぎています。根拠を確認して次の一手を提示してください。"})
        for doc in docs:
            if doc["kind"] not in ("mail", "teams_message", "capture", "search_result") or parse_time(doc["observed_at"]) < now - timedelta(hours=12):
                continue
            for project in self.store.projects():
                if any(k and k.casefold() in doc["title"].casefold() for k in project["keywords"]):
                    candidates.append({"key": "change:" + doc["id"] + ":" + doc["modified_at"], "document_id": doc["id"], "kind": "project_change", "instruction": "担当案件に関連する新しい情報です。既知の決定からの重要な変化や依頼があるか原文を確認し、重要な場合だけ提案してください。"})
                    break
        minute = local.hour * 60 + local.minute
        for clock in self.cfg.briefing_times:
            if docs and 0 <= minute - minute_of_day(clock) < 60:
                candidates.append({"key": "briefing:" + local.date().isoformat() + ":" + clock, "document_id": docs[0]["id"], "kind": "briefing", "instruction": "現在の案件、今日の予定、承認済みの期限と重要な変化を確認してください。通知に値するものがなければNO_REPLY。"})
        priority = {"meeting": 0, "deadline": 1, "briefing": 2, "transcript": 3, "project_change": 4}
        return sorted(candidates, key=lambda c: priority[c["kind"]])

    def _claim(self, key: str, now: datetime) -> bool:
        with self.store.transaction() as cx:
            row = cx.execute("SELECT value FROM state WHERE key=?", ("job:" + key,)).fetchone()
            state = json.loads(row[0]) if row else {}
            if state.get("status") == "done" or state.get("attempts", 0) >= 3:
                return False
            if state.get("retry_at") and parse_time(state["retry_at"]) > now:
                return False
            new = {"status": "running", "attempts": state.get("attempts", 0) + 1,
                   "retry_at": stamp(now + timedelta(minutes=5))}
            cx.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("job:" + key, canonical(new)))
        return True

    def tick(self, now: datetime | None = None, cancel=None) -> dict:
        now = now or now_utc()
        if not self.cfg.heartbeat_enabled or not self.cfg.bedrock.enabled:
            return {"status": "skipped", "reason": "disabled"}
        if not active_hours(self.cfg, now) or self.store.get_state("focus", False) or (cancel and cancel.is_set()):
            return {"status": "skipped", "reason": "quiet_or_busy"}
        last = self.store.get_state("monitor_last_attempt")
        if last and parse_time(last) > now - timedelta(minutes=self.cfg.heartbeat_interval_minutes):
            return {"status": "skipped", "reason": "cadence"}
        for candidate in self.candidates(now):
            key = candidate["key"]
            if not self._claim(key, now):
                continue
            self.store.set_state("monitor_last_attempt", stamp(now))
            try:
                prompt = candidate["instruction"] + "\n対象文書ID: " + candidate["document_id"] + "\n変更が重要な場合のみnotifyを使ってください。"
                self.agent.turn(prompt, session="monitor", mode="monitor", event_key=key, cancel=cancel)
                self.store.set_state("job:" + key, {"status": "done", "completed_at": stamp()})
                return {"status": "completed", "kind": candidate["kind"]}
            except AssistantError as error:
                state = self.store.get_state("job:" + key, {})
                # A foreground preemption should not exhaust the three real failure attempts.
                if error.code == "TURN_PREEMPTED":
                    state["attempts"] = max(0, state.get("attempts", 1) - 1)
                state.update({"status": "retry", "error_code": error.code, "retry_at": stamp(now + timedelta(minutes=5))})
                self.store.set_state("job:" + key, state)
                self.store.audit("monitor", error.code)
                return {"status": "failed", "error_code": error.code}
        return {"status": "skipped", "reason": "no_new_work"}
