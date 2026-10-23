"""Offline transport checks, including a real local CONNECT proxy rejection."""
from __future__ import annotations

import copy
import io
import os
import socket
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from work_assistant.bedrock import BedrockClient
from work_assistant.common import AssistantError
from work_assistant.config import Settings, initialize, load
from work_assistant.network import validate_proxy_url
from work_assistant.setup_flow import SetupError, validate_setup
from test_bedrock_models import cfg, Opener, ROOT, Wire


class NetworkTests(unittest.TestCase):
    def call(self, error):
        def fail(_): raise error
        with patch.dict(os.environ, {"AWS_BEARER_TOKEN_BEDROCK": "synthetic-bearer"}):
            with self.assertRaises(AssistantError) as result:
                BedrockClient(cfg(), Opener(fail))._request(ROOT + "/foundation-models", method="GET")
        self.assertNotIn("secret-canary", str(result.exception))
        return result.exception.code

    def test_proxy_tls_dns_timeout_and_aws_errors_have_distinct_safe_categories(self):
        for reason, code in ((OSError("Tunnel connection failed: 407 secret-canary"), "BEDROCK_PROXY_AUTH_REQUIRED"),
                             (OSError("Tunnel connection failed: 403 secret-canary"), "BEDROCK_PROXY_BLOCKED"),
                             (ssl.SSLCertVerificationError(1, "secret-canary"), "BEDROCK_TLS_CERTIFICATE"),
                             (ssl.SSLError(1, "secret-canary"), "BEDROCK_TLS_ERROR"),
                             (socket.gaierror(-2, "secret-canary"), "BEDROCK_DNS_FAILED"),
                             (TimeoutError("secret-canary"), "BEDROCK_TIMEOUT")):
            with self.subTest(code=code): self.assertEqual(self.call(urllib.error.URLError(reason)), code)
        for status, code in ((407, "BEDROCK_PROXY_AUTH_REQUIRED"), (403, "BEDROCK_ACCESS_DENIED")):
            error = urllib.error.HTTPError(ROOT, status, "secret-canary", {}, io.BytesIO(b"secret-canary"))
            self.assertEqual(self.call(error), code)

    def test_explicit_proxy_routes_https_connect_and_reports_407_without_leaking_bearer(self):
        received = []
        class Proxy(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                received.append((self.path, dict(self.headers)))
                self.send_response(407); self.send_header("Proxy-Authenticate", "Negotiate"); self.end_headers()
            def log_message(self, *_): pass
        server = HTTPServer(("127.0.0.1", 0), Proxy)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            settings = cfg(proxy_url=f"http://127.0.0.1:{server.server_port}", timeout_seconds=5)
            with patch.dict(os.environ, {"AWS_BEARER_TOKEN_BEDROCK": "synthetic-bearer"}), patch("urllib.request.proxy_bypass", return_value=False):
                with self.assertRaises(AssistantError) as result:
                    BedrockClient(settings)._request(ROOT + "/foundation-models", method="GET")
            self.assertEqual(result.exception.code, "BEDROCK_PROXY_AUTH_REQUIRED")
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0][0], "bedrock-runtime.ap-northeast-1.amazonaws.com:443")
            self.assertNotIn("Authorization", received[0][1]); self.assertNotIn("synthetic-bearer", str(received))
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=2)

    def test_proxy_selection_keeps_verified_tls_and_default_os_environment_discovery(self):
        for proxy in ("", "http://proxy.example:8080"):
            with patch.dict(os.environ, {"AWS_BEARER_TOKEN_BEDROCK": "synthetic-bearer"}), patch("work_assistant.bedrock.urllib.request.build_opener") as build:
                build.return_value.open.return_value = Wire({"models": []})
                BedrockClient(cfg(proxy_url=proxy))._request(ROOT + "/foundation-models", method="GET")
            handlers = build.call_args.args
            https = next(h for h in handlers if isinstance(h, urllib.request.HTTPSHandler))
            self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED); self.assertTrue(https._context.check_hostname)
            explicit = [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
            self.assertEqual([h.proxies for h in explicit], [{"https": proxy}] if proxy else [])

    def test_invalid_ca_file_fails_before_transport_and_does_not_disable_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.pem"; path.write_text("not-a-certificate", encoding="utf-8")
            opener = Opener(lambda _: self.fail("Invalid CA must not call the transport"))
            with patch.dict(os.environ, {"AWS_BEARER_TOKEN_BEDROCK": "synthetic-bearer"}):
                with self.assertRaises(AssistantError) as result:
                    BedrockClient(cfg(ca_bundle=str(path)), opener)._request(ROOT)
            self.assertEqual(result.exception.code, "BEDROCK_CA_BUNDLE_INVALID")

    def test_proxy_validation_rejects_credentials_pac_and_invalid_urls_without_echoing(self):
        for proxy in ("", "http://proxy.example:8080", "https://proxy.example:443", "http://[::1]:8080"):
            self.assertEqual(validate_proxy_url(proxy), proxy)
        for proxy in ("http://user:secret-canary@proxy.example:8080", "http://proxy.example/config.pac", "proxy.example:8080",
                      "http://proxy.example:99999", "http://proxy.example:0", "http://proxy.example?secret-canary", "http://proxy.example\n"):
            with self.assertRaises(AssistantError) as result: validate_proxy_url(proxy)
            self.assertEqual(result.exception.code, "BEDROCK_PROXY_INVALID"); self.assertNotIn("secret-canary", str(result.exception))
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); initialize(home); old = load(home)
            candidate = copy.deepcopy(old); candidate.bedrock.proxy_url = "http://proxy.example/config.pac"
            with self.assertRaises(SetupError) as result: validate_setup(candidate, home)
            self.assertEqual(result.exception.field, "proxy_url")
            self.assertEqual(old.bedrock.proxy_url, "")
