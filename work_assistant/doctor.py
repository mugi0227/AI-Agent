"""Evidence-based diagnostics. Live tests use synthetic input only and are opt-in."""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from .bedrock import BedrockClient, response_text
from .common import AssistantError, atomic_json, canonical, safe_code, stamp
from .config import load
from .security import expanded, validate_bedrock
from .store import Store
from .tools import function, string


def diagnose(home: Path, live: bool = False) -> dict:
    cfg = load(home)
    checks = []
    def record(name, status, evidence):
        checks.append({"check": name, "status": status, "evidence": evidence})
    record("python", "PASS" if sys.version_info >= (3, 11) else "FAIL", platform.python_version())
    record("platform", "PASS" if os.name == "nt" else "NOT_TESTED", "Windows native integration requires company PC" if os.name != "nt" else "Windows")
    record("tk_module", "PASS" if importlib.util.find_spec("tkinter") else "FAIL", "Module discovery only; GUI launch is a separate manual test")
    try:
        store = Store(home / "assistant.sqlite3")
        record("sqlite_fts5", "PASS", sqlite3.sqlite_version)
    except Exception as error:
        record("sqlite_fts5", "FAIL", safe_code(error))
        store = None
    record("local_data_policy", "PASS" if cfg.local_data_approved else "BLOCKED_POLICY", "Explicit configuration flag; not evidence of organizational approval")
    for key, paths in (("roots", cfg.roots), ("drop_folders", cfg.drop_folders)):
        for index, path in enumerate(paths):
            record(f"{key}_{index}", "PASS" if expanded(path).is_dir() else "FAIL", "Exists as directory only; does not prove read, hydration, or cloud access")
    try:
        validate_bedrock(cfg.bedrock)
        record("bedrock_config", "PASS", "Approved endpoint syntax and explicit feature flags only")
    except AssistantError as error:
        record("bedrock_config", "BLOCKED_POLICY" if "APPROVED" in error.code or "DISABLED" in error.code else "FAIL", error.code)
    if not live:
        record("bedrock_tool_roundtrip", "NOT_TESTED", "Run doctor --live only after approvals; uses synthetic nonce and up to two billable calls")
    else:
        try:
            client = BedrockClient(cfg.bedrock)
            nonce = "probe-" + uuid.uuid4().hex[:12]
            schema = function("echo_probe", "Echo the exact nonce to validate function calling", {"nonce": string(100)})
            items = [{"role": "user", "content": "Call echo_probe with nonce=" + nonce + ". After receiving its result, reply only with that nonce."}]
            started = time.monotonic()
            if store is None: raise AssistantError("DB_UNAVAILABLE")
            reservation = 10000 + cfg.bedrock.max_output_tokens
            day = store.reserve_budget(reservation, cfg.bedrock.daily_calls, cfg.bedrock.daily_tokens)
            first = client.create(items, "This is a synthetic connectivity test, not company data.", [schema])
            store.settle_budget(day, reservation, first.get("usage", {}).get("total_tokens", reservation))
            calls = [x for x in first["output"] if x.get("type") == "function_call"]
            if len(calls) != 1 or calls[0]["name"] != "echo_probe" or json.loads(calls[0]["arguments"]) != {"nonce": nonce}:
                raise AssistantError("PROBE_TOOL_NOT_CALLED")
            call = calls[0]
            items += first["output"] + [{"type": "function_call_output", "call_id": call["call_id"], "output": canonical({"nonce": nonce})}]
            day = store.reserve_budget(reservation, cfg.bedrock.daily_calls, cfg.bedrock.daily_tokens)
            second = client.create(items, "Return the nonce from the tool result.", [schema])
            store.settle_budget(day, reservation, second.get("usage", {}).get("total_tokens", reservation))
            if nonce not in response_text(second) or any(x.get("type") == "function_call" for x in second["output"]):
                raise AssistantError("PROBE_TOOL_RESULT_NOT_USED")
            record("bedrock_tool_roundtrip", "PASS", {"elapsed_seconds": round(time.monotonic() - started, 2), "reasoning_items_preserved": sum(x.get("type") == "reasoning" for x in first["output"]), "store": False})
        except Exception as error:
            record("bedrock_tool_roundtrip", "FAIL", safe_code(error))
    for name in ("native_host_browser", "power_automate_standard", "box_sso_dlp", "other_organizer_transcript_read", "other_organizer_transcript_trigger", "sharepoint_search", "sync_latency"):
        record(name, "NOT_TESTED", "Follow docs/COMPANY_CODEX_PROMPT.md; no tenant result is assumed")
    report = {"schema_version": 1, "at": stamp(), "checks": checks}
    atomic_json(home / "diagnostics" / "doctor.json", report)
    return report
