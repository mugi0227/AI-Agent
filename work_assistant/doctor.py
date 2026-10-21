"""Evidence-based diagnostics. Live tests use synthetic input only and are opt-in."""
from __future__ import annotations

import importlib.util
import os
import platform
import sqlite3
import sys
from pathlib import Path

from .common import AssistantError, atomic_json, safe_code, stamp
from .config import load
from .security import expanded, validate_bedrock
from .store import Store


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
            from .setup_flow import probe_connection
            if store is None: raise AssistantError("DB_UNAVAILABLE")
            record("bedrock_tool_roundtrip", "PASS", probe_connection(cfg, store))
        except Exception as error:
            record("bedrock_tool_roundtrip", "FAIL", safe_code(error))
    for name in ("native_host_browser", "power_automate_standard", "box_sso_dlp", "other_organizer_transcript_read", "other_organizer_transcript_trigger", "sharepoint_search", "sync_latency"):
        record(name, "NOT_TESTED", "Follow docs/COMPANY_CODEX_PROMPT.md; no tenant result is assumed")
    report = {"schema_version": 1, "at": stamp(), "checks": checks}
    atomic_json(home / "diagnostics" / "doctor.json", report)
    return report
