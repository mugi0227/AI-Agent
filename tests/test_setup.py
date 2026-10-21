from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from work_assistant.common import AssistantError
from work_assistant.config import Settings, initialize, load
from work_assistant.runtime import Runtime
from work_assistant.setup_flow import SetupError, connection_fingerprint, probe_connection


class EchoProbeClient:
    def __init__(self):
        self.calls = []

    def create(self, items, instructions, tools):
        self.calls.append(copy.deepcopy((items, instructions, tools)))
        nonce = items[0]["content"].split("nonce=", 1)[1].split(".", 1)[0]
        if len(self.calls) == 1:
            output = [{"type": "function_call", "call_id": "probe", "name": "echo_probe", "arguments": json.dumps({"nonce": nonce})}]
        else:
            output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": nonce}]}]
        return {"status": "completed", "output": output, "usage": {"total_tokens": 50}}


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "state"; initialize(self.home)
        self.root = Path(self.tmp.name) / "documents"; self.root.mkdir()
        self.rt = Runtime(self.home)

    def tearDown(self):
        self.rt.close(); self.tmp.cleanup()

    def candidate(self):
        cfg = copy.deepcopy(self.rt.cfg)
        cfg.local_data_approved = True; cfg.roots = [str(self.root)]
        cfg.bedrock.enabled = cfg.bedrock.data_transfer_approved = cfg.bedrock.cross_region_approved = True
        return cfg

    def test_save_applies_without_restart_and_preserves_shared_references_and_details(self):
        self.rt.cfg.retention_days = 47
        self.rt.cfg.drop_folders = ["preserved-drop-folder"]
        self.rt.cfg.bedrock.reasoning_effort = "high"
        current, bedrock = self.rt.cfg, self.rt.cfg.bedrock
        candidate = self.candidate()
        self.rt.configure(candidate)
        self.assertIs(self.rt.cfg, current)
        self.assertIs(self.rt.agent.cfg, current)
        self.assertIs(self.rt.scheduler.cfg, current)
        self.assertIs(self.rt.ingestor.cfg, current)
        self.assertIs(self.rt.bridge.cfg, current)
        self.assertIs(self.rt.work_runner.cfg, current)
        self.assertIs(self.rt.agent.client.settings, bedrock)
        self.assertEqual(asdict(load(self.home)), asdict(candidate))
        self.assertEqual(current.retention_days, 47)
        self.assertEqual(current.drop_folders, ["preserved-drop-folder"])
        self.assertEqual(bedrock.reasoning_effort, "high")
        candidate.roots.clear()
        self.assertEqual(current.roots, [str(self.root)])

    def test_failed_atomic_save_preserves_disk_and_runtime_and_can_retry(self):
        before = (self.home / "config.json").read_bytes()
        snapshot = asdict(self.rt.cfg)
        with patch("work_assistant.runtime.atomic_json", side_effect=OSError("write failed")):
            with self.assertRaises(OSError): self.rt.configure(self.candidate())
        self.assertEqual((self.home / "config.json").read_bytes(), before)
        self.assertEqual(asdict(self.rt.cfg), snapshot)
        self.rt.configure(self.candidate())
        self.assertTrue(self.rt.cfg.local_data_approved)

    def test_active_operation_blocks_configuration_without_blocking_parallel_work(self):
        with self.rt.operation():
            with self.rt.operation():
                with self.assertRaisesRegex(AssistantError, "SETTINGS_BUSY"):
                    self.rt.configure(self.candidate())
            self.assertFalse(self.rt.cfg.local_data_approved)
        self.rt.configure(self.candidate())

    def test_configuration_commit_blocks_new_operations_until_all_fields_are_applied(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        from work_assistant.common import atomic_json
        def slow_write(path, value):
            entered.set(); release.wait(5); atomic_json(path, value)
        def save():
            try: self.rt.configure(self.candidate())
            except Exception as error: errors.append(error)
        with patch("work_assistant.runtime.atomic_json", side_effect=slow_write):
            thread = threading.Thread(target=save); thread.start()
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(AssistantError, "SETTINGS_BUSY"):
                    with self.rt.operation(): pass
                self.assertFalse(self.rt.cfg.local_data_approved)
            finally: release.set(); thread.join(5)
        self.assertFalse(errors)
        self.assertTrue(self.rt.cfg.local_data_approved)

    def test_invalid_folders_and_endpoints_leave_original_configuration(self):
        for path in (self.root / "missing", self.home, self.root / ".aws"):
            if path.name == ".aws": path.mkdir()
            cfg = self.candidate(); cfg.roots = [str(path)]
            with self.assertRaises(SetupError): self.rt.configure(cfg)
        cfg = self.candidate(); cfg.bedrock.endpoint = "https://example.com/openai/v1"
        with self.assertRaises(SetupError): self.rt.configure(cfg)
        self.assertFalse(self.rt.cfg.local_data_approved)
        self.assertFalse(load(self.home).local_data_approved)

    def test_connection_probe_sends_only_synthetic_input_and_charges_both_calls(self):
        self.rt.store.remember_turn("main", "user", "private-conversation-canary")
        cfg = self.candidate(); client = EchoProbeClient()
        result = probe_connection(cfg, self.rt.store, client=client)
        self.assertFalse(result["store"])
        self.assertEqual(len(client.calls), 2)
        request = json.dumps(client.calls)
        self.assertNotIn("private-conversation-canary", request)
        self.assertNotIn(str(self.root), request)
        self.assertEqual(client.calls[0][2][0]["name"], "echo_probe")
        with self.rt.store.connection() as cx:
            row = cx.execute("SELECT calls,tokens FROM budget").fetchone()
        self.assertEqual(tuple(row), (2, 100))

    def test_probe_requires_egress_and_region_permissions_before_any_call(self):
        for field in ("enabled", "data_transfer_approved", "cross_region_approved"):
            cfg = self.candidate(); setattr(cfg.bedrock, field, False)
            client = EchoProbeClient()
            with self.assertRaises(AssistantError): probe_connection(cfg, self.rt.store, client=client)
            self.assertEqual(client.calls, [])

    def test_probe_rejects_unsupported_tool_call_and_respects_daily_limit(self):
        client = EchoProbeClient()
        with patch.object(client, "create", return_value={"output": [], "usage": {"total_tokens": 1}}):
            with self.assertRaisesRegex(AssistantError, "PROBE_TOOL_NOT_CALLED"):
                probe_connection(self.candidate(), self.rt.store, client=client)
        cfg = self.candidate(); cfg.bedrock.daily_calls = 1
        client = EchoProbeClient()
        with self.assertRaisesRegex(AssistantError, "DAILY_BUDGET_EXCEEDED"):
            probe_connection(cfg, self.rt.store, client=client)
        self.assertEqual(client.calls, [])

    def test_connection_evidence_changes_when_auth_model_endpoint_or_permissions_change(self):
        original = self.candidate(); original_hash = connection_fingerprint(original)
        for field, value in (("model", "global.openai.other"), ("profile", "another-profile"), ("data_transfer_approved", False), ("endpoint", "https://bedrock-mantle.us-east-1.api.aws/openai/v1")):
            cfg = copy.deepcopy(original); setattr(cfg.bedrock, field, value)
            self.assertNotEqual(original_hash, connection_fingerprint(cfg))


if __name__ == "__main__": unittest.main()
