"""Explicit local settings. Defaults never enable inference or scan user folders."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .common import AssistantError, atomic_json


@dataclass
class BedrockSettings:
    enabled: bool = False
    data_transfer_approved: bool = False
    cross_region_approved: bool = False
    endpoint: str = "https://bedrock-runtime.ap-northeast-1.amazonaws.com/openai/v1"
    region: str = "ap-northeast-1"
    model: str = "global.openai.gpt-6-luna"
    auth: str = "sigv4"
    profile: str = ""
    credential_ref: str = ""
    api_key_env: str = "AWS_BEARER_TOKEN_BEDROCK"
    reasoning_effort: str = "medium"
    encrypted_reasoning: bool = True
    streaming: bool = True
    timeout_seconds: int = 40
    max_output_tokens: int = 3000
    ca_bundle: str = ""
    proxy_url: str = ""
    daily_calls: int = 100
    daily_tokens: int = 200000


@dataclass
class Settings:
    schema_version: int = 1
    local_data_approved: bool = False
    roots: list[str] = field(default_factory=list)
    drop_folders: list[str] = field(default_factory=list)
    outbox: str = ""
    bridge_enabled: bool = False
    allowed_web_hosts: list[str] = field(default_factory=list)
    extension_ids: list[str] = field(default_factory=list)
    retention_days: int = 30
    cloud_lease_hours: int = 24
    max_file_bytes: int = 10_000_000
    max_document_chars: int = 180000
    scan_interval_seconds: int = 60
    hydrate_cloud_files: bool = False
    heartbeat_enabled: bool = False
    heartbeat_interval_minutes: int = 30
    active_start: str = "08:00"
    active_end: str = "19:00"
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    briefing_times: list[str] = field(default_factory=lambda: ["08:00", "18:00"])
    meeting_lead_minutes: int = 30
    notification_daily_limit: int = 5
    notification_cooldown_minutes: int = 60
    max_model_rounds: int = 6
    max_tool_calls: int = 12
    max_job_model_rounds: int = 40
    max_job_tool_calls: int = 100
    max_context_chars: int = 50000
    background_workers: int = 2
    durable_work_enabled: bool = False
    memory_embedding_model: str = ""
    max_memory_items: int = 1000
    compaction_trigger_messages: int = 24
    compaction_min_chars: int = 12000
    bedrock: BedrockSettings = field(default_factory=BedrockSettings)

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise AssistantError("CONFIG_VERSION")
        ranges = {
            "retention_days": (1, 365), "cloud_lease_hours": (1, 168),
            "max_file_bytes": (1024, 50_000_000), "max_document_chars": (1000, 500000),
            "scan_interval_seconds": (5, 3600), "heartbeat_interval_minutes": (5, 1440),
            "meeting_lead_minutes": (5, 120), "notification_daily_limit": (1, 50),
            "notification_cooldown_minutes": (1, 1440), "max_model_rounds": (1, 12),
            "max_tool_calls": (1, 30), "max_context_chars": (4000, 200000),
            "background_workers": (1, 4), "compaction_trigger_messages": (16, 200), "compaction_min_chars": (2000, 100000),
            "max_job_model_rounds": (6, 200), "max_job_tool_calls": (12, 500),
            "max_memory_items": (100, 10000),
        }
        for key, (low, high) in ranges.items():
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise AssistantError("CONFIG_RANGE", f"{key}が範囲外です")
        for name in ("local_data_approved", "bridge_enabled", "hydrate_cloud_files", "heartbeat_enabled", "durable_work_enabled"):
            if type(getattr(self, name)) is not bool:
                raise AssistantError("CONFIG_BOOLEAN")
        for name in ("roots", "drop_folders", "allowed_web_hosts", "extension_ids", "briefing_times"):
            items = getattr(self, name)
            if not isinstance(items, list) or len(items) > 100 or any(not isinstance(x, str) for x in items):
                raise AssistantError("CONFIG_LIST")
        if not isinstance(self.weekdays, list) or any(type(v) is not int or v not in range(7) for v in self.weekdays):
            raise AssistantError("CONFIG_WEEKDAY")
        for value in [self.active_start, self.active_end, *self.briefing_times]:
            minute_of_day(value)
        if self.active_start == self.active_end:
            raise AssistantError("EMPTY_ACTIVE_HOURS")
        for host in self.allowed_web_hosts:
            if not host or "/" in host or "*" in host or ":" in host or host != host.lower():
                raise AssistantError("CONFIG_HOST", "ホストは小文字の完全一致で指定します")
        if any(len(x) != 32 or any(c not in "abcdefghijklmnop" for c in x) for x in self.extension_ids):
            raise AssistantError("CONFIG_EXTENSION_ID")
        if type(self.outbox) is not str:
            raise AssistantError("CONFIG_OUTBOX")
        if not isinstance(self.memory_embedding_model, str) or len(self.memory_embedding_model) > 4000:
            raise AssistantError("CONFIG_STRING")
        b = self.bedrock
        for name in ("endpoint", "region", "model", "auth", "profile", "credential_ref", "api_key_env", "reasoning_effort", "ca_bundle", "proxy_url"):
            if not isinstance(getattr(b, name), str) or len(getattr(b, name)) > 4000:
                raise AssistantError("CONFIG_STRING")
        for name in ("enabled", "data_transfer_approved", "cross_region_approved", "encrypted_reasoning", "streaming"):
            if type(getattr(b, name)) is not bool:
                raise AssistantError("CONFIG_BOOLEAN")
        if b.auth not in ("sigv4", "api_key", "access_keys"):
            raise AssistantError("CONFIG_AUTH")
        if b.credential_ref and not re.fullmatch(r"[0-9a-f]{32}", b.credential_ref):
            raise AssistantError("CREDENTIAL_REFERENCE_INVALID")
        for name, low, high in (("timeout_seconds", 5, 120), ("max_output_tokens", 128, 16000),
                                ("daily_calls", 1, 10000), ("daily_tokens", 1000, 10_000_000)):
            v = getattr(b, name)
            if type(v) is not int or not low <= v <= high:
                raise AssistantError("CONFIG_BUDGET")
        if b.reasoning_effort not in ("none", "low", "medium", "high", "xhigh", "max"):
            raise AssistantError("CONFIG_REASONING")


def minute_of_day(value: str) -> int:
    try:
        h, m = map(int, value.split(":"))
        if not (0 <= h <= 24 and 0 <= m < 60 and (h != 24 or m == 0)):
            raise ValueError()
        return h * 60 + m
    except (ValueError, AttributeError):
        raise AssistantError("CONFIG_TIME", "時刻はHH:MMで指定します") from None


def default_home() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "WorkAssistant" if base else Path.home() / ".local" / "share" / "WorkAssistant"


def load(home: Path) -> Settings:
    path = home / "config.json"
    if not path.is_file():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        b = BedrockSettings(**data.pop("bedrock", {}))
        cfg = Settings(**data, bedrock=b)
        cfg.validate()
        return cfg
    except AssistantError:
        raise
    except (ValueError, TypeError, AttributeError, OSError):
        raise AssistantError("CONFIG_INVALID", "ローカル設定の形式を確認してください") from None


def initialize(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        home.chmod(0o700)
    if not (home / "config.json").exists():
        atomic_json(home / "config.json", asdict(Settings()))
    for name in ("captures", "diagnostics"):
        (home / name).mkdir(exist_ok=True)
