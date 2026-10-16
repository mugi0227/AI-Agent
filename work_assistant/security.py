"""Enforced boundaries; prompts are not a security boundary."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .common import AssistantError
from .config import BedrockSettings, Settings

ALLOWED_SUFFIXES = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".eml", ".vtt", ".docx", ".pptx", ".xlsx", ".pdf"}
DENIED_PARTS = {".git", ".aws", ".azure", ".ssh", ".gnupg", ".venv", "node_modules", "__pycache__"}
DENIED_NAMES = {"credentials", "config.json", "id_rsa", "id_ed25519", "secrets.json", "package-lock.json"}


def expanded(path: str | Path) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser().absolute()


def is_link_or_junction(path: Path) -> bool:
    info = path.lstat()
    tag = getattr(info, "st_reparse_tag", 0)
    return stat.S_ISLNK(info.st_mode) or tag in (0xA0000003, 0xA000000C)


def allowed_path(path: Path, roots: list[str], *, home: Path | None = None) -> Path:
    path = expanded(path)
    # Inspect components BEFORE resolve so junction/symlink ancestry cannot disappear.
    try:
        for part in (path, *path.parents):
            if part.exists() and is_link_or_junction(part):
                raise AssistantError("LINK_BLOCKED")
        resolved = path.resolve(strict=True)
        legal = any(resolved.is_relative_to(expanded(root).resolve(strict=True)) for root in roots)
        if not legal:
            raise AssistantError("PATH_NOT_ALLOWED")
        if home and resolved.is_relative_to(home.resolve()):
            raise AssistantError("STATE_DIRECTORY_BLOCKED")
        if any(p.lower() in DENIED_PARTS for p in resolved.parts):
            raise AssistantError("SENSITIVE_PATH_BLOCKED")
        if resolved.name.lower() in DENIED_NAMES or resolved.name.lower().startswith(".env"):
            raise AssistantError("SENSITIVE_PATH_BLOCKED")
        if not resolved.is_file():
            raise AssistantError("NOT_A_FILE")
        return resolved
    except OSError:
        raise AssistantError("FILE_UNAVAILABLE") from None


def require_file(path: Path, cfg: Settings, home: Path) -> Path:
    if not cfg.local_data_approved:
        raise AssistantError("LOCAL_DATA_NOT_APPROVED")
    path = allowed_path(path, cfg.roots, home=home)
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise AssistantError("FILE_TYPE_BLOCKED")
    info = path.stat()
    if info.st_size > cfg.max_file_bytes:
        raise AssistantError("FILE_TOO_LARGE")
    attributes = getattr(info, "st_file_attributes", 0)
    if not cfg.hydrate_cloud_files and attributes & (0x1000 | 0x40000 | 0x400000):
        raise AssistantError("CLOUD_PLACEHOLDER", "オフライン利用が許可されたファイルだけを取得してください")
    return path


def safe_web_url(url: str, hosts: list[str]) -> str:
    if not isinstance(url, str) or len(url) > 4000:
        raise AssistantError("WEB_URL_INVALID")
    try:
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname not in hosts or p.username or p.password or p.port not in (None, 443):
            raise AssistantError("WEB_HOST_BLOCKED")
        # Query strings/fragments frequently contain share tokens or personal information.
        return urlunsplit(("https", p.hostname, p.path, "", ""))
    except ValueError:
        raise AssistantError("WEB_URL_INVALID") from None


def validate_bedrock(b: BedrockSettings, *, live: bool = True) -> tuple[str, str]:
    if live and (not b.enabled or not b.data_transfer_approved):
        raise AssistantError("MODEL_EGRESS_NOT_APPROVED", "Bedrockへの送信承認と有効化が必要です")
    try:
        p = urlsplit(b.endpoint)
        runtime = f"bedrock-runtime.{b.region}.amazonaws.com"
        mantle = f"bedrock-mantle.{b.region}.api.aws"
        if p.scheme != "https" or p.hostname not in (runtime, mantle) or p.username or p.password:
            raise AssistantError("BEDROCK_ENDPOINT_BLOCKED", "承認されたBedrockエンドポイントだけを使用します")
        if p.port not in (None, 443) or p.query or p.fragment or p.path.rstrip("/") != "/openai/v1":
            raise AssistantError("BEDROCK_ENDPOINT_BLOCKED")
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", b.region):
            raise AssistantError("BEDROCK_REGION_INVALID")
        if not re.fullmatch(r"(?:(?:global|us)\.)?openai\.[a-z0-9.-]+", b.model):
            raise AssistantError("BEDROCK_MODEL_INVALID")
        if p.hostname == runtime:
            if not b.model.startswith(("global.", "us.")):
                raise AssistantError("INFERENCE_PROFILE_REQUIRED")
            if live and not b.cross_region_approved:
                raise AssistantError("CROSS_REGION_NOT_APPROVED", "推論プロファイルの処理地域を承認してください")
            if b.model.startswith("us.") and not b.region.startswith(("us-", "ca-")):
                raise AssistantError("PROFILE_REGION_MISMATCH")
            return b.endpoint.rstrip("/"), "bedrock"
        if b.model.startswith(("global.", "us.")):
            raise AssistantError("MANTLE_MODEL_ID_INVALID")
        if b.model == "openai.gpt-6-luna" and b.region != "us-east-1":
            raise AssistantError("LUNA_MANTLE_REGION_UNVERIFIED")
        return b.endpoint.rstrip("/"), "bedrock-mantle"
    except ValueError:
        raise AssistantError("BEDROCK_ENDPOINT_BLOCKED") from None
