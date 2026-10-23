"""Settings validation and synthetic connection checks; independent of Tk."""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .bedrock import BedrockClient, response_text
from .common import AssistantError, canonical, digest
from .config import Settings
from .credentials import CredentialStore
from .security import DENIED_PARTS, expanded, is_link_or_junction, validate_bedrock
from .tools import function, string


class SetupError(AssistantError):
    def __init__(self, field: str, message: str):
        super().__init__("SETUP_INVALID", message)
        self.field = field


def validate_setup(cfg: Settings, home: Path) -> None:
    cfg.validate()
    for raw in cfg.roots:
        path = expanded(raw)
        try:
            if not path.is_dir():
                raise SetupError("roots", "資料フォルダが見つかりません。存在するフォルダを選び直してください。")
            if any(is_link_or_junction(p) for p in (path, *path.parents)):
                raise SetupError("roots", "リンク先のフォルダは利用できません。元のフォルダを選んでください。")
            if path.resolve().is_relative_to(home.resolve()) or any(p.lower() in DENIED_PARTS for p in path.parts):
                raise SetupError("roots", "アプリの保存先や認証情報のフォルダは資料に指定できません。")
            if path == Path(path.anchor):
                raise SetupError("roots", "ドライブ全体ではなく、利用する資料のフォルダを選んでください。")
        except OSError:
            raise SetupError("roots", "資料フォルダにアクセスできません。場所とアクセス権を確認してください。") from None
    if cfg.bedrock.auth == "api_key" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cfg.bedrock.api_key_env):
        raise SetupError("api_key_env", "キー本体ではなく、キーを登録した環境変数名を入力してください。")
    try:
        validate_bedrock(cfg.bedrock, live=False)
    except AssistantError as error:
        field = "model" if "MODEL" in error.code or "PROFILE" in error.code else "region" if "REGION" in error.code else "endpoint"
        raise SetupError(field, {
            "BEDROCK_ENDPOINT_BLOCKED": "接続先は指定地域のBedrock HTTPS URLを指定してください。ClaudeはRuntimeのホストのみ、OpenAIは末尾に /openai/v1 を指定します。",
            "BEDROCK_REGION_INVALID": "AWSの地域を確認してください。例: ap-northeast-1。",
            "INFERENCE_PROFILE_REQUIRED": "Bedrock Runtimeでは、global. または us. で始まる推論プロファイルIDが必要です。",
            "PROFILE_REGION_MISMATCH": "推論プロファイルの接頭辞とAWSの地域を確認してください。地域をまたぐ場合は利用するプロファイルの処理地域も確認します。",
            "MANTLE_MODEL_ID_INVALID": "Bedrock Mantleではglobal. や us. を付けないモデルIDを指定してください。",
        }.get(error.code, "モデルID・地域・接続先の組み合わせを確認してください。")) from None


def connection_fingerprint(cfg: Settings) -> str:
    return digest(canonical(asdict(cfg.bedrock)))


def probe_connection(cfg: Settings, store, *, client=None) -> dict:
    """Two budgeted calls using only a random nonce and a local echo tool."""
    validate_bedrock(cfg.bedrock)
    client = client or BedrockClient(cfg.bedrock, credential_store=CredentialStore(store.path.parent))
    nonce = "probe-" + uuid.uuid4().hex[:12]
    schema = function("echo_probe", "Echo the exact nonce to validate function calling", {"nonce": string(100)})
    items = [{"role": "user", "content": "Call echo_probe with nonce=" + nonce + ". After receiving its result, reply only with that nonce."}]
    started = time.monotonic()
    reservation = 10000 + cfg.bedrock.max_output_tokens

    def invoke(instructions):
        day = store.reserve_budget(reservation, cfg.bedrock.daily_calls, cfg.bedrock.daily_tokens)
        response = client.create(items, instructions, [schema])
        store.settle_budget(day, reservation, response.get("usage", {}).get("total_tokens", reservation))
        return response

    first = invoke("This is a synthetic connectivity test, not company data.")
    calls = [x for x in first["output"] if x.get("type") == "function_call"]
    try:
        valid = len(calls) == 1 and calls[0]["name"] == "echo_probe" and json.loads(calls[0]["arguments"]) == {"nonce": nonce}
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise AssistantError("PROBE_TOOL_NOT_CALLED")
    call = calls[0]
    items += first["output"] + [{"type": "function_call_output", "call_id": call["call_id"], "output": canonical({"nonce": nonce})}]
    second = invoke("Return the nonce from the tool result.")
    if nonce not in response_text(second) or any(x.get("type") == "function_call" for x in second["output"]):
        raise AssistantError("PROBE_TOOL_RESULT_NOT_USED")
    return {"elapsed_seconds": round(time.monotonic() - started, 2),
            "reasoning_items_preserved": sum(x.get("type") == "reasoning" for x in first["output"]), "store": False}
