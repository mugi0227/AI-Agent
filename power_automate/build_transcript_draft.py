"""Generate a REVIEW-ONLY flow definition, not an importable package or a deployment.

Company Codex must bind real connection references, validate output shapes and
run the manual flow before adapting it to a transcript trigger. No cloud call.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def connector(api, operation, parameters, after=None):
    return {"type": "OpenApiConnection", "runAfter": after or {}, "inputs": {
        "host": {"apiId": "/providers/Microsoft.PowerApps/apis/" + api, "connectionName": api, "operationId": operation},
        "parameters": parameters, "authentication": "@parameters('$authentication')"}}


def build(sink="onedrive"):
    if sink not in ("onedrive", "box"): raise ValueError("Unsupported sink")
    api = "shared_onedriveforbusiness" if sink == "onedrive" else "shared_box"
    body = {"schema_version": 1, "event_id": "@guid()", "kind": "transcript",
        "source_id": "@concat('teams:', triggerBody()?['meetingId'], ':', items('Each_transcript')?['id'])",
        "title": "@triggerBody()?['title']", "content": "@body('Get_transcript_content')", "content_type": "text/vtt",
        "source_url": "@triggerBody()?['meetingUrl']", "source_modified_at": "@coalesce(items('Each_transcript')?['createdDateTime'], outputs('Observed_at'))",
        "observed_at": "@outputs('Observed_at')", "expires_at": "@addHours(outputs('Observed_at'), 24)",
        "metadata": {"coverage": "returned_transcript", "organizer_scope": "verify_in_company_test"}}
    inner = {
        "Get_transcript_content": connector("shared_teams", "GetMeetingTranscriptContent", {"meetingId": "@triggerBody()?['meetingId']", "transcriptId": "@items('Each_transcript')?['id']"}),
        "Observed_at": {"type": "Compose", "inputs": "@utcNow()", "runAfter": {"Get_transcript_content": ["Succeeded"]}},
        "Envelope": {"type": "Compose", "inputs": body, "runAfter": {"Observed_at": ["Succeeded"]}},
        "Save_JSON": connector(api, "CreateFile", {"folderPath": "@triggerBody()?['sinkFolder']", "name": "@concat(outputs('Envelope')?['event_id'], '.json')", "body": "@string(outputs('Envelope'))"}, {"Envelope": ["Succeeded"]})}
    definition = {"$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
        "contentVersion": "1.0.0.0", "parameters": {"$connections": {"defaultValue": {}, "type": "Object"}, "$authentication": {"defaultValue": {}, "type": "SecureObject"}},
        "triggers": {"manual": {"type": "Request", "kind": "Button", "inputs": {"schema": {"type": "object", "properties": {k: {"type": "string"} for k in ("meetingId", "title", "meetingUrl", "sinkFolder")}, "required": ["meetingId", "title", "meetingUrl", "sinkFolder"]}}}},
        "actions": {"List_transcripts": connector("shared_teams", "ListMeetingTranscripts", {"meetingId": "@triggerBody()?['meetingId']"}),
                    "Each_transcript": {"type": "Foreach", "foreach": "@body('List_transcripts')?['value']", "runAfter": {"List_transcripts": ["Succeeded"]}, "actions": inner, "runtimeConfiguration": {"concurrency": {"repetitions": 1}}}}}
    return {"artifact_type": "REVIEW_ONLY_NOT_IMPORT_PACKAGE", "assumptions_to_verify": ["Manual trigger property names", "List response collection is value", "Transcript content is a VTT string, not base64 wrapper", "List contains all allowed transcript instances", "sinkFolder is approved and fixed before enabling automation", "Failure scopes, empty-body guards and per-source refresh are added per README"],
            "properties": {"connectionReferences": {"shared_teams": {"runtimeSource": "embedded", "connection": {}, "api": {"name": "shared_teams"}}, api: {"runtimeSource": "embedded", "connection": {}, "api": {"name": api}}}, "definition": definition}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--sink", choices=["onedrive", "box"], default="onedrive"); parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(); args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build(args.sink), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
