"""A small typed capability surface. No shell, arbitrary HTTP, approvals or settings tool."""
from __future__ import annotations

import json
from datetime import datetime

from .bridge import Bridge
from .common import AssistantError, JST, canonical, digest, now_utc, require_keys
from .config import Settings, minute_of_day
from .ingest import Ingestor
from .store import Store


def active_hours(cfg: Settings, now: datetime | None = None) -> bool:
    local = (now or now_utc()).astimezone(JST)
    current = local.hour * 60 + local.minute
    start, end = minute_of_day(cfg.active_start), minute_of_day(cfg.active_end)
    if start < end:
        return local.weekday() in cfg.weekdays and start <= current < end
    # Overnight windows belong to the weekday on which the window starts.
    day = local.weekday() if current >= start else (local.weekday() - 1) % 7
    return day in cfg.weekdays and (current >= start or current < end)


def string(limit=500, nullable=False):
    return {"type": ["string", "null"] if nullable else "string", "maxLength": limit}


def function(name: str, description: str, properties: dict) -> dict:
    return {"type": "function", "name": name, "description": description,
            "strict": True, "parameters": {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}}


TOOL_SCHEMAS = [
    function("search_documents", "取得済み文書・メール・会議文字起こしを日本語検索。ゼロ件は存在しない証明ではない。", {
        "query": string(), "limit": {"type": "integer", "minimum": 1, "maximum": 12},
        "kind": {"type": ["string", "null"], "enum": [None, "document", "mail", "teams_message", "transcript", "calendar", "capture", "search_result"]}}),
    function("read_document", "根拠文書の本文の指定範囲を読む。更新/取得時刻と取得範囲を確認する。", {
        "document_id": string(80), "offset": {"type": "integer", "minimum": 0, "maximum": 500000},
        "length": {"type": "integer", "minimum": 1, "maximum": 6000}}),
    function("get_project_context", "ユーザー定義の案件と承認済みの記憶・タスクを取得する。", {"project_id": string(80, True)}),
    function("search_conversation", "この対話セッションの過去の発言を検索。根拠文書とは区別する。", {"query": string(300)}),
    function("propose_action", "原文を引用して行動/決定/記憶の候補を作成。候補は人の承認まで確定しない。", {
        "kind": {"type": "string", "enum": ["action", "decision", "memory"]}, "title": string(500),
        "document_id": string(80), "quote": string(3000), "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "project_id": string(80, True), "due_at": string(50, True)}),
    function("request_information", "Power Automateに不足情報を非同期依頼。同期を待たずpendingと説明する。未使用の引数はnull。", {
        "action": {"type": "string", "enum": ["search_mail", "get_calendar", "get_transcript", "search_sharepoint"]},
        "query": string(500, True), "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 20},
        "meeting_id": string(2048, True), "start": string(50, True), "end": string(50, True)}),
    function("get_request_status", "自分が依頼した非同期取得の状態を確認する。", {"request_id": string(80)}),
    function("notify", "定期確認専用。重要な変化だけを原文引用付きで通知候補にする。通知しない選択を優先。", {
        "title": string(200), "text": string(1800), "document_id": string(80), "quote": string(1200),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1}}),
]


def validate_tool(name: str, arguments: dict) -> dict:
    tool = next((s for s in TOOL_SCHEMAS if s["name"] == name), None)
    if tool is None:
        raise AssistantError("TOOL_NOT_ALLOWED")
    schema = tool["parameters"]
    require_keys(arguments, set(schema["required"]))
    for key, value in arguments.items():
        rule = schema["properties"][key]
        types = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
        actual = "null" if value is None else "string" if isinstance(value, str) else "integer" if type(value) is int else "number" if type(value) is float else "other"
        if actual not in types and not (actual == "integer" and "number" in types):
            raise AssistantError("TOOL_ARGUMENT_TYPE")
        if "enum" in rule and value not in rule["enum"]:
            raise AssistantError("TOOL_ARGUMENT_ENUM")
        if isinstance(value, str) and len(value) > rule.get("maxLength", 10000):
            raise AssistantError("TOOL_ARGUMENT_LENGTH")
        if actual in ("integer", "number") and not rule.get("minimum", -1e100) <= value <= rule.get("maximum", 1e100):
            raise AssistantError("TOOL_ARGUMENT_RANGE")
    return arguments


class Tools:
    def __init__(self, store: Store, cfg: Settings, ingestor: Ingestor, bridge: Bridge,
                 session: str, mode: str = "chat", event_key: str = ""):
        self.store, self.cfg, self.ingestor, self.bridge = store, cfg, ingestor, bridge
        self.session, self.mode, self.event_key = session, mode, event_key
        self.seen: set[str] = set()
        self.trace: list[dict] = []

    def schemas(self) -> list[dict]:
        return [s for s in TOOL_SCHEMAS if s["name"] != "notify" or self.mode == "monitor"]

    def execute(self, name: str, a: dict) -> dict:
        validate_tool(name, a)
        if name not in {s["name"] for s in self.schemas()}:
            raise AssistantError("TOOL_NOT_ALLOWED")
        if name == "search_documents":
            results = []
            for row in self.store.search(a["query"], a["limit"], kind=a["kind"] or ""):
                try:
                    doc = self.ingestor.validate_live_source(row["id"])
                    if row["text"] not in doc["body"]:
                        continue
                except AssistantError:
                    continue
                self.seen.add(row["id"])
                results.append({k: v for k, v in row.items() if k not in ("source_url", "rank")})
            return {"results": results, "scope": "local_cache_only", "complete": False}
        if name == "read_document":
            doc = self.ingestor.validate_live_source(a["document_id"])
            self.seen.add(doc["id"])
            offset, length = a["offset"], a["length"]
            return {"document_id": doc["id"], "title": doc["title"], "text": doc["body"][offset:offset + length],
                    "offset": offset, "next_offset": offset + length if offset + length < len(doc["body"]) else None,
                    "source_modified_at": doc["modified_at"], "observed_at": doc["observed_at"],
                    "coverage": doc["metadata"].get("coverage", "provided_text"), "truncated": doc["metadata"].get("truncated", False)}
        if name == "get_project_context":
            notes = []
            for item in self.store.proposals("approved"):
                if item["kind"] == "action":
                    continue  # Tasks below carry completion state; do not revive completed actions.
                if a["project_id"] and item["project_id"] != a["project_id"]:
                    continue
                try:
                    self.ingestor.validate_live_source(item["document_id"])
                except AssistantError:
                    continue
                self.seen.add(item["document_id"])
                notes.append({k: item[k] for k in ("title", "kind", "document_id", "quote", "due_at", "project_id")})
            tasks = []
            for task in self.store.tasks():
                if a["project_id"] and task["project_id"] != a["project_id"]:
                    continue
                try:
                    self.ingestor.validate_live_source(task["document_id"])
                except AssistantError:
                    continue
                self.seen.add(task["document_id"])
                tasks.append(task)
            return {"projects": [p for p in self.store.projects() if not a["project_id"] or p["id"] == a["project_id"]], "confirmed_notes": notes[:30], "open_tasks": tasks[:30]}
        if name == "search_conversation":
            return {"results": self.store.search_conversations(a["query"], self.session), "provenance": "past_conversation_not_source_truth"}
        if name == "propose_action":
            self.ingestor.validate_live_source(a["document_id"])
            if a["document_id"] not in self.seen:
                raise AssistantError("READ_SOURCE_FIRST")
            pid = self.store.propose(kind=a["kind"], title=a["title"], document_id=a["document_id"], quote=a["quote"], confidence=a["confidence"], project_id=a["project_id"] or "", due_at=a["due_at"])
            return {"proposal_id": pid, "status": "pending_human_approval", "scheduled": False}
        if name == "request_information":
            keys = {"query", "limit"} if a["action"] in ("search_mail", "search_sharepoint") else {"start", "end"} if a["action"] == "get_calendar" else {"meeting_id"}
            if any(a[k] is not None for k in a.keys() - keys - {"action"}):
                raise AssistantError("BRIDGE_UNUSED_ARGUMENT")
            return self.bridge.request(a["action"], {k: a[k] for k in keys})
        if name == "get_request_status":
            return self.bridge.status(a["request_id"])
        if name == "notify":
            doc = self.ingestor.validate_live_source(a["document_id"])
            if a["document_id"] not in self.seen or not a["quote"].strip() or a["quote"] not in doc["body"]:
                raise AssistantError("PROVENANCE_INVALID")
            if a["confidence"] < 0.75 or not active_hours(self.cfg) or self.store.get_state("focus", False):
                return {"status": "suppressed_by_policy"}
            sent = self.store.notify(digest(self.event_key + ":" + doc["id"]), a["title"], a["text"] + "\n\n根拠: " + a["quote"], doc["id"], daily_limit=self.cfg.notification_daily_limit, cooldown_minutes=self.cfg.notification_cooldown_minutes)
            return {"status": "recorded_locally" if sent else "suppressed_by_policy"}
        raise AssistantError("TOOL_NOT_ALLOWED")
