from __future__ import annotations
import io
import json
import os
import struct
import tempfile
import threading
import unittest
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from work_assistant.__main__ import synthetic_envelope
from work_assistant.agent import Agent
from work_assistant.bedrock import BedrockClient, ScriptedClient, validate_response
from work_assistant.bridge import Bridge, validate_arguments
from work_assistant.common import AssistantError, JST, atomic_json, canonical, digest, now_utc, parse_time, stamp
from work_assistant.config import BedrockSettings, Settings, initialize, load
from work_assistant.extract import extract_bytes, html_text, vtt_text
from work_assistant.ingest import Ingestor, validate_envelope
from work_assistant.native import capture, read_frame, write_frame
from work_assistant.runtime import Runtime
from work_assistant.scheduler import Scheduler
from work_assistant.security import allowed_path, safe_web_url, validate_bedrock
from work_assistant.store import Store
from work_assistant.tools import Tools, active_hours


def message(text="回答"):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]}


def call(name, args, call_id="c1", extra=None):
    return {"status": "completed", "output": (extra or []) + [{"type": "function_call", "call_id": call_id, "name": name, "arguments": canonical(args)}]}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "state"
        initialize(self.home)
        self.root = self.base / "docs"; self.root.mkdir()
        self.drop = self.base / "drop"; self.drop.mkdir()
        self.out = self.base / "out"; self.out.mkdir()
        self.cfg = Settings(local_data_approved=True, roots=[str(self.root)], drop_folders=[str(self.drop)],
                            outbox=str(self.out), bridge_enabled=True, allowed_web_hosts=["example.invalid"], extension_ids=["a" * 32])
        self.store = Store(self.home / "test.sqlite3")
        self.ingest = Ingestor(self.store, self.cfg, self.home)
        self.bridge = Bridge(self.store, self.cfg, self.ingest)
    def tearDown(self): self.tmp.cleanup()
    def add(self, **changes):
        raw = synthetic_envelope(); raw.update(changes)
        self.ingest.envelope(raw)
        did = str(uuid.uuid5(uuid.NAMESPACE_URL, "m365:" + raw["source_id"]))
        return raw, did
    def tools(self, mode="chat"):
        return Tools(self.store, self.cfg, self.ingest, self.bridge, "main", mode, "test-event")
    def agent(self, responses):
        client = ScriptedClient(responses)
        return Agent(self.store, self.cfg, self.ingest, self.bridge, client), client
    def assertCode(self, code, func, *args, **kwargs):
        with self.assertRaises(AssistantError) as context: func(*args, **kwargs)
        self.assertEqual(context.exception.code, code)


class StorageTests(Fixture):
    def test_default_off(self):
        cfg = load(self.home)
        self.assertFalse(cfg.local_data_approved); self.assertFalse(cfg.bedrock.enabled)
    def test_japanese_search(self):
        _, did = self.add()
        self.assertEqual(self.store.search("塗工幅")[0]["id"], did)
    def test_fts_query_is_data(self):
        self.add(); self.store.search('" OR 1=1; DROP TABLE documents; --')
        self.assertEqual(self.store.stats()["documents"], 1)
    def test_duplicate_receipt(self):
        raw, _ = self.add(); self.assertEqual(self.ingest.envelope(raw), "duplicate")
    def test_collision(self):
        raw, _ = self.add(); raw["content"] = "別の原文"
        self.assertCode("EVENT_ID_COLLISION", self.ingest.envelope, raw)
    def test_old_revision(self):
        raw, did = self.add(); raw["event_id"] = str(uuid.uuid4()); raw["source_modified_at"] = stamp(now_utc()-timedelta(days=1)); raw["content"] = "古い"
        self.assertEqual(self.ingest.envelope(raw), "older_revision_ignored")
        self.assertIn("塗工", self.store.get_document(did)["body"])
    def test_deleted_cannot_resurrect_out_of_order(self):
        raw, did = self.add(); tomb = dict(raw, kind="tombstone", content="", event_id=str(uuid.uuid4()))
        self.ingest.envelope(tomb)
        self.assertFalse(self.store.search("塗工"))
        raw["event_id"] = str(uuid.uuid4())
        self.assertEqual(self.ingest.envelope(raw), "revoked_revision_ignored")
        self.assertCode("SOURCE_UNAVAILABLE", self.store.get_document, did)
    def test_delete_before_arrival(self):
        raw = synthetic_envelope(); self.ingest.envelope(dict(raw, kind="tombstone", content="", event_id=str(uuid.uuid4())))
        self.assertEqual(self.ingest.envelope(raw), "revoked_revision_ignored")
    def test_new_authorized_observation_after_revocation(self):
        raw, did = self.add(); self.ingest.envelope(dict(raw, kind="tombstone", content="", event_id=str(uuid.uuid4())))
        raw.update(event_id=str(uuid.uuid4()), observed_at=stamp(now_utc()+timedelta(seconds=1)))
        self.ingest.envelope(raw); self.assertIn("塗工", self.store.get_document(did)["body"])
    def test_expiry_removes_derived(self):
        raw, did = self.add(); self.store.remember_turn("main", "assistant", raw["content"])
        self.store.expire(now_utc()+timedelta(days=2))
        self.assertFalse(self.store.search("塗工")); self.assertFalse(self.store.history("main"))
        with self.store.connection() as cx:
            self.assertEqual(cx.execute("SELECT body,title,metadata FROM documents").fetchone()[:], ("", "", "{}"))
    def test_clock_requires_timezone(self): self.assertCode("INVALID_TIMESTAMP", parse_time, "2026-10-16T10:00:00")
    def test_reserved_metadata(self):
        raw = synthetic_envelope(); raw["metadata"]["origin"] = "local"
        self.assertCode("RESERVED_METADATA", self.ingest.envelope, raw)
    def test_calendar_requires_interval(self):
        raw = synthetic_envelope(kind="calendar")
        self.assertCode("CALENDAR_TIME_REQUIRED", self.ingest.envelope, raw)
    def test_not_approved(self):
        self.cfg.local_data_approved = False
        self.assertCode("LOCAL_DATA_NOT_APPROVED", self.ingest.envelope, synthetic_envelope())
    def test_atomic_budget(self):
        self.store.reserve_budget(100, 1, 1000)
        self.assertCode("DAILY_BUDGET_EXCEEDED", self.store.reserve_budget, 100, 1, 1000)
    def test_audit_no_body(self): self.assertCode("UNSAFE_AUDIT_VALUE", self.store.audit, "request", "secret body")
    def test_proposal_approval(self):
        _, did = self.add(); pid = self.store.propose(kind="action", title="条件確認", document_id=did, quote="次回までに測定条件を確認します。", confidence=.9)
        self.assertFalse(self.store.tasks()); self.store.decide_proposal(pid, True); self.assertEqual(len(self.store.tasks()), 1)
    def test_fake_quote_blocked(self):
        _, did = self.add()
        self.assertCode("PROVENANCE_INVALID", self.store.propose, kind="action", title="x", document_id=did, quote="存在しない", confidence=.9)
    def test_completed_task_not_revived(self):
        _, did = self.add(); pid = self.store.propose(kind="action", title="確認", document_id=did, quote="次回までに測定条件を確認します。", confidence=.9)
        self.store.decide_proposal(pid, True); self.store.complete_task(self.store.tasks()[0]["id"])
        context = self.tools().execute("get_project_context", {"project_id": None})
        self.assertFalse(context["open_tasks"]); self.assertFalse(context["confirmed_notes"])


class IngestionTests(Fixture):
    def test_path_outside(self):
        p = self.base / "outside.txt"; p.write_text("秘密")
        self.assertCode("PATH_NOT_ALLOWED", self.ingest.file, p)
    def test_state_blocked(self):
        self.cfg.roots.append(str(self.home)); p = self.home / "private.txt"; p.write_text("秘密")
        self.assertCode("STATE_DIRECTORY_BLOCKED", self.ingest.file, p)
    def test_symlink_blocked(self):
        target = self.base / "outside.txt"; target.write_text("秘密")
        p = self.root / "link.txt"
        try: p.symlink_to(target)
        except (OSError, NotImplementedError): self.skipTest("Symlink privilege unavailable")
        self.assertCode("LINK_BLOCKED", self.ingest.file, p)
    def test_secret_name(self):
        p = self.root / "secrets.json"; p.write_text("{}")
        self.assertCode("SENSITIVE_PATH_BLOCKED", self.ingest.file, p)
    def test_file_refresh_after_change(self):
        p = self.root / "notes.txt"; p.write_text("古い資料", encoding="utf-8")
        self.ingest.scan_files(); self.ingest.scan_files(); self.assertTrue(self.store.search("古い"))
        p.write_text("新しい塗工資料です", encoding="utf-8")
        self.ingest.scan_files(); self.ingest.scan_files(); self.assertTrue(self.store.search("新しい"))
    def test_live_file_revalidation(self):
        p = self.root / "notes.txt"; p.write_text("原文", encoding="utf-8"); self.ingest.file(p)
        did = self.store.documents()[0]["id"]; p.unlink()
        self.assertCode("SOURCE_UNAVAILABLE", self.ingest.validate_live_source, did)
    def test_drop_stability_and_retry_fixed_file(self):
        p = self.drop / "one.json"; p.write_text("bad")
        self.ingest.scan_drops(); self.assertTrue(self.ingest.scan_drops()["errors"])
        self.assertFalse(self.ingest.scan_drops()["errors"])
        atomic_json(p, synthetic_envelope()); self.ingest.scan_drops(); self.ingest.scan_drops()
        self.assertTrue(self.store.search("塗工"))
    def test_vtt_speakers_and_times(self):
        text, meta = vtt_text("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v 田中>塗工を確認します。</v>\n")
        self.assertIn("田中", text); self.assertIn("00:00:01.000", text); self.assertIn("塗工", text)
    def test_html_removes_active_content(self):
        self.assertNotIn("secret", html_text("<script>secret</script><p>本文</p><form>secret</form>"))
    def test_docx_extract(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>測定条件</w:t></w:r></w:p></w:body></w:document>')
        text, _ = extract_bytes(stream.getvalue(), ".docx"); self.assertIn("測定条件", text)
    def test_xml_entities_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY a "secret">]><x>&a;</x>')
        with self.assertRaises(AssistantError): extract_bytes(stream.getvalue(), ".docx")


class AgentTests(Fixture):
    def test_reasoning_and_call_id_roundtrip(self):
        _, did = self.add()
        reasoning = {"type": "reasoning", "id": "r1", "encrypted_content": "opaque", "summary": []}
        agent, client = self.agent([call("read_document", {"document_id": did, "offset": 0, "length": 1000}, extra=[reasoning]), message("確認しました [資料:"+did+"]")])
        result = agent.turn("会議の確認事項は？")
        self.assertIn(reasoning, client.calls[1]["input"])
        self.assertTrue(any(i.get("call_id") == "c1" and i.get("type") == "function_call_output" for i in client.calls[1]["input"]))
        self.assertEqual(result.citations[0]["id"], did)
    def test_unknown_tool_not_executed(self):
        agent, client = self.agent([call("execute_shell", {"command": "bad"}), message("実行できません")])
        result = agent.turn("依頼")
        self.assertNotEqual(result.trace[0]["status"], "ok")
    def test_no_approval_tool(self):
        self.assertNotIn("approve", " ".join(x["name"] for x in self.tools().schemas()))
    def test_unverified_citation_blocked(self):
        agent, _ = self.agent([message("[資料:made-up]")])
        self.assertCode("CITATION_NOT_VERIFIED", agent.turn, "依頼")
    def test_model_incomplete(self): self.assertCode("MODEL_RESPONSE_INCOMPLETE", validate_response, {"status": "incomplete", "output": []})
    def test_round_limit(self):
        self.cfg.max_model_rounds = 1; agent, _ = self.agent([call("get_project_context", {"project_id": None})])
        self.assertCode("MODEL_ROUND_LIMIT", agent.turn, "依頼")
    def test_cancellation(self):
        cancel = threading.Event(); cancel.set(); agent, client = self.agent([message()])
        self.assertCode("TURN_PREEMPTED", agent.turn, "依頼", cancel=cancel); self.assertFalse(client.calls)
    def test_call_id_collision(self):
        agent, _ = self.agent([call("get_project_context", {"project_id": None}), call("search_conversation", {"query": "x"})])
        self.assertCode("TOOL_CALL_ID_COLLISION", agent.turn, "依頼")
    def test_read_before_propose(self):
        _, did = self.add()
        self.assertCode("READ_SOURCE_FIRST", self.tools().execute, "propose_action", {"kind": "action", "title": "確認", "document_id": did, "quote": "次回までに測定条件を確認します。", "confidence": .9, "project_id": None, "due_at": None})
    def test_no_notify_in_chat(self):
        self.assertNotIn("notify", [s["name"] for s in self.tools().schemas()])
    def test_strict_tool_arguments(self):
        self.assertCode("INVALID_OBJECT", self.tools().execute, "get_project_context", {"project_id": None, "command": "bad"})
    def test_runtime_offline(self):
        rt = Runtime(self.home, self.cfg, ScriptedClient([message("確認")]))
        self.assertEqual(rt.ask("こんにちは").text, "確認")


class TransportTests(Fixture):
    def test_endpoint_openai_blocked(self):
        b = BedrockSettings(endpoint="https://api.openai.com/v1")
        self.assertCode("BEDROCK_ENDPOINT_BLOCKED", validate_bedrock, b, live=False)
    def test_endpoint_suffix_attack(self):
        b = BedrockSettings(endpoint="https://bedrock-runtime.ap-northeast-1.amazonaws.com.attacker.invalid/openai/v1")
        self.assertCode("BEDROCK_ENDPOINT_BLOCKED", validate_bedrock, b, live=False)
    def test_cross_region_explicit(self):
        b = BedrockSettings(enabled=True, data_transfer_approved=True)
        self.assertCode("CROSS_REGION_NOT_APPROVED", validate_bedrock, b)
    def test_runtime_profile_required(self):
        self.assertCode("INFERENCE_PROFILE_REQUIRED", validate_bedrock, BedrockSettings(model="openai.gpt-6-luna"), live=False)
    def test_mantle_special_path(self):
        b = BedrockSettings(endpoint="https://bedrock-mantle.us-east-1.api.aws/openai/v1", region="us-east-1", model="openai.gpt-6-luna")
        self.assertEqual(validate_bedrock(b, live=False)[1], "bedrock-mantle")
    def test_wire_store_false(self):
        b = BedrockSettings(enabled=True, data_transfer_approved=True, cross_region_approved=True, auth="api_key", streaming=False)
        class Response(io.BytesIO): headers = {"Content-Type": "application/json"}
        class Opener:
            def open(inner, request, timeout):
                inner.request = request
                return Response(canonical(message()).encode())
        opener = Opener()
        with patch.dict(os.environ, {b.api_key_env: "synthetic-key"}):
            BedrockClient(b, opener).create([], "test", [])
        payload = json.loads(opener.request.data)
        self.assertIs(payload["store"], False); self.assertNotIn("previous_response_id", payload)
    def test_sse_delta_and_completion(self):
        content = 'data: '+canonical({"type": "response.output_text.delta", "delta": "テスト"})+'\n\ndata: '+canonical({"type": "response.completed", "response": message()})+'\n\n'
        deltas=[]; result=BedrockClient._read_stream(io.BytesIO(content.encode()), deltas.append)
        self.assertEqual(deltas,["テスト"]); self.assertEqual(result["status"], "completed")
    def test_sse_truncation(self): self.assertCode("MODEL_STREAM_TRUNCATED", BedrockClient._read_stream, io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'))
    def test_bridge_duplicate_and_no_retained_body(self):
        request = self.bridge.request("search_mail", {"query": "測定", "limit": 2})
        response = {"schema_version":1,"kind":"bridge_response","request_id":request["request_id"],"action":"search_mail","status":"ok","documents":[synthetic_envelope(kind="mail")],"error_code":"","coverage":"partial"}
        self.assertEqual(self.bridge.receive(response), "ok"); self.assertEqual(self.bridge.receive(response), "duplicate")
        with self.store.connection() as cx: self.assertNotIn("塗工", cx.execute("SELECT result FROM bridge_requests").fetchone()[0])
    def test_bridge_unknown_id(self):
        response={"schema_version":1,"kind":"bridge_response","request_id":"unknown","action":"search_mail","status":"ok","documents":[],"error_code":"","coverage":"unknown"}
        self.assertCode("BRIDGE_CORRELATION_FAILED", self.bridge.receive, response)
    def test_bridge_allowlist(self): self.assertCode("BRIDGE_ACTION_BLOCKED", validate_arguments, "send_email", {})
    def test_native_binary_unicode(self):
        stream=io.BytesIO(); write_frame(stream, {"text":"塗工\n条件"}); stream.seek(0)
        self.assertEqual(read_frame(stream)["text"], "塗工\n条件")
    def test_native_fragmented_stream(self):
        class Fragment(io.BytesIO):
            def read(self, size=-1): return super().read(min(size,1))
        data=io.BytesIO(); write_frame(data,{"operation":"ping"})
        self.assertEqual(read_frame(Fragment(data.getvalue())), {"operation":"ping"})
    def test_native_length_guard(self): self.assertCode("NATIVE_FRAME_LIMIT", read_frame, io.BytesIO(struct.pack("=I", 1_000_001)))
    def test_capture_url_redaction(self):
        result=capture({"operation":"capture","url":"https://example.invalid/a?token=secret#x","title":"資料","text":"確認した文章","scope":"selection"}, self.cfg,self.home,"chrome-extension://"+"a"*32+"/")
        self.assertEqual(result["status"],"saved_locally")
        raw=json.loads(next((self.home/"captures").glob("*.json")).read_text())
        self.assertEqual(raw["source_url"],"https://example.invalid/a")
    def test_capture_disallowed_origin(self): self.assertCode("NATIVE_ORIGIN_BLOCKED", capture, {"operation":"ping"},self.cfg,self.home,"chrome-extension://"+"b"*32+"/")
    def test_capture_disallowed_host(self): self.assertCode("WEB_HOST_BLOCKED", safe_web_url, "https://attacker.invalid", self.cfg.allowed_web_hosts)


class ScheduleTests(Fixture):
    def test_quiet_hours(self):
        self.assertFalse(active_hours(self.cfg, parse_time("2026-10-16T23:00:00+09:00")))
    def test_weekend(self): self.assertFalse(active_hours(self.cfg, parse_time("2026-10-17T10:00:00+09:00")))
    def test_business_hours(self): self.assertTrue(active_hours(self.cfg, parse_time("2026-10-16T10:00:00+09:00")))
    def test_overnight_weekday(self):
        self.cfg.active_start="22:00"; self.cfg.active_end="02:00"
        self.assertTrue(active_hours(self.cfg, parse_time("2026-10-17T01:00:00+09:00")))
    def test_no_change_no_inference(self):
        self.cfg.heartbeat_enabled=True; self.cfg.bedrock.enabled=True
        agent,client=self.agent([])
        Scheduler(self.store,self.cfg,agent).tick(parse_time("2026-10-16T10:00:00+09:00"))
        self.assertFalse(client.calls)
    def test_notification_dedupe(self):
        _,did=self.add()
        self.assertTrue(self.store.notify("same","確認","本文",did,daily_limit=5,cooldown_minutes=1))
        self.assertFalse(self.store.notify("same","確認","本文",did,daily_limit=5,cooldown_minutes=1))
    def test_notification_cooldown(self):
        _,did=self.add()
        self.store.notify("one","確認","本文",did,daily_limit=5,cooldown_minutes=60)
        self.assertFalse(self.store.notify("two","確認","本文",did,daily_limit=5,cooldown_minutes=60))
    def test_persistent_claim(self):
        agent,_=self.agent([]); scheduler=Scheduler(self.store,self.cfg,agent)
        self.assertTrue(scheduler._claim("event",now_utc()))
        self.assertFalse(Scheduler(self.store,self.cfg,agent)._claim("event",now_utc()))
    def test_foreground_yields_monitor(self):
        self.cfg.heartbeat_enabled=True;self.cfg.bedrock.enabled=True;agent,client=self.agent([])
        event=threading.Event();event.set()
        self.assertEqual(Scheduler(self.store,self.cfg,agent).tick(cancel=event)["status"],"skipped")
        self.assertFalse(client.calls)


if __name__ == "__main__": unittest.main()
