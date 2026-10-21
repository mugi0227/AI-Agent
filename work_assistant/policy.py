"""Host-enforced capabilities. Model output cannot grant authority."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from .common import AssistantError

# Store checks this inside BEGIN IMMEDIATE, before committing a tool side effect.
execution_guard: ContextVar = ContextVar("work_assistant_execution_guard", default=None)

READ_TOOLS = frozenset({"search_documents", "read_document", "get_project_context", "search_conversation", "search_memory", "read_memory", "get_request_status", "read_work_result"})
BASE_TOOLS = READ_TOOLS | {"propose_action", "request_information"}
WORK_TOOLS = BASE_TOOLS | {"delegate_work", "wait_for_work", "wait_for_information", "wait_for_approval", "wait_until", "wait_for_change", "save_artifact"}
ALL_TOOLS = WORK_TOOLS | {"notify"}


@dataclass(frozen=True)
class Capability:
    agent_id: str = "main"
    tools: frozenset[str] = BASE_TOOLS
    # None means all sources already allowed by the local ingestion policy.
    document_ids: frozenset[str] | None = None
    project_ids: frozenset[str] | None = None

    def authorize(self, name: str, document_id: str | None = None) -> None:
        if name not in self.tools or name not in ALL_TOOLS:
            raise AssistantError("TOOL_POLICY_DENIED")
        if document_id is not None and self.document_ids is not None and document_id not in self.document_ids:
            raise AssistantError("SOURCE_SCOPE_DENIED")

    def source_allowed(self, document_id: str) -> bool:
        return self.document_ids is None or document_id in self.document_ids

    def project_allowed(self, project_id: str) -> bool:
        return self.project_ids is None or project_id in self.project_ids

    def attenuate(self, child: Capability) -> Capability:
        def intersect(parent, requested):
            return requested if parent is None else parent if requested is None else parent & requested
        return Capability(child.agent_id, self.tools & child.tools,
                          intersect(self.document_ids, child.document_ids),
                          intersect(self.project_ids, child.project_ids))

    def as_dict(self) -> dict:
        return {"agent_id": self.agent_id, "tools": sorted(self.tools),
                "document_ids": None if self.document_ids is None else sorted(self.document_ids),
                "project_ids": None if self.project_ids is None else sorted(self.project_ids)}

    @classmethod
    def from_dict(cls, value: dict) -> Capability:
        return cls(value["agent_id"], frozenset(value["tools"]),
                   None if value["document_ids"] is None else frozenset(value["document_ids"]),
                   None if value.get("project_ids") is None else frozenset(value["project_ids"]))
