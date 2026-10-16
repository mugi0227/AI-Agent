"""OpenAI Responses wire protocol on Bedrock only; API key or refreshable SigV4.

No OpenAI SDK default endpoint, remote tools, redirects, retry amplification or
server-side response retention. Network is enabled only after local approval.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Callable

from .common import AssistantError, canonical
from .config import BedrockSettings
from .security import validate_bedrock

MAX_RESPONSE_BYTES = 8_000_000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AssistantError("BEDROCK_REDIRECT_BLOCKED")


def response_text(response: dict) -> str:
    parts = []
    for item in response.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    parts.append(content.get("text", ""))
                elif content.get("type") == "refusal":
                    raise AssistantError("MODEL_REFUSAL", "モデルがこの依頼への回答を拒否しました")
    return "\n".join(parts)


def validate_response(response: dict) -> dict:
    if not isinstance(response, dict) or not isinstance(response.get("output"), list):
        raise AssistantError("MODEL_RESPONSE_INVALID")
    if response.get("status") != "completed":
        raise AssistantError("MODEL_RESPONSE_INCOMPLETE", "推論が未完了です。ツールは実行していません")
    for item in response["output"]:
        if not isinstance(item, dict):
            raise AssistantError("MODEL_RESPONSE_INVALID")
        if item.get("type") == "function_call" and not all(isinstance(item.get(k), str) and item[k] for k in ("call_id", "name", "arguments")):
            raise AssistantError("MODEL_TOOL_CALL_INVALID")
    return response


class BedrockClient:
    is_live = True

    def __init__(self, settings: BedrockSettings, opener=None):
        self.settings = settings
        self._opener = opener  # Explicit injection for offline transport tests.

    def _headers(self, url: str, body: bytes, service: str) -> dict:
        cfg = self.settings
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if cfg.streaming else "application/json"}
        if cfg.auth == "api_key":
            key = os.environ.get(cfg.api_key_env, "")
            if not key or "\n" in key or "\r" in key:
                raise AssistantError("BEDROCK_KEY_MISSING", "Bedrock APIキーの環境変数を確認してください")
            headers["Authorization"] = "Bearer " + key
            return headers
        try:
            import boto3
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest
            os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
            session = boto3.Session(profile_name=cfg.profile or None, region_name=cfg.region)
            credentials = session.get_credentials()
            if credentials is None:
                raise AssistantError("AWS_CREDENTIALS_MISSING")
            frozen = credentials.get_frozen_credentials()  # Refresh SSO/temporary credentials per call.
            request = AWSRequest(method="POST", url=url, data=body, headers=headers)
            SigV4Auth(frozen, service, cfg.region).add_auth(request)
            return dict(request.headers)
        except ImportError:
            raise AssistantError("SIGV4_DEPENDENCY_MISSING", "承認済boto3/botocoreが必要です") from None
        except AssistantError:
            raise
        except Exception:
            raise AssistantError("AWS_CREDENTIALS_UNAVAILABLE", "プロファイル/SSOログイン/有効期限を確認してください") from None

    def create(self, input_items: list[dict], instructions: str, tools: list[dict],
               on_delta: Callable[[str], None] | None = None) -> dict:
        cfg = self.settings
        base, service = validate_bedrock(cfg)
        payload = {"model": cfg.model, "input": input_items, "instructions": instructions,
                   "tools": tools, "store": False, "stream": cfg.streaming,
                   "max_output_tokens": cfg.max_output_tokens,
                   "reasoning": {"effort": cfg.reasoning_effort}, "parallel_tool_calls": False}
        if cfg.encrypted_reasoning:
            payload["include"] = ["reasoning.encrypted_content"]
        body = canonical(payload).encode("utf-8")
        url = base + "/responses"
        headers = self._headers(url, body, service)
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            context = ssl.create_default_context(cafile=cfg.ca_bundle or None)
            opener = self._opener or urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
            with opener.open(request, timeout=cfg.timeout_seconds) as response:
                content_type = response.headers.get("Content-Type", "")
                if "text/event-stream" in content_type:
                    result = self._read_stream(response, on_delta)
                else:
                    data = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise AssistantError("MODEL_RESPONSE_TOO_LARGE")
                    result = json.loads(data.decode("utf-8"))
            return validate_response(result)
        except urllib.error.HTTPError as error:
            # Never expose raw error body: providers may echo prompts or credentials.
            if error.code in (401, 403):
                code = "BEDROCK_ACCESS_DENIED"
            elif error.code == 429:
                code = "BEDROCK_THROTTLED"
            elif error.code in (400, 404):
                code = "BEDROCK_API_OR_MODEL_MISMATCH"
            else:
                code = "BEDROCK_HTTP_ERROR"
            raise AssistantError(code, f"HTTP {error.code}。認証・モデル・API・管理ポリシーを検証してください", retryable=error.code in (429, 500, 502, 503, 504)) from None
        except AssistantError:
            raise
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError):
            raise AssistantError("BEDROCK_NETWORK_OR_TLS", "接続/社内proxy/証明書を確認してください。TLS検証は無効化しません", retryable=True) from None
        except (UnicodeError, ValueError, TypeError):
            raise AssistantError("MODEL_RESPONSE_INVALID") from None

    @staticmethod
    def _read_stream(stream, on_delta=None) -> dict:
        size, event_lines, completed = 0, [], None
        while True:
            line = stream.readline(1_000_001)
            if not line:
                break
            size += len(line)
            if len(line) > 1_000_000 or size > MAX_RESPONSE_BYTES:
                raise AssistantError("MODEL_RESPONSE_TOO_LARGE")
            line = line.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                event_lines.append(line[5:].lstrip())
            if line == "" and event_lines:
                text = "\n".join(event_lines)
                event_lines = []
                if text == "[DONE]":
                    break
                event = json.loads(text)
                event_type = event.get("type")
                if event_type == "response.output_text.delta" and on_delta:
                    on_delta(str(event.get("delta", "")))
                elif event_type == "response.completed":
                    completed = event.get("response")
                elif event_type in ("error", "response.failed", "response.incomplete"):
                    raise AssistantError("MODEL_STREAM_FAILED")
        if completed is None:
            raise AssistantError("MODEL_STREAM_TRUNCATED", "完了イベントを受信できませんでした")
        return completed


class ScriptedClient:
    """Only for unit tests; it cannot contact any endpoint."""
    is_live = False

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, input_items, instructions, tools, on_delta=None):
        self.calls.append(json.loads(canonical({"input": input_items, "instructions": instructions, "tools": tools})))
        if not self.responses:
            raise AssistantError("MOCK_RESPONSES_EXHAUSTED")
        return validate_response(self.responses.pop(0))
