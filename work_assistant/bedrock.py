"""Bedrock gateway: OpenAI Responses or Claude Converse; API key or SigV4.

No OpenAI SDK default endpoint, remote tools, redirects, retry amplification or
server-side response retention. Inference requires explicit egress approval;
model discovery reads metadata only after the user requests the catalog.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from urllib.parse import quote
from typing import Callable

from .common import AssistantError, canonical
from .config import BedrockSettings
from .credentials import AccessKeys
from .security import model_api, validate_bedrock
from . import converse

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

    def __init__(self, settings: BedrockSettings, opener=None, *, credential_store=None, credentials: AccessKeys | None = None):
        self.settings = settings
        self._opener = opener  # Explicit injection for offline transport tests.
        self.credential_store, self.credentials = credential_store, credentials

    def _headers(self, url: str, body: bytes | None, service: str, method="POST", accept="application/json") -> dict:
        cfg = self.settings
        headers = {"Content-Type": "application/json", "Accept": accept}
        if cfg.auth == "api_key":
            key = os.environ.get(cfg.api_key_env, "")
            if not key or "\n" in key or "\r" in key:
                raise AssistantError("BEDROCK_KEY_MISSING", "Bedrock APIキーの環境変数を確認してください")
            headers["Authorization"] = "Bearer " + key
            return headers
        try:
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest
            if cfg.auth == "access_keys":
                from botocore.credentials import Credentials
                keys = self.credentials
                if keys is None:
                    if not cfg.credential_ref or self.credential_store is None: raise AssistantError("AWS_ACCESS_KEYS_MISSING")
                    keys = self.credential_store.read(cfg.credential_ref)
                keys.validate()
                frozen = Credentials(keys.access_key_id, keys.secret_access_key, keys.session_token or None).get_frozen_credentials()
            elif cfg.auth == "sigv4":
                import boto3
                os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
                session = boto3.Session(profile_name=cfg.profile or None, region_name=cfg.region)
                credentials = session.get_credentials()
                if credentials is None: raise AssistantError("AWS_CREDENTIALS_MISSING")
                frozen = credentials.get_frozen_credentials()  # Refresh SSO/temporary credentials per call.
            else:
                raise AssistantError("CONFIG_AUTH")
            request = AWSRequest(method=method, url=url, data=body, headers=headers)
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
        api = model_api(cfg.model)
        if api == "converse":
            try:
                payload = converse.request_payload(input_items, instructions, tools, cfg.max_output_tokens)
            except (ValueError, KeyError, TypeError, AttributeError):
                raise AssistantError("MODEL_INPUT_INVALID") from None
            suffix = "/converse-stream" if cfg.streaming else "/converse"
            result = self._request(base + "/model/" + quote(cfg.model, safe="") + suffix, payload,
                service=service, stream="converse" if cfg.streaming else None, on_delta=on_delta)
            return validate_response(result if cfg.streaming else converse.normalize(result))
        # A waiting job can be resumed after switching providers. Do not send
        # Claude-only opaque content to Responses. Plain text and tools remain.
        input_items = [i for i in input_items if not (i.get("type") == "reasoning" and "bedrock_content" in i)]
        payload = {"model": cfg.model, "input": input_items, "instructions": instructions,
                   "tools": tools, "store": False, "stream": cfg.streaming,
                   "max_output_tokens": cfg.max_output_tokens,
                   "reasoning": {"effort": cfg.reasoning_effort}, "parallel_tool_calls": False}
        if cfg.encrypted_reasoning:
            payload["include"] = ["reasoning.encrypted_content"]
        return validate_response(self._request(base + "/responses", payload, service=service,
            stream="responses" if cfg.streaming else None, on_delta=on_delta))

    def _request(self, url, payload=None, *, service="bedrock", method="POST", stream=None, on_delta=None, timeout=None):
        cfg = self.settings
        body = canonical(payload).encode("utf-8") if payload is not None else None
        accept = "application/vnd.amazon.eventstream" if stream == "converse" else "text/event-stream" if stream == "responses" else "application/json"
        headers = self._headers(url, body, service, method, accept)
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            context = ssl.create_default_context(cafile=cfg.ca_bundle or None)
            opener = self._opener or urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
            with opener.open(request, timeout=timeout or cfg.timeout_seconds) as response:
                content_type = response.headers.get("Content-Type", "")
                if stream == "converse":
                    if "application/vnd.amazon.eventstream" not in content_type:
                        raise AssistantError("MODEL_STREAM_INVALID")
                    result = converse.read_stream(response, on_delta)
                elif "text/event-stream" in content_type and stream == "responses":
                    result = self._read_stream(response, on_delta)
                else:
                    data = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise AssistantError("MODEL_RESPONSE_TOO_LARGE")
                    result = json.loads(data.decode("utf-8"))
            if not isinstance(result, dict): raise AssistantError("MODEL_RESPONSE_INVALID")
            return result
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
