"""Bedrock transport settings and safe error categories; TLS remains verified."""
from __future__ import annotations

import re
import socket
import ssl
import urllib.error
from urllib.parse import urlsplit

from .common import AssistantError


def validate_proxy_url(value: str) -> str:
    if not value: return ""
    try:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 33 for c in value): raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment or
                parsed.port == 0):
            raise ValueError()
        if not re.fullmatch(r"[A-Za-z0-9.:[\]-]+", parsed.netloc): raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AssistantError("BEDROCK_PROXY_INVALID") from None
    return value


def network_error_code(error: BaseException) -> str:
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError): return "BEDROCK_TLS_CERTIFICATE"
    if isinstance(reason, ssl.SSLError): return "BEDROCK_TLS_ERROR"
    if isinstance(reason, TimeoutError): return "BEDROCK_TIMEOUT"
    if isinstance(reason, socket.gaierror): return "BEDROCK_DNS_FAILED"
    # urllib wraps CONNECT status in an OSError rather than an HTTPError.
    # Inspect only its fixed status prefix; never expose exception text.
    if re.search(r"Tunnel connection failed:\s*407\b", str(reason)): return "BEDROCK_PROXY_AUTH_REQUIRED"
    if re.search(r"Tunnel connection failed:\s*403\b", str(reason)): return "BEDROCK_PROXY_BLOCKED"
    return "BEDROCK_NETWORK_OR_TLS"
