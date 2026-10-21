"""Durability and isolation acceptance tests, including a real process crash."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from test_core import Fixture, call, message
from work_assistant.agent import Agent
from work_assistant.bedrock import ScriptedClient
from work_assistant.common import AssistantError, canonical, now_utc, stamp
from work_assistant.execution import WorkRunner, WorkExecution
from work_assistant.memory import MemoryEngine, cosine
from work_assistant.policy import BASE_TOOLS, READ_TOOLS, Capability
from work_assistant.state import DurableState
from work_assistant.store import Store
from work_assistant.tools import Tools
from work_assistant.scheduler import Scheduler


class DurableTests(Fixture):
    def setUp(self):
        super().setUp(); self.queue = DurableState(self.store)

    def runner(self, responses):
        agent, client = self.agent(responses)
        return WorkRunner(self.queue, self.cfg, agent), client

    def test_queue_survives_reopen_and_deduplicates_ingress(self):
        jid = self.queue.submit("会議の準備", idempotency_key="user-request-1")
        other = DurableState(Store(self.store.path))
        self.assertEqual(other.submit("会議の準備", idempotency_key="user-request-1"), jid)
        self.assertCode("IDEMPOTENCY_CONFLICT", other.submit, "別の依頼", idempotency_key="user-request-1")
        self.assertEqual(other.job(jid)["goal"], "会議の準備")

    def test_two_independent_workers_cannot_claim_the_same_job(self):
        jid = self.queue.submit("確認")
        barrier = threading.Barrier(2)
        def claim(_):
            queue = DurableState(Store(self.store.path)); barrier.wait(); return queue.claim()
        with ThreadPoolExecutor(2) as pool: results = list(pool.map(claim, range(2)))
        self.assertEqual([r["id"] for r in results if r], [jid])

    def test_lease_expiry_recovers_checkpoint_and_fences_old_tool_writes(self):
        jid = self.queue.submit("確認")
        first = self.queue.claim(); self.queue.checkpoint(jid, first["token"], {"round": 1, "note": "中間状態"})
        second = self.queue.claim(now=now_utc() + timedelta(seconds=46))
        self.assertEqual(second["checkpoint"]["note"], "中間状態")
        self.assertNotEqual(second["token"], first["token"])
        with self.queue.fenced(jid, first["token"]):
            self.assertCode("STALE_RUN", self.queue.checkpoint, jid, first["token"], {"bad": True})
        self.assertCode("STALE_RUN", self.queue.finish, jid, first["token"], {"text": "古い結果"})
        self.queue.finish(jid, second["token"], {"text": "新しい結果"})
        self.assertEqual(self.queue.job(jid)["result"]["text"], "新しい結果")

    def test_execution_guard_blocks_proposal_inside_side_effect_transaction(self):
        raw, did = self.add(); jid = self.queue.submit("確認"); first = self.queue.claim()
        self.queue.cancel(jid)
        with self.queue.fenced(jid, first["token"]):
            self.assertCode("STALE_RUN", self.store.propose, kind="action", title="確認", document_id=did,
                            quote=raw["content"], confidence=.9)
        self.assertFalse(self.store.proposals())

    def test_foreground_lane_and_global_admission_are_persistent(self):
        lease = self.queue.admit_session("main", "same", max_runs=1)
        other = DurableState(Store(self.store.path))
        self.assertCode("SESSION_BUSY", other.admit_session, "main", "same", max_runs=3)
        self.assertCode("RUNTIME_BUSY", other.admit_session, "main", "other", max_runs=1)
        self.queue.release_session(lease)
        newer = other.admit_session("main", "same", max_runs=1)
        with self.store.connection() as cx: self.assertCode("STALE_RUN", other.assert_session, cx, lease)
        other.release_session(newer)

    def test_reopening_runtime_does_not_interrupt_another_live_session(self):
        lease = self.queue.admit_session("main", "active")
        mid = self.store.remember_turn("active", "user", "処理中", status="running")
        Store(self.store.path).recover_interrupted()
        self.assertEqual(self.store.messages("active")[0]["status"], "running")
        self.queue.release_session(lease); self.store.recover_interrupted()
        self.assertEqual(self.store.messages("active")[0]["status"], "interrupted")

    def test_global_worker_limit_and_profile_revocation(self):
        a = self.queue.submit("一つ目"); b = self.queue.submit("二つ目")
        first = self.queue.claim(max_workers=1); self.assertEqual(first["id"], a)
        self.assertIsNone(self.queue.claim(max_workers=1))
        self.queue.set_profile("main", "変更", tools=READ_TOOLS)
        self.assertCode("STALE_RUN", self.queue.checkpoint, a, first["token"], {})
        self.assertEqual(self.queue.job(a)["error_code"], "POLICY_CHANGED")
        self.assertIsNone(self.queue.claim(max_workers=1))
        self.assertEqual(self.queue.job(b)["error_code"], "POLICY_CHANGED")

    def test_waiting_timer_uses_no_model_calls_until_due_and_restores_loop(self):
        at = now_utc() + timedelta(seconds=30)
        runner, client = self.runner([call("wait_until", {"at": stamp(at)}), message("指定時刻後に確認しました")])
        jid = self.queue.submit("指定時刻に確認")
        self.assertEqual(runner.run_one()["status"], "waiting")
        for _ in range(3): self.assertEqual(runner.run_one()["status"], "idle")
        self.assertEqual(len(client.calls), 1)
        with patch("work_assistant.state.now_utc", return_value=at + timedelta(seconds=1)):
            self.assertEqual(runner.run_one()["status"], "completed")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.queue.job(jid)["result"]["text"], "指定時刻後に確認しました")
        self.assertIn("RESUME_EVENT_REFERENCE", canonical(client.calls[-1]["input"]))

    def test_parent_delegates_and_waits_while_child_has_isolated_capabilities(self):
        self.queue.set_profile("research", "資料調査", tools=READ_TOOLS, document_ids=[], project_ids=[])
        runner, client = self.runner([call("delegate_work", {"agent_id": "research", "goal": "論点を調査"})])
        parent = self.queue.submit("調査結果をまとめる")
        # Stop after a tool result using the next model response as an explicit wait.
        first_create = client.create
        def create(items, instructions, tools, delta=None):
            if client.responses: return first_create(items, instructions, tools, delta)
            child = next(j for j in self.queue.jobs() if j["parent_id"] == parent)
            return call("wait_for_work", {"job_id": child["id"]}, call_id="wait")
        client.create = create
        self.assertEqual(runner.run_one(job_id=parent)["status"], "waiting")
        child = next(j for j in self.queue.jobs() if j["parent_id"] == parent)
        child_runner, child_client = self.runner([message("調査結果")])
        self.assertEqual(child_runner.run_one(job_id=child["id"])["status"], "completed")
        offered = {s["name"] for s in child_client.calls[0]["tools"]}
        self.assertNotIn("propose_action", offered); self.assertNotIn("request_information", offered)
        parent_runner, _ = self.runner([call("read_work_result", {"job_id": child["id"]}, call_id="read"), message("統合結果")])
        self.assertEqual(parent_runner.run_one(job_id=parent)["status"], "completed")
        self.assertEqual(self.queue.job(parent)["result"]["text"], "統合結果")

    def test_child_cannot_read_another_jobs_private_result(self):
        parent = self.queue.submit("依頼元"); job = self.queue.claim()
        outsider = self.queue.submit("別の依頼")
        execution = WorkExecution(self.queue, job, self.ingest)
        self.assertCode("WORK_SCOPE_DENIED", execution.dispatch, "read_work_result", {"job_id": outsider}, set())

    def test_child_capabilities_are_intersection_of_parent_and_requested_role(self):
        self.queue.set_profile("restricted", "制限付き", tools=READ_TOOLS | {"delegate_work"}, document_ids=[], project_ids=[])
        parent = self.queue.submit("依頼元", agent_id="restricted"); job = self.queue.claim()
        self.queue.set_profile("wide", "広い役割", tools=BASE_TOOLS)
        execution = WorkExecution(self.queue, job, self.ingest); execution.call_id = "delegate"
        with self.queue.fenced(parent, job["token"]):
            child = execution.dispatch("delegate_work", {"agent_id": "wide", "goal": "確認"}, set())
        cap = Capability.from_dict(self.queue.job(child["job_id"])["capability"])
        self.assertNotIn("propose_action", cap.tools); self.assertEqual(cap.document_ids, frozenset())

    def test_cancelling_parent_cancels_waiting_children_and_prevents_publication(self):
        parent = self.queue.submit("依頼元"); p = self.queue.claim()
        child = self.queue.submit("子の仕事", parent_id=parent); c = self.queue.claim(job_id=child)
        self.queue.suspend(parent, p["token"], {"kind": "children", "ids": [child]})
        self.queue.cancel(parent)
        self.assertEqual(self.queue.job(child)["status"], "cancelled")
        self.assertCode("STALE_RUN", self.queue.finish, child, c["token"], {"text": "遅い回答"})

    def test_artifact_versions_are_owned_and_retry_is_idempotent(self):
        jid = self.queue.submit("資料作成"); job = self.queue.claim()
        one = self.queue.artifact(jid, job["token"], "会議メモ", "初稿", [], artifact_id="a")
        same = self.queue.artifact(jid, job["token"], "会議メモ", "初稿", [], artifact_id="a")
        two = self.queue.artifact(jid, job["token"], "会議メモ", "改訂", [], artifact_id="b")
        self.assertEqual(one["version"], same["version"]); self.assertEqual(two["version"], 2)
        self.assertEqual(len(self.queue.artifacts(jid)), 2)

    def test_bridge_wait_resumes_when_request_completes(self):
        runner, client = self.runner([call("request_information", {"action": "search_mail", "query": "会議", "limit": 2, "meeting_id": None, "start": None, "end": None})])
        original = client.create
        def create(items, instructions, tools, delta=None):
            if client.responses: return original(items, instructions, tools, delta)
            request = next(x for x in reversed(items) if x.get("type") == "function_call_output")
            return call("wait_for_information", {"request_id": json.loads(request["output"])["request_id"]}, call_id="wait")
        client.create = create
        jid = self.queue.submit("追加のメールを調査")
        self.assertEqual(runner.run_one()["status"], "waiting")
        self.assertEqual(runner.run_one()["status"], "idle")
        request_id = self.queue.job(jid)["wait_spec"]["request_id"]
        self.bridge.receive({"schema_version": 1, "kind": "bridge_response", "request_id": request_id, "action": "search_mail", "status": "ok", "documents": [], "error_code": "", "coverage": "partial"})
        resumed, _ = self.runner([message("取得済みの範囲では見つかりませんでした")])
        self.assertEqual(resumed.run_one()["status"], "completed")

    def test_event_debounce_and_schedule_coalescing_survive_restart(self):
        now = now_utc()
        rid = self.queue.add_routine("毎分確認", "確認する", interval_seconds=60, at=stamp(now - timedelta(minutes=10)), enabled=True)
        self.assertEqual(self.queue.schedule(now=now), 1)
        other = DurableState(Store(self.store.path))
        self.assertEqual(other.schedule(now=now), 0)
        self.assertEqual(len(other.jobs()), 1)
        erid = other.add_routine("更新確認", "更新を確認", event_kind="user", event_key="change", enabled=True)
        other.publish("user", "change", {"revision": 1}, debounce_seconds=2)
        other.publish("user", "change", {"revision": 2}, debounce_seconds=2)
        self.assertEqual(other.schedule(), 0)
        self.assertEqual(other.schedule(now=now_utc() + timedelta(seconds=3)), 1)
        self.assertEqual(other.schedule(now=now_utc() + timedelta(seconds=4)), 0)

    def test_disabled_routine_never_enqueues_work(self):
        self.queue.add_routine("確認", "確認する", at=stamp(now_utc() - timedelta(seconds=1)))
        self.assertEqual(self.queue.schedule(), 0); self.assertFalse(self.queue.jobs())

    def test_revised_source_discards_checkpoint_then_restarts_root_goal(self):
        raw, did = self.add(); jid = self.queue.submit("資料更新後に再確認", document_ids=[did]); job = self.queue.claim()
        self.queue.checkpoint(jid, job["token"], {"items": ["古い資料の内容"]})
        raw.update(event_id=str(uuid.uuid4()), content="改訂された本文", source_modified_at=stamp(now_utc() + timedelta(microseconds=1)), observed_at=stamp(now_utc() + timedelta(microseconds=2)))
        self.ingest.envelope(raw)
        current = self.queue.job(jid)
        self.assertEqual(current["status"], "queued"); self.assertFalse(current["checkpoint"])
        self.assertEqual(current["goal"], "資料更新後に再確認")
        self.assertCode("STALE_RUN", self.queue.finish, jid, job["token"], {"text": "古い回答"})

    def test_process_crash_after_bridge_effect_replays_without_duplicate_request(self):
        jid = self.queue.submit("追加情報を取得")
        script = r'''
import json,os,sys
from pathlib import Path
from work_assistant.agent import Agent
from work_assistant.bedrock import ScriptedClient
from work_assistant.bridge import Bridge
from work_assistant.config import Settings
from work_assistant.execution import WorkRunner
from work_assistant.ingest import Ingestor
from work_assistant.state import DurableState
from work_assistant.store import Store
store=Store(Path(sys.argv[1]));home=Path(sys.argv[2]);cfg=Settings(local_data_approved=True,bridge_enabled=True,outbox=sys.argv[3])
ingest=Ingestor(store,cfg,home);bridge=Bridge(store,cfg,ingest)
response={"status":"completed","output":[{"type":"reasoning","encrypted_content":"reasoning-envelope"},{"type":"function_call","call_id":"request-1","name":"request_information","arguments":json.dumps({"action":"search_mail","query":"会議","limit":2,"meeting_id":None,"start":None,"end":None})}]}
agent=Agent(store,cfg,ingest,bridge,ScriptedClient([response]));original=bridge.request
def crash(*args,**kwargs):
 original(*args,**kwargs);os._exit(23)
bridge.request=crash
WorkRunner(agent.state,cfg,agent).run_one()
'''
        crashed = subprocess.run([sys.executable, "-c", script, str(self.store.path), str(self.home), str(self.out)], capture_output=True, timeout=20)
        self.assertEqual(crashed.returncode, 23, crashed.stderr.decode(errors="replace"))
        self.assertEqual(len(list(self.out.glob("*.request.json"))), 1)
        with self.store.connection() as cx:
            cx.execute("UPDATE work_jobs SET lease_expires=? WHERE id=?", (stamp(now_utc() - timedelta(seconds=1)), jid))
            cx.execute("UPDATE run_leases SET expires_at=?", (stamp(now_utc() - timedelta(seconds=1)),))
        resumed, client = self.runner([message("追加取得を依頼しました")])
        self.assertEqual(resumed.run_one()["status"], "completed")
        with self.store.connection() as cx: self.assertEqual(cx.execute("SELECT count(*) FROM bridge_requests").fetchone()[0], 1)
        self.assertEqual(len(list(self.out.glob("*.request.json"))), 1)
        self.assertIn("reasoning-envelope", canonical(client.calls[0]["input"]))

    def test_parallel_roles_use_separate_memory_and_complete_without_cross_talk(self):
        self.queue.set_profile("finance", "経理", "finance", tools=READ_TOOLS, document_ids=[], project_ids=[])
        self.queue.set_profile("research", "調査", "research", tools=READ_TOOLS, document_ids=[], project_ids=[])
        self.queue.submit("経理の確認", agent_id="finance"); self.queue.submit("研究の確認", agent_id="research")
        barrier = threading.Barrier(2); received = []; lock = threading.Lock()
        class Client:
            is_live = False
            def create(inner, items, instructions, tools, delta=None):
                with lock: received.append(canonical(items))
                barrier.wait(5)
                return message("経理の回答" if "finance" in instructions else "研究の回答")
        agent = Agent(self.store, self.cfg, self.ingest, self.bridge, Client())
        agent.memory.put("経理の確認: 予算はA", namespace="finance")
        agent.memory.put("研究の確認: 試料はB", namespace="research")
        runner = WorkRunner(self.queue, self.cfg, agent)
        with ThreadPoolExecutor(2) as pool: outcomes = list(pool.map(lambda _: runner.run_one(), range(2)))
        self.assertTrue(all(o["status"] == "completed" for o in outcomes))
        self.assertEqual(len(received), 2)
        self.assertFalse(any("予算はA" in text and "試料はB" in text for text in received))

    def test_foreground_arrival_yields_background_then_resumes_saved_turn(self):
        agent, client = self.agent([message("再開後の回答")])
        original = client.create
        lease = []
        def create(items, instructions, tools, delta=None):
            if not lease:
                lease.append(self.queue.admit_session("main", "interactive"))
                return message("途中の回答")
            return original(items, instructions, tools, delta)
        client.create = create
        runner = WorkRunner(self.queue, self.cfg, agent); jid = self.queue.submit("継続する依頼")
        self.assertEqual(runner.run_one()["status"], "interrupted")
        self.assertEqual(self.queue.job(jid)["status"], "queued")
        self.assertIsNone(self.queue.claim())
        self.queue.release_session(lease[0])
        with patch("work_assistant.state.now_utc", return_value=now_utc()+timedelta(seconds=3)):
            self.assertEqual(runner.run_one()["status"], "completed")
        self.assertEqual(self.queue.job(jid)["result"]["text"], "再開後の回答")

    def test_policy_revocation_reaches_children_and_discards_old_context(self):
        parent = self.queue.submit("親の依頼"); p = self.queue.claim()
        child = self.queue.submit("親から渡された内容", parent_id=parent, agent_id="monitor"); c = self.queue.claim(job_id=child)
        self.queue.checkpoint(child, c["token"], {"items": ["古い権限で読んだ内容"]})
        self.queue.set_profile("main", "制限後", tools=READ_TOOLS, document_ids=[], project_ids=[])
        self.assertEqual(self.queue.job(child)["status"], "failed")
        self.assertFalse(self.queue.job(child)["checkpoint"])
        self.assertNotIn("親から渡された内容", self.queue.job(child)["goal"])
        self.assertCode("STALE_RUN", self.queue.finish, child, c["token"], {"text": "古い権限の結果"})
        self.queue.retry(parent)
        restarted = self.queue.job(parent)
        self.assertFalse(restarted["checkpoint"]); self.assertEqual(restarted["capability"]["document_ids"], [])
        self.assertCode("RETRY_NOT_AVAILABLE", self.queue.retry, child)

    def test_deadline_expires_waiting_job_without_another_inference(self):
        now = now_utc(); jid = self.queue.submit("確認", deadline=stamp(now+timedelta(seconds=2)))
        job = self.queue.claim(); self.queue.suspend(jid, job["token"], {"kind": "timer", "at": stamp(now+timedelta(seconds=1))})
        self.assertIsNone(self.queue.claim(now=now+timedelta(seconds=3)))
        self.assertEqual(self.queue.job(jid)["error_code"], "JOB_DEADLINE")

    def test_retry_backoff_is_bounded_and_metrics_use_observed_results(self):
        runner, client = self.runner([]); jid = self.queue.submit("確認")
        def fail(*a, **k): raise AssistantError("BEDROCK_NETWORK_OR_TLS", retryable=True)
        client.create = fail
        self.assertEqual(runner.run_one()["status"], "failed")
        self.assertEqual(runner.run_one()["status"], "idle")
        failed = self.queue.job(jid); self.assertEqual(failed["status"], "queued")
        future = now_utc()+timedelta(seconds=11)
        resumed, _ = self.runner([message("復旧しました")])
        with patch("work_assistant.state.now_utc", return_value=future): self.assertEqual(resumed.run_one()["status"], "completed")
        self.queue.feedback(jid, helpful=True, intervened=True, retrieval_relevant=2, retrieval_total=3)
        metrics = self.queue.metrics()
        self.assertEqual(metrics["recovery_success_rate"], 1)
        self.assertEqual(metrics["intervention_rate"], 1)
        self.assertAlmostEqual(metrics["memory_retrieval_precision"], 2/3)
        self.assertIsNone(metrics["false_proactive_rate"])

    def test_proactive_admission_deduplicates_unchanged_evidence_and_defers_over_budget(self):
        raw, did = self.add(); self.cfg.heartbeat_enabled=True; self.cfg.bedrock.enabled=True
        self.cfg.active_start="00:00"; self.cfg.active_end="24:00"; self.cfg.weekdays=list(range(7)); self.cfg.briefing_times=[]
        agent, client = self.agent([message("NO_REPLY")]); scheduler=Scheduler(self.store,self.cfg,agent)
        now=now_utc(); first=scheduler.tick(now)
        self.assertEqual(first["status"], "queued"); self.assertFalse(client.calls)
        self.assertEqual(WorkRunner(self.queue,self.cfg,agent).run_one()["status"], "completed")
        self.store.set_state("monitor_last_attempt", stamp(now-timedelta(hours=1)))
        self.assertEqual(scheduler.tick(now)["reason"], "no_new_work")
        self.assertFalse(self.store.notifications())
        other, _ = self.add(source_id="other:transcript")
        self.store.reserve_budget(1000, 1, 1000000, now=now)
        self.cfg.bedrock.daily_calls=1
        self.assertEqual(scheduler.tick(now)["reason"], "no_new_work")
        self.assertEqual(self.store.get_state("opportunity:last")["assessment"]["reason"], "daily_budget")

    def test_pruning_retains_new_children_and_removes_finished_old_subtree(self):
        parent=self.queue.submit("親"); p=self.queue.claim()
        child=self.queue.submit("子",parent_id=parent); c=self.queue.claim(job_id=child)
        self.queue.finish(child,c["token"],{"text":"子の結果"}); self.queue.finish(parent,p["token"],{"text":"親の結果"})
        self.queue.prune(1,now=now_utc()+timedelta(hours=12)); self.assertEqual(len(self.queue.jobs()),2)
        self.queue.prune(1,now=now_utc()+timedelta(days=2)); self.assertFalse(self.queue.jobs())

    def test_cli_manages_roles_memory_schedules_and_jobs_as_utf8_json(self):
        from dataclasses import asdict
        (self.home/"config.json").write_text(json.dumps(asdict(self.cfg)),encoding="utf-8")
        def cli(*args):
            r=subprocess.run([sys.executable,"-m","work_assistant","--home",str(self.home),*args],capture_output=True,timeout=20)
            self.assertEqual(r.returncode,0,r.stderr.decode(errors="replace")); return json.loads(r.stdout.decode("utf-8"))
        cli("role","set","research","調査担当","--documents","","--projects","")
        cli("memory","add","測定条件の確認を優先する","--agent","research")
        self.assertEqual(len(cli("memory","search","測定条件","--agent","research")),1)
        job=cli("work","add","継続タスク","--agent","research")
        self.assertEqual(cli("work","show",job["job_id"])["goal"],"継続タスク")
        rid=cli("routine","add","毎時確認","資料を確認","--every-minutes","60")["routine_id"]
        cli("routine","enable",rid); self.assertTrue(cli("routine","list")[0]["enabled"])
        cli("work","cancel",job["job_id"])
        self.assertEqual(cli("work","list")[0]["status"],"cancelled")

    def test_parent_cannot_publish_before_children_finish_and_their_results_are_loaded(self):
        self.queue.set_profile("research", "調査", tools=READ_TOOLS)
        runner, _ = self.runner([call("delegate_work", {"agent_id":"research","goal":"調査する"}), message("まだ子の結果を見ていない回答")])
        parent = self.queue.submit("調査結果をまとめる")
        self.assertEqual(runner.run_one(job_id=parent)["status"], "waiting")
        self.assertFalse(self.queue.job(parent)["result"])
        child=next(j for j in self.queue.jobs() if j["parent_id"]==parent)
        child_runner,_=self.runner([message("子が確認した具体的な結果")])
        self.assertEqual(child_runner.run_one(job_id=child["id"])["status"],"completed")
        resumed,client=self.runner([message("確認結果を統合しました")])
        self.assertEqual(resumed.run_one(job_id=parent)["status"],"completed")
        self.assertIn("子が確認した具体的な結果",canonical(client.calls[0]["input"]))

    def test_long_work_context_is_compacted_without_splitting_tool_call_pairs(self):
        _,did=self.add(content="測定条件と評価方法。"*3000)
        self.cfg.max_context_chars=12000; self.cfg.max_model_rounds=12
        inputs=[]
        class Client:
            is_live=False
            reads=0
            summaries=0
            def create(inner,items,instructions,tools,delta=None):
                inputs.append(json.loads(canonical(items)))
                if not tools:
                    inner.summaries+=1; return message("測定条件を確認中。残りの原文を確認する。資料ID: "+did)
                inner.reads+=1
                if inner.reads<=7:
                    return call("read_document",{"document_id":did,"offset":(inner.reads-1)*2000,"length":2000},call_id=f"read-{inner.reads}",extra=[{"type":"reasoning","encrypted_content":f"proof-{inner.reads}"}])
                return message("確認しました [資料:"+did+"]")
        client=Client(); agent=Agent(self.store,self.cfg,self.ingest,self.bridge,client)
        jid=self.queue.submit("資料を確認: "+did)
        result=WorkRunner(self.queue,self.cfg,agent).run_one(job_id=jid)
        self.assertEqual(result["status"],"completed",result)
        self.assertGreaterEqual(client.summaries,1)
        self.assertIn("WORK_SUMMARY_REFERENCE",canonical(inputs[-1]))
        calls={o["call_id"] for o in inputs[-1] if o.get("type")=="function_call"}
        outputs={o["call_id"] for o in inputs[-1] if o.get("type")=="function_call_output"}
        self.assertEqual(calls,outputs)
        self.assertIn("proof-7",canonical(inputs[-1]))

    def test_local_file_revision_waits_for_reimport_then_restarts_from_fresh_content(self):
        path=self.root/"meeting.txt"; path.write_text("変更前の測定条件",encoding="utf-8"); self.ingest.file(path)
        did=self.store.documents()[0]["id"]; jid=self.queue.submit("変更後に確認",document_ids=[did]); job=self.queue.claim()
        self.queue.checkpoint(jid,job["token"],{"items":["古い内容"]})
        path.write_text("変更後の測定条件と新しい評価方法",encoding="utf-8")
        self.assertCode("SOURCE_UNAVAILABLE",self.ingest.validate_live_source,did)
        self.assertEqual(self.queue.job(jid)["status"],"waiting")
        self.ingest.file(path)
        self.assertEqual(self.queue.job(jid)["status"],"queued")
        runner,client=self.runner([message("新しい原文を確認しました")])
        self.assertEqual(runner.run_one(job_id=jid)["status"],"completed")
        self.assertIn("変更後の測定条件",canonical(client.calls[0]["input"]))
        self.assertNotIn("変更前の測定条件",canonical(client.calls[0]["input"]))

    def test_invalid_final_answer_is_discarded_before_user_requested_retry(self):
        runner,_=self.runner([message("誤った出典 [資料:invented]")]); jid=self.queue.submit("確認")
        self.assertEqual(runner.run_one()["error_code"],"CITATION_NOT_VERIFIED")
        self.queue.retry(jid)
        resumed,client=self.runner([message("確認できた範囲を回答します")])
        self.assertEqual(resumed.run_one()["status"],"completed")
        self.assertNotIn("invented",canonical(client.calls[0]["input"]))

    def test_event_revision_survives_wall_clock_reversal_and_expired_inbox_rows(self):
        now=now_utc(); self.queue.publish("user","update",{"version":1},now=now)
        rid=self.queue.add_routine("変更確認","変更を確認",event_kind="user",event_key="update",enabled=True)
        # The second event has an earlier clock but a later durable sequence.
        self.queue.publish("user","update",{"version":2},now=now-timedelta(seconds=2))
        self.assertEqual(self.queue.schedule(now=now),1)
        self.queue.publish("user","update",{"version":2},now=now)
        self.assertEqual(self.queue.schedule(now=now),0)
        with self.store.connection() as cx: cx.execute("DELETE FROM runtime_events")
        self.queue.publish("user","update",{"version":3},now=now)
        self.assertEqual(self.queue.schedule(now=now),1)

    def test_two_runtimes_keep_live_chat_owner_and_reject_competing_turn(self):
        from work_assistant.runtime import Runtime
        entered,release=threading.Event(),threading.Event()
        class Client:
            is_live=False
            def create(inner,*args):
                entered.set(); release.wait(5); return message("最初の回答")
        first=Runtime(self.home,self.cfg,Client())
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(first.ask,"最初の質問")
            self.assertTrue(entered.wait(5))
            second=Runtime(self.home,self.cfg,ScriptedClient([message("次の回答")]))
            self.assertEqual(second.store.messages("main")[0]["status"],"running")
            self.assertCode("SESSION_BUSY",second.ask,"競合する質問")
            release.set(); self.assertEqual(future.result(timeout=5).text,"最初の回答")
        self.assertEqual(second.ask("次の質問").text,"次の回答")
        self.assertEqual(len(second.store.messages("main")),4)
        first.close();second.close()

    def test_human_approval_wait_does_not_allow_model_to_approve(self):
        raw,did=self.add()
        args={"kind":"action","title":"測定条件を確認","document_id":did,"quote":raw["content"],"confidence":.9,"project_id":None,"due_at":None}
        runner,client=self.runner([call("propose_action",args)])
        original=client.create
        def create(items,instructions,tools,delta=None):
            self.assertFalse(any(s["name"] in {"approve","decide_proposal"} for s in tools))
            if client.responses: return original(items,instructions,tools,delta)
            proposal=self.store.proposals()[0]
            return call("wait_for_approval",{"proposal_id":proposal["id"]},call_id="approval-wait")
        client.create=create
        jid=self.queue.submit("確認事項を提案し、承認後に整理する",document_ids=[did])
        self.assertEqual(runner.run_one()["status"],"waiting")
        self.assertEqual(runner.run_one()["status"],"idle"); self.assertFalse(self.store.tasks())
        pid=self.queue.job(jid)["wait_spec"]["proposal_id"]
        self.store.decide_proposal(pid,True)
        resumed,new_client=self.runner([message("承認された確認事項を整理しました")])
        self.assertEqual(resumed.run_one()["status"],"completed")
        self.assertIn('"status":"approved"',canonical(new_client.calls[0]["input"]).replace('\\"','"'))
        self.assertEqual(len(self.store.tasks()),1)

    def test_daily_budget_exhaustion_waits_for_next_day_instead_of_losing_task(self):
        runner,client=self.runner([])
        def exhausted(*a,**k): raise AssistantError("DAILY_BUDGET_EXCEEDED")
        client.create=exhausted
        jid=self.queue.submit("予算更新後に続ける")
        runner.run_one(); job=self.queue.job(jid)
        self.assertEqual(job["status"],"waiting"); self.assertEqual(job["wait_spec"]["reason"],"daily_budget")
        self.assertEqual(runner.run_one()["status"],"idle")
        resumed,_=self.runner([message("予算更新後の結果")])
        from work_assistant.common import parse_time
        with patch("work_assistant.state.now_utc",return_value=parse_time(job["wait_spec"]["at"])+timedelta(seconds=1)):
            self.assertEqual(resumed.run_one()["status"],"completed")

    def test_proactive_role_does_not_receive_forbidden_project_context(self):
        _,did=self.add(kind="capture",title="案件Aの測定条件")
        pid=self.store.add_project("案件A","共有しない案件の方針",["案件A"])
        self.cfg.heartbeat_enabled=True;self.cfg.bedrock.enabled=True;self.cfg.active_start="00:00";self.cfg.active_end="24:00";self.cfg.weekdays=list(range(7));self.cfg.briefing_times=[]
        self.queue.set_profile("monitor","定期確認",tools=BASE_TOOLS|{"notify"},document_ids=[did],project_ids=[])
        agent,client=self.agent([]);scheduler=Scheduler(self.store,self.cfg,agent)
        self.assertEqual(scheduler.tick()["reason"],"no_new_work");self.assertFalse(client.calls)
        self.queue.set_profile("monitor","定期確認",tools=BASE_TOOLS|{"notify"},document_ids=[did],project_ids=[pid])
        queued=scheduler.tick()
        self.assertEqual(queued["status"],"queued")
        self.assertNotIn("共有しない案件の方針",self.queue.job(queued["job_id"])["goal"])

    def test_same_text_file_resave_also_releases_revision_wait(self):
        path=self.root/"resaved.txt";path.write_text("同じ本文",encoding="utf-8");self.ingest.file(path)
        did=self.store.documents()[0]["id"];jid=self.queue.submit("資料の変更後に確認",document_ids=[did]);job=self.queue.claim()
        before=path.stat();os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns+1000000000))
        self.assertCode("SOURCE_UNAVAILABLE",self.ingest.validate_live_source,did)
        self.assertEqual(self.queue.job(jid)["status"],"waiting")
        self.ingest.file(path)
        with patch("work_assistant.state.now_utc",return_value=now_utc()+timedelta(seconds=3)):
            resumed=self.queue.claim(job_id=jid)
        self.assertIsNotNone(resumed)
        self.assertFalse(resumed["checkpoint"])


class MemoryTests(Fixture):
    def setUp(self):
        super().setUp(); self.engine = MemoryEngine(self.store, self.cfg, self.ingest)

    def test_memory_is_namespace_scoped_and_preserves_provenance(self):
        self.engine.put("経費の確認は月曜日", namespace="finance")
        self.engine.put("測定条件の確認は火曜日", namespace="research")
        found = self.engine.search("確認", Capability("finance"))
        self.assertEqual(len(found), 1); self.assertIn("経費", found[0]["body"])
        self.assertEqual(found[0]["source_kind"], "user")

    def test_lexical_retrieval_selects_relevant_notes_from_distractors(self):
        wanted = self.engine.put("会議前に測定条件と評価方法を確認する")
        self.engine.put("請求書の支払先を確認する")
        self.engine.put("出張の交通費と領収書を保存する")
        result = self.engine.search("測定条件 評価方法", Capability())
        self.assertEqual(result[0]["id"], wanted)

    def test_vector_retrieval_can_find_non_overlapping_terms_without_network(self):
        class FixtureEmbedder:
            model_id = "offline-test-vectors"
            def encode(self, texts):
                return [[1., 0.] if s in ("automobile", "車両の仕様") else [0., 1.] for s in texts]
        engine = MemoryEngine(self.store, self.cfg, self.ingest, FixtureEmbedder())
        wanted = engine.put("車両の仕様"); engine.put("請求書の支払い")
        result = engine.search("automobile", Capability())
        self.assertEqual(result[0]["id"], wanted)
        self.assertAlmostEqual(cosine([1, 0], [1, 0]), 1)

    def test_memory_source_expiry_revokes_index_and_cached_content(self):
        _, did = self.add(); mid = self.engine.put("測定条件のメモ", document_id=did)
        self.assertTrue(self.engine.search("測定条件", Capability()))
        self.store.invalidate(did)
        self.assertFalse(self.engine.search("測定条件", Capability()))
        with self.store.connection() as cx: self.assertEqual(cx.execute("SELECT count(*) FROM memory_index").fetchone()[0], 0)

    def test_expired_and_forbidden_memories_are_not_retrieved(self):
        _, did = self.add(); self.engine.put("測定条件のメモ", document_id=did)
        self.assertFalse(self.engine.search("測定条件", Capability(document_ids=frozenset())))
        mid = self.engine.put("別のメモ")
        self.engine.prune(now_utc() + timedelta(days=31))
        self.assertFalse(self.engine.search("メモ", Capability()))

    def test_compaction_keeps_recent_turns_and_saves_summary_without_deleting_raw_history(self):
        self.cfg.compaction_trigger_messages = 16; self.cfg.compaction_min_chars = 2000
        for i in range(28): self.store.remember_turn("main", "user" if i % 2 == 0 else "assistant", f"記録{i}: " + "測定条件を確認する。" * 20)
        agent, client = self.agent([message("過去の会話要約: 測定条件と未確認事項"), message("今回の回答")])
        result = agent.turn("続きを確認")
        self.assertEqual(result.text, "今回の回答"); self.assertEqual(len(client.calls), 2)
        self.assertFalse(client.calls[0]["tools"])
        context = canonical(client.calls[1]["input"])
        self.assertIn("CONVERSATION_SUMMARY_REFERENCE", context); self.assertIn("記録27", context)
        self.assertNotIn("記録0:", context)
        self.assertEqual(len(self.store.messages("main")), 30)

    def test_fabricated_tool_calls_from_summarizer_are_rejected(self):
        self.cfg.compaction_trigger_messages = 16; self.cfg.compaction_min_chars = 2000
        for i in range(28): self.store.remember_turn("main", "user", "確認する。" * 60)
        agent, client = self.agent([call("propose_action", {})])
        self.assertCode("SUMMARY_INVALID", agent.turn, "確認")
        self.assertFalse(self.store.proposals())

    def test_unconfigured_or_missing_embedding_model_has_explicit_lexical_fallback(self):
        self.cfg.memory_embedding_model = str(self.home / "missing-model")
        engine = MemoryEngine(self.store, self.cfg, self.ingest)
        self.assertIsNone(engine.embedder); self.assertEqual(engine.embedding_error, "EMBEDDING_MODEL_UNAVAILABLE")
        engine.put("会議のメモ"); self.assertTrue(engine.search("会議", Capability()))

    def test_tool_policy_is_enforced_even_for_direct_fabricated_calls(self):
        _, did = self.add()
        tools = Tools(self.store, self.cfg, self.ingest, self.bridge, "scoped", capability=Capability("restricted", READ_TOOLS, frozenset(), frozenset()))
        self.assertCode("SOURCE_SCOPE_DENIED", tools.execute, "read_document", {"document_id": did, "offset": 0, "length": 100})
        self.assertFalse(tools.execute("search_documents", {"query": "測定条件", "limit": 10, "kind": None})["results"])
        self.assertCode("TOOL_POLICY_DENIED", tools.execute, "request_information", {"action": "search_mail", "query": "会議", "limit": 2, "meeting_id": None, "start": None, "end": None})

    def test_compaction_network_failure_keeps_recent_turns_without_repeating_failed_summary(self):
        self.cfg.compaction_trigger_messages=16; self.cfg.compaction_min_chars=2000
        for i in range(28): self.store.remember_turn("main","user",f"記録{i}: "+"確認する。"*60)
        agent,client=self.agent([message("今回の回答"),message("次の回答")]); original=client.create; attempts=[]
        def create(items,instructions,tools,delta=None):
            if not tools:
                attempts.append(True); raise AssistantError("BEDROCK_NETWORK_OR_TLS")
            return original(items,instructions,tools,delta)
        client.create=create
        self.assertEqual(agent.turn("確認").text,"今回の回答")
        self.assertIn("要約未完了",canonical(client.calls[0]["input"]))
        self.assertIn("記録27",canonical(client.calls[0]["input"]))
        self.assertEqual(agent.turn("次の確認").text,"次の回答"); self.assertEqual(len(attempts),1)

    def test_forgotten_approved_note_is_not_automatically_recreated(self):
        raw,did=self.add()
        pid=self.store.propose(kind="memory",title="測定条件の記憶",document_id=did,quote=raw["content"],confidence=.9)
        self.store.decide_proposal(pid,True)
        item=self.engine.search("測定条件",Capability())[0]
        self.engine.forget(item["id"])
        self.assertFalse(self.engine.search("測定条件",Capability()))

    def test_embedding_model_change_reindexes_existing_namespace_only(self):
        self.engine.put("車両の仕様"); self.engine.put("経費の確認",namespace="finance")
        class FixtureEmbedder:
            model_id="new-model"
            def encode(self,texts): return [[1.,0.] for _ in texts]
        engine=MemoryEngine(self.store,self.cfg,self.ingest,FixtureEmbedder())
        engine.search("automobile",Capability())
        with self.store.connection() as cx:
            owners={r[0]:r[1] for r in cx.execute("SELECT namespace,embedding_model FROM memory_items")}
        self.assertEqual(owners["main"],"new-model"); self.assertEqual(owners["finance"],"")

    def test_long_procedural_memory_can_be_read_in_pages_with_namespace_enforcement(self):
        mid=self.engine.put("週次報告の手順。"*400,kind="procedural",namespace="research")
        cap=Capability("research")
        first=self.engine.read(mid,cap,length=1000)
        self.assertEqual(len(first["text"]),1000); self.assertEqual(first["next_offset"],1000)
        self.assertCode("MEMORY_NOT_FOUND",self.engine.read,mid,Capability("finance"))

    def test_project_scope_filters_approved_memory_and_rejects_cross_project_proposals(self):
        raw,did=self.add();pid=self.store.add_project("制限案件","案件の目的",["測定条件"])
        proposal=self.store.propose(kind="memory",title="測定条件の方針",document_id=did,quote=raw["content"],confidence=.9,project_id=pid)
        self.store.decide_proposal(proposal,True)
        allowed=Capability(project_ids=frozenset({pid}))
        mid=self.engine.search("測定条件",allowed)[0]["id"]
        denied=Capability(project_ids=frozenset())
        self.assertFalse(self.engine.search("測定条件",denied))
        self.assertCode("MEMORY_NOT_FOUND",self.engine.read,mid,denied)
        tools=Tools(self.store,self.cfg,self.ingest,self.bridge,"main",capability=denied)
        tools.execute("read_document",{"document_id":did,"offset":0,"length":1000})
        self.assertCode("PROJECT_SCOPE_DENIED",tools.execute,"propose_action",{"kind":"action","title":"別の確認","document_id":did,"quote":raw["content"],"confidence":.9,"project_id":pid,"due_at":None})
