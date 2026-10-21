from __future__ import annotations

import copy
import io
import json
import os
import struct
import tempfile
import unittest
import urllib.error
import zlib
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch

from work_assistant.bedrock import BedrockClient, response_text
from work_assistant.common import AssistantError, canonical
from work_assistant.config import BedrockSettings, Settings
from work_assistant.converse import normalize, request_payload, read_stream
from work_assistant.model_catalog import list_models
from work_assistant.security import validate_bedrock
from work_assistant.setup_flow import probe_connection
from work_assistant.store import Store
from work_assistant.agent import Agent
from work_assistant.execution import WorkRunner
from work_assistant.common import now_utc, stamp
from test_core import Fixture

CLAUDE = "anthropic.claude-sonnet-4-6"
PROFILE = "apac." + CLAUDE
ROOT = "https://bedrock-runtime.ap-northeast-1.amazonaws.com"


def cfg(**kwargs):
    values = dict(enabled=True, data_transfer_approved=True, endpoint=ROOT, model=CLAUDE, auth="api_key", streaming=False)
    values.update(kwargs)
    return BedrockSettings(**values)


def response(content=None, stop="end_turn"):
    return {"output": {"message": {"role": "assistant", "content": content or [{"text": "日本語の応答"}]}},
            "stopReason": stop, "usage": {"inputTokens": 20, "outputTokens": 5, "totalTokens": 25}}


def frame(event, data, message_type="event"):
    headers = bytearray()
    for name, value in ((":message-type", message_type), (":event-type", event), (":content-type", "application/json")):
        n, v = name.encode(), value.encode()
        headers.extend(bytes([len(n)]) + n + b"\x07" + struct.pack(">H", len(v)) + v)
    payload = canonical(data).encode()
    prelude = struct.pack(">II", 16 + len(headers) + len(payload), len(headers))
    body = prelude + struct.pack(">I", zlib.crc32(prelude)) + headers + payload
    return bytes(body) + struct.pack(">I", zlib.crc32(body))


def stream_events(tool=False):
    values = [("messageStart", {"role": "assistant"})]
    if tool:
        values += [("contentBlockStart", {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": "tool-1", "name": "echo_probe"}}}),
                   ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '{"nonce":'}}}),
                   ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '"日本語"}'}}})]
    else:
        values += [("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "日本"}}),
                   ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "語"}})]
    return values + [("contentBlockStop", {"contentBlockIndex": 0}),
                     ("messageStop", {"stopReason": "tool_use" if tool else "end_turn"}),
                     ("metadata", {"usage": {"totalTokens": 25}})]


class Wire(io.BytesIO):
    def __init__(self, data, content_type="application/json"):
        super().__init__(data if isinstance(data, bytes) else canonical(data).encode())
        self.headers = {"Content-Type": content_type}


class Opener:
    def __init__(self, fn): self.fn, self.requests = fn, []
    def open(self, request, timeout):
        self.requests.append(request)
        return self.fn(request)


class ConverseTests(unittest.TestCase):
    def test_regional_claude_and_geographic_profiles_enforce_policy(self):
        self.assertEqual(validate_bedrock(cfg()), (ROOT, "bedrock"))
        for prefix in ("global.", "apac.", "jp."):
            with self.assertRaises(AssistantError) as error:
                validate_bedrock(cfg(model=prefix + CLAUDE))
            self.assertEqual(error.exception.code, "CROSS_REGION_NOT_APPROVED")
            validate_bedrock(cfg(model=prefix + CLAUDE, cross_region_approved=True))
        with self.assertRaisesRegex(AssistantError, "PROFILE_REGION_MISMATCH"):
            validate_bedrock(cfg(model="eu." + CLAUDE), live=False)
        for model in ("anthropic.claude-v2", "amazon.nova-pro-v1:0", CLAUDE + "/../../evil"):
            with self.assertRaises(AssistantError): validate_bedrock(cfg(model=model), live=False)
        for endpoint in (ROOT + ".evil.invalid", ROOT + "/other", ROOT + "?token=bad", "http://" + urlsplit(ROOT).netloc,
                         "https://bedrock-mantle.ap-northeast-1.api.aws"):
            with self.assertRaises(AssistantError): validate_bedrock(cfg(endpoint=endpoint), live=False)

    def test_payload_groups_text_tools_results_and_preserves_signed_reasoning(self):
        thought = {"reasoningContent": {"reasoningText": {"text": "thought", "signature": "signed"}}}
        items = [{"role": "user", "content": "依頼"}, {"role": "user", "content": "資料"},
                 {"type": "reasoning", "encrypted_content": "openai-opaque-must-not-forward"},
                 {"type": "reasoning", "bedrock_content": thought},
                 {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "確認します"}]},
                 {"type": "function_call", "call_id": "t1", "name": "read_document", "arguments": '{"id":"文書"}'},
                 {"type": "function_call", "call_id": "t2", "name": "read_document", "arguments": '{"id":"別文書"}'},
                 {"type": "function_call_output", "call_id": "t1", "output": '{"text":"本文"}'},
                 {"type": "function_call_output", "call_id": "t2", "output": '{"error":"missing"}'},
                 {"role": "user", "content": "再開イベント"}]
        before = copy.deepcopy(items)
        result = request_payload(items, "制約", [], 3000)
        self.assertEqual(items, before)
        self.assertEqual([m["role"] for m in result["messages"]], ["user", "assistant", "user"])
        self.assertEqual(result["messages"][1]["content"][0], thought)
        self.assertEqual(result["messages"][1]["content"][2]["toolUse"]["input"], {"id": "文書"})
        self.assertEqual(result["messages"][2]["content"][1]["toolResult"]["toolUseId"], "t2")
        self.assertNotIn("openai-opaque", canonical(result)); self.assertNotIn("toolConfig", result)
        self.assertNotIn("reasoning", result); self.assertNotIn("store", result)

    def test_normalize_rejects_incomplete_blocked_or_malformed_tool_responses(self):
        call = {"toolUse": {"toolUseId": "a", "name": "echo", "input": {}}}
        good = normalize(response([call], "tool_use"))
        self.assertEqual(good["output"][0]["call_id"], "a")
        for stop in ("max_tokens", "malformed_tool_use", "model_context_window_exceeded", "guardrail_intervened", "content_filtered"):
            with self.assertRaises(AssistantError): normalize(response([call], stop))
        for item in (response([call], "end_turn"), response([call, call], "tool_use"),
                     response(stop="tool_use"), {"stopReason": "end_turn", "output": None}):
            with self.assertRaises(AssistantError): normalize(item)

    def test_claude_probe_roundtrip_uses_real_adapter_and_omits_responses_parameters(self):
        calls = []
        def transport(request):
            payload = json.loads(request.data); calls.append(payload)
            if len(calls) == 1:
                nonce = payload["messages"][0]["content"][0]["text"].split("nonce=", 1)[1].split(".")[0]
                return Wire(response([{"toolUse": {"toolUseId": "probe", "name": "echo_probe", "input": {"nonce": nonce}}}], "tool_use"))
            result = payload["messages"][-1]["content"][0]["toolResult"]
            self.assertEqual(result["toolUseId"], "probe")
            return Wire(response([{"text": json.loads(result["content"][0]["text"])["nonce"]}]))
        opener = Opener(transport); settings = Settings(local_data_approved=True, bedrock=cfg())
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {settings.bedrock.api_key_env: "synthetic-key"}):
            store = Store(Path(tmp) / "state.sqlite3")
            result = probe_connection(settings, store, client=BedrockClient(settings.bedrock, opener))
            with store.connection() as cx: self.assertEqual(tuple(cx.execute("SELECT calls,tokens FROM budget").fetchone()), (2, 50))
        self.assertFalse(result["store"])
        self.assertEqual(opener.requests[0].full_url, ROOT + "/model/" + CLAUDE + "/converse")
        self.assertEqual(calls[0]["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["type"], "object")
        self.assertFalse({"model", "store", "reasoning", "include", "stream", "parallel_tool_calls"} & calls[0].keys())

    def test_stream_text_tool_json_signed_reasoning_and_fragmented_reads(self):
        class Fragmented(io.BytesIO):
            def read(self, count=-1): return super().read(min(count, 3))
        deltas = []
        raw = b"".join(frame(*e) for e in stream_events())
        self.assertEqual(response_text(read_stream(Fragmented(raw), deltas.append)), "日本語")
        self.assertEqual(deltas, ["日本", "語"])
        tool = read_stream(io.BytesIO(b"".join(frame(*e) for e in stream_events(True))))
        self.assertEqual(json.loads(tool["output"][0]["arguments"]), {"nonce": "日本語"})
        values = stream_events()
        values[1:1] = [("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"reasoningContent": {"text": "考慮"}}}),
                       ("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"reasoningContent": {"signature": "signed"}}}),
                       ("contentBlockStop", {"contentBlockIndex": 1})]
        result = read_stream(io.BytesIO(b"".join(frame(*e) for e in values)))
        self.assertEqual(result["output"][-1]["bedrock_content"]["reasoningContent"]["reasoningText"]["signature"], "signed")

    def test_stream_fails_closed_on_truncation_crc_exceptions_and_missing_completion(self):
        events = stream_events(True); raw = b"".join(frame(*e) for e in events)
        bad_crc = bytearray(raw); bad_crc[30] ^= 1
        fixtures = [raw[:-1], raw[:11], bytes(bad_crc), b"".join(frame(*e) for e in events[:-1]),
                    frame("validationException", {"message": "secret-provider-echo"}, "exception"),
                    b"".join(frame(*e) for e in events[:3] + events[-2:]),
                    struct.pack(">III", 9_000_000, 0, 0)]
        for data in fixtures:
            with self.subTest(size=len(data)), self.assertRaises(AssistantError) as caught: read_stream(io.BytesIO(data))
            self.assertNotIn("secret-provider-echo", str(caught.exception))

    def test_stream_transport_and_no_automatic_retry_or_error_body_exposure(self):
        opener = Opener(lambda _: Wire(b"".join(frame(*e) for e in stream_events()), "application/vnd.amazon.eventstream"))
        with patch.dict(os.environ, {"AWS_BEARER_TOKEN_BEDROCK": "synthetic-key"}):
            self.assertEqual(response_text(BedrockClient(cfg(streaming=True), opener).create([{"role": "user", "content": "質問"}], "指示", [])), "日本語")
        self.assertTrue(opener.requests[0].full_url.endswith("/converse-stream"))
        def denied(req): raise urllib.error.HTTPError(req.full_url, 403, "denied", {}, io.BytesIO(b"secret-provider-echo"))
        opener = Opener(denied)
        with patch.dict(os.environ, {"AWS_BEARER_TOKEN_BEDROCK": "synthetic-key"}), self.assertRaises(AssistantError) as error:
            BedrockClient(cfg(), opener).create([], "指示", [])
        self.assertEqual(error.exception.code, "BEDROCK_ACCESS_DENIED")
        self.assertEqual(len(opener.requests), 1); self.assertNotIn("secret-provider-echo", str(error.exception))


def foundation(mid=CLAUDE, **kwargs):
    data = {"modelId": mid, "modelName": "Claude Sonnet", "modelLifecycle": {"status": "ACTIVE"}, "outputModalities": ["TEXT"], "inferenceTypesSupported": ["ON_DEMAND"], "responseStreamingSupported": True}
    return dict(data, **kwargs)


def profile(mid=PROFILE, **kwargs):
    data = {"inferenceProfileId": mid, "inferenceProfileName": "APAC Claude Sonnet", "status": "ACTIVE", "models": [{"modelArn": "arn:aws:bedrock:ap-northeast-1::foundation-model/" + CLAUDE}, {"modelArn": "arn:aws:bedrock:ap-southeast-2::foundation-model/" + CLAUDE}]}
    return dict(data, **kwargs)


def available(**kwargs):
    return dict({"authorizationStatus": "AUTHORIZED", "entitlementAvailability": "AVAILABLE", "regionAvailability": "AVAILABLE", "agreementAvailability": {"status": "AVAILABLE"}}, **kwargs)


class CatalogTests(unittest.TestCase):
    def catalog(self, fn):
        opener = Opener(lambda req: Wire(fn(urlsplit(req.full_url))))
        with patch.dict(os.environ, {"AWS_BEARER_TOKEN_BEDROCK": "synthetic-key"}):
            result = list_models(cfg(), client=BedrockClient(cfg(), opener))
        self.assertTrue(all(req.get_method() == "GET" and req.data is None for req in opener.requests))
        self.assertTrue(all(urlsplit(req.full_url).netloc == "bedrock.ap-northeast-1.amazonaws.com" for req in opener.requests))
        self.assertTrue(all(req.get_header("Authorization") == "Bearer synthetic-key" for req in opener.requests))
        return result, opener.requests

    def test_catalog_paginates_filters_and_checks_account_availability(self):
        def fn(url):
            if url.path == "/foundation-models":
                return {"modelSummaries": [foundation(), foundation("amazon.nova-pro-v1:0"), foundation("anthropic.claude-3-haiku-v1:0", modelLifecycle={"status": "LEGACY"})]}
            if url.path == "/inference-profiles":
                if "nextToken" not in parse_qs(url.query): return {"inferenceProfileSummaries": [profile()], "nextToken": "page+2/="}
                self.assertEqual(parse_qs(url.query)["nextToken"], ["page+2/="])
                return {"inferenceProfileSummaries": [profile("global.openai.gpt-6-luna", inferenceProfileName="OpenAI Luna", models=[{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-6-luna"}])]}
            return available()
        result, requests = self.catalog(fn)
        self.assertEqual({c.model for c in result.choices}, {CLAUDE, PROFILE, "global.openai.gpt-6-luna"})
        self.assertTrue(all(c.availability == "available" for c in result.choices))
        self.assertFalse(result.warnings)
        self.assertEqual(len([r for r in requests if "/foundation-model-availability/" in r.full_url]), 2)
        self.assertEqual(next(c for c in result.choices if c.model == PROFILE).regions, ("ap-northeast-1", "ap-southeast-2"))

    def test_known_unavailable_hidden_and_optional_permission_denial_stays_unknown(self):
        def fn(url):
            if url.path == "/foundation-models": return {"modelSummaries": [foundation()]}
            if url.path == "/inference-profiles": return {"inferenceProfileSummaries": [profile()]}
            return available(authorizationStatus="NOT_AUTHORIZED")
        result, _ = self.catalog(fn)
        self.assertFalse(result.choices); self.assertEqual(result.excluded, 2)
        def unknown(url):
            if "availability" in url.path: raise AssistantError("BEDROCK_ACCESS_DENIED")
            return fn(url)
        result, _ = self.catalog(unknown)
        self.assertEqual(len(result.choices), 2); self.assertTrue(result.warnings)
        self.assertTrue(all(c.availability == "unknown" for c in result.choices))

    def test_source_region_absence_does_not_remove_cross_region_profile(self):
        def fn(url):
            if url.path == "/foundation-models": return {"modelSummaries": [foundation()]}
            if url.path == "/inference-profiles": return {"inferenceProfileSummaries": [profile()]}
            return available(regionAvailability="NOT_AVAILABLE")
        result, _ = self.catalog(fn)
        self.assertEqual([c.model for c in result.choices], [PROFILE])
        self.assertEqual(result.choices[0].availability, "unknown")

    def test_partial_failure_is_distinct_from_empty_catalog_and_total_failure(self):
        def fn(url):
            if url.path == "/foundation-models": raise AssistantError("BEDROCK_ACCESS_DENIED")
            if url.path == "/inference-profiles": return {"inferenceProfileSummaries": [profile()]}
            return available()
        result, _ = self.catalog(fn)
        self.assertEqual(len(result.choices), 1); self.assertTrue(result.warnings)
        with self.assertRaises(AssistantError) as error:
            self.catalog(lambda _: (_ for _ in ()).throw(AssistantError("BEDROCK_ACCESS_DENIED")))
        self.assertEqual(error.exception.code, "BEDROCK_ACCESS_DENIED")
        result, _ = self.catalog(lambda u: {"modelSummaries": []} if u.path == "/foundation-models" else {"inferenceProfileSummaries": []})
        self.assertFalse(result.choices); self.assertFalse(result.warnings)

    def test_duplicate_pagination_token_and_bad_region_are_bounded(self):
        def fn(url):
            if url.path == "/foundation-models": return {"modelSummaries": []}
            if url.path == "/inference-profiles": return {"inferenceProfileSummaries": [profile()], "nextToken": "repeat"}
            return available()
        result, requests = self.catalog(fn)
        self.assertEqual(len(result.choices), 1); self.assertTrue(result.warnings)
        self.assertEqual(len([r for r in requests if urlsplit(r.full_url).path == "/inference-profiles"]), 2)
        with self.assertRaisesRegex(AssistantError, "BEDROCK_REGION_INVALID"):
            list_models(cfg(region="ap-northeast-1.evil.invalid"))


class ClaudeAgentTests(Fixture):
    def test_real_converse_adapter_reads_document_and_persists_cited_answer(self):
        _, did = self.add(); self.cfg.bedrock = cfg()
        payloads = []
        def transport(req):
            payloads.append(json.loads(req.data))
            if len(payloads) == 1:
                return Wire(response([{"toolUse": {"toolUseId": "read", "name": "read_document", "input": {"document_id": did, "offset": 0, "length": 1000}}}], "tool_use"))
            self.assertIn(did, canonical(payloads[-1]["messages"][-1]["content"][0]["toolResult"]))
            return Wire(response([{"text": "塗工幅を確認しました [資料:" + did + "]"}]))
        agent = Agent(self.store, self.cfg, self.ingest, self.bridge, BedrockClient(self.cfg.bedrock, Opener(transport)))
        with patch.dict(os.environ, {"AWS_BEARER_TOKEN_BEDROCK": "synthetic-key"}):
            result = agent.turn("資料の塗工幅を確認して")
        self.assertEqual(result.citations[0]["id"], did)
        self.assertEqual(result.trace[0]["status"], "ok")
        self.assertEqual(self.store.messages("main")[-1]["text"], result.text)

    def test_waiting_claude_job_survives_new_runner_and_restores_tool_pair(self):
        self.cfg.bedrock = cfg(); at = now_utc() + timedelta(seconds=30)
        first = Opener(lambda _: Wire(response([{"toolUse": {"toolUseId": "wait", "name": "wait_until", "input": {"at": stamp(at)}}}], "tool_use")))
        agent = Agent(self.store, self.cfg, self.ingest, self.bridge, BedrockClient(self.cfg.bedrock, first))
        queue = agent.state; jid = queue.submit("指定時刻に確認")
        with patch.dict(os.environ, {"AWS_BEARER_TOKEN_BEDROCK": "synthetic-key"}):
            runner = WorkRunner(queue, self.cfg, agent)
            self.assertEqual(runner.run_one()["status"], "waiting")
            self.assertEqual(runner.run_one()["status"], "idle")
            self.assertEqual(len(first.requests), 1)
            def transport(req):
                messages = json.loads(req.data)["messages"]
                self.assertTrue(any("toolUse" in b and b["toolUse"]["toolUseId"] == "wait" for m in messages for b in m["content"]))
                self.assertTrue(any("toolResult" in b and b["toolResult"]["toolUseId"] == "wait" for m in messages for b in m["content"]))
                self.assertIn("RESUME_EVENT_REFERENCE", canonical(messages))
                return Wire(response([{"text": "指定時刻後に確認しました"}]))
            restored = Agent(Store(self.store.path), self.cfg, self.ingest, self.bridge, BedrockClient(self.cfg.bedrock, Opener(transport)))
            with patch("work_assistant.state.now_utc", return_value=at + timedelta(seconds=1)):
                self.assertEqual(WorkRunner(restored.state, self.cfg, restored).run_one()["status"], "completed")
        self.assertEqual(queue.job(jid)["result"]["text"], "指定時刻後に確認しました")


if __name__ == "__main__": unittest.main()
