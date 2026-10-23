"""Authentication tests use synthetic keys and never contact AWS."""
from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
import os
import tempfile
import unittest
import uuid
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qsl, quote

from work_assistant.bedrock import BedrockClient
from work_assistant.common import AssistantError, canonical
from work_assistant.config import BedrockSettings, Settings, initialize, load
from work_assistant.credentials import AccessKeys, CredentialStore, WindowsProtector
from work_assistant.runtime import Runtime
from work_assistant.setup_flow import connection_fingerprint


def keys(suffix="A", temporary=False):
    return AccessKeys(("ASIA" if temporary else "TEST") + "ACCESSKEY0000000" + suffix, suffix * 40, "test-session-token-" + suffix if temporary else "")


class MemoryProtector:
    """Opaque test storage; plaintext stays in this object's memory only."""
    def __init__(self): self.records = {}
    def protect(self, data, entropy):
        opaque = b"synthetic-ciphertext-" + uuid.uuid4().hex.encode()
        self.records[opaque] = (data, entropy)
        return opaque
    def unprotect(self, data, entropy):
        if data not in self.records or self.records[data][1] != entropy: raise AssistantError("CREDENTIAL_DECRYPT_FAILED")
        return self.records[data][0]


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.home = Path(self.tmp.name)
        initialize(self.home); self.rt = Runtime(self.home)
        self.rt.credentials.protector = MemoryProtector()

    def tearDown(self): self.rt.close(); self.tmp.cleanup()

    def candidate(self):
        cfg = copy.deepcopy(self.rt.cfg); cfg.bedrock.auth = "access_keys"
        return cfg

    def test_store_roundtrip_has_no_plaintext_or_secrets_in_repr(self):
        material = keys(temporary=True); ref = self.rt.credentials.save(material)
        reopened = CredentialStore(self.home, protector=self.rt.credentials.protector)
        self.assertEqual(reopened.read(ref), material)
        for value in (material.access_key_id, material.secret_access_key, material.session_token):
            self.assertNotIn(value, repr(material))
            self.assertNotIn(value.encode(), (self.home / "credentials" / (ref + ".bin")).read_bytes())
        with self.assertRaises(AssistantError): reopened.read("../outside")
        blob = self.home / "credentials" / (ref + ".bin"); blob.write_bytes(b"tampered")
        with self.assertRaises(AssistantError) as error: reopened.read(ref)
        self.assertEqual(error.exception.code, "CREDENTIAL_DECRYPT_FAILED")

    def test_save_rotation_delete_and_reopen_preserve_shared_runtime_references(self):
        cfg = self.candidate(); bedrock = self.rt.cfg.bedrock
        self.assertTrue(self.rt.configure(cfg, credentials=keys()))
        old = load(self.home).bedrock.credential_ref; before_hash = connection_fingerprint(self.rt.cfg)
        self.assertIs(self.rt.agent.client.settings, bedrock)
        self.assertIs(self.rt.agent.client.credential_store, self.rt.credentials)
        self.assertEqual(self.rt.credentials.read(old), keys())
        self.assertTrue(self.rt.configure(self.candidate(), credentials=keys("B", True)))
        new = load(self.home).bedrock.credential_ref
        self.assertNotEqual(old, new); self.assertNotEqual(before_hash, connection_fingerprint(self.rt.cfg))
        self.assertEqual(self.rt.credentials.read(new), keys("B", True))
        self.assertFalse((self.home / "credentials" / (old + ".bin")).exists())
        self.assertTrue(self.rt.configure(self.candidate(), clear_credentials=True))
        self.assertEqual(load(self.home).bedrock.credential_ref, "")
        self.assertFalse(list((self.home / "credentials").glob("*.bin")))
        for path in self.home.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                for material in (keys(), keys("B", True)):
                    for value in (material.access_key_id, material.secret_access_key, material.session_token):
                        if value: self.assertNotIn(value.encode(), data)

    def test_failed_config_save_or_busy_runtime_cannot_replace_existing_key(self):
        self.rt.configure(self.candidate(), credentials=keys())
        before = (self.home / "config.json").read_bytes(); snapshot = asdict(self.rt.cfg)
        with patch("work_assistant.runtime.atomic_json", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError): self.rt.configure(self.candidate(), credentials=keys("B"))
        self.assertEqual((self.home / "config.json").read_bytes(), before)
        self.assertEqual(asdict(self.rt.cfg), snapshot)
        self.assertEqual(len(list((self.home / "credentials").glob("*.bin"))), 1)
        self.assertEqual(self.rt.credentials.read(self.rt.cfg.bedrock.credential_ref), keys())
        with self.rt.operation(), patch.object(self.rt.credentials, "save") as save:
            with self.assertRaises(AssistantError): self.rt.configure(self.candidate(), credentials=keys("B"))
            save.assert_not_called()

    def test_failed_encryption_keeps_old_configuration_and_no_plaintext_fallback(self):
        before = (self.home / "config.json").read_bytes()
        with patch.object(self.rt.credentials.protector, "protect", side_effect=AssistantError("CREDENTIAL_STORE_FAILED")):
            with self.assertRaises(AssistantError): self.rt.configure(self.candidate(), credentials=keys())
        self.assertEqual((self.home / "config.json").read_bytes(), before)
        self.assertFalse(list(self.home.glob("credentials/*")))

    def test_retired_ciphertext_cleanup_failure_is_reported_and_can_be_retried(self):
        self.rt.configure(self.candidate(), credentials=keys()); old = self.rt.cfg.bedrock.credential_ref
        with patch.object(self.rt.credentials, "delete", side_effect=AssistantError("CREDENTIAL_DELETE_FAILED")):
            self.assertFalse(self.rt.configure(self.candidate(), credentials=keys("B")))
        self.assertNotEqual(load(self.home).bedrock.credential_ref, old)
        self.assertEqual(self.rt.credentials.read(self.rt.cfg.bedrock.credential_ref), keys("B"))
        self.assertTrue(self.rt.configure(self.candidate()))
        self.assertFalse((self.home / "credentials" / (old + ".bin")).exists())
        with patch("pathlib.Path.iterdir", side_effect=PermissionError("synthetic denied directory")):
            self.assertFalse(self.rt.credentials.prune(keep=self.rt.cfg.bedrock.credential_ref))
            self.assertTrue(self.rt.credentials.has_retired(keep=self.rt.cfg.bedrock.credential_ref))

    def test_auth_switch_preserves_key_and_legacy_config_loads_without_new_field(self):
        self.rt.configure(self.candidate(), credentials=keys())
        ref = self.rt.cfg.bedrock.credential_ref; cfg = copy.deepcopy(self.rt.cfg); cfg.bedrock.auth = "sigv4"
        self.rt.configure(cfg); self.assertEqual(self.rt.credentials.read(ref), keys())
        legacy = asdict(Settings()); legacy["bedrock"].pop("credential_ref")
        (self.home / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(load(self.home).bedrock.credential_ref, "")

    def test_another_process_settings_lock_and_stale_key_reference_cannot_remove_new_key(self):
        with self.rt.credentials.settings_lock():
            with self.assertRaises(AssistantError) as error: self.rt.configure(self.candidate(), credentials=keys())
            self.assertEqual(error.exception.code, "SETTINGS_BUSY")
        self.rt.configure(self.candidate(), credentials=keys())
        other = Runtime(self.home); other.credentials.protector = self.rt.credentials.protector
        try:
            stale = copy.deepcopy(other.cfg)
            self.rt.configure(self.candidate(), credentials=keys("B")); current = self.rt.cfg.bedrock.credential_ref
            with self.assertRaises(AssistantError) as error: other.configure(stale, credentials=keys("C"))
            self.assertEqual(error.exception.code, "SETTINGS_CHANGED")
            self.assertEqual(load(self.home).bedrock.credential_ref, current)
            self.assertEqual(self.rt.credentials.read(current), keys("B"))
        finally: other.close()

    def test_key_and_session_token_validation_never_echoes_input(self):
        cases = [(AccessKeys("bad-secret-canary", "A" * 40), "AWS_ACCESS_KEY_ID_INVALID"),
                 (AccessKeys(keys().access_key_id, "bad-secret-canary"), "AWS_SECRET_ACCESS_KEY_INVALID"),
                 (AccessKeys(keys(temporary=True).access_key_id, "A" * 40), "AWS_SESSION_TOKEN_REQUIRED"),
                 (AccessKeys(keys().access_key_id, "A" * 40, "bad-secret-canary\n"), "AWS_SESSION_TOKEN_INVALID")]
        for material, code in cases:
            with self.assertRaises(AssistantError) as error: material.validate()
            self.assertEqual(error.exception.code, code); self.assertNotIn("bad-secret-canary", str(error.exception))

    @unittest.skipIf(os.name == "nt", "Symlink creation requires extra Windows privileges")
    def test_dangling_lock_and_vault_links_are_rejected_before_any_file_write(self):
        outside = self.home / "outside-target"
        (self.home / ".settings.lock").symlink_to(outside)
        with self.assertRaises(AssistantError) as error:
            with self.rt.credentials.settings_lock(): pass
        self.assertEqual(error.exception.code, "CREDENTIAL_PATH_INVALID"); self.assertFalse(outside.exists())
        (self.home / "credentials").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(AssistantError) as error: self.rt.credentials.save(keys())
        self.assertEqual(error.exception.code, "CREDENTIAL_PATH_INVALID"); self.assertFalse(outside.exists())

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
    def test_real_windows_dpapi_roundtrip_and_entropy_rejection(self):
        store = CredentialStore(self.home)
        material = AccessKeys(keys(temporary=True).access_key_id, "A" * 40, "synthetic-token-" + "T" * 5000)
        ref = store.save(material)
        self.assertEqual(CredentialStore(self.home).read(ref), material)
        encrypted = (self.home / "credentials" / (ref + ".bin")).read_bytes()
        self.assertNotIn(material.secret_access_key.encode(), encrypted)
        if importlib.util.find_spec("botocore"):
            client = BedrockClient(BedrockSettings(auth="access_keys", credential_ref=ref), credential_store=CredentialStore(self.home))
            headers = client._headers("https://bedrock.ap-northeast-1.amazonaws.com/foundation-models", None, "bedrock", "GET")
            self.assertIn(material.access_key_id, headers["Authorization"])
            self.assertEqual(headers["X-Amz-Security-Token"], material.session_token)
        with self.assertRaises(AssistantError): WindowsProtector().unprotect(encrypted, b"wrong-application-entropy")
        store.delete(ref); self.assertFalse((self.home / "credentials" / (ref + ".bin")).exists())

    @unittest.skipIf(os.name == "nt", "Unsupported persistence path applies to non-Windows")
    def test_non_windows_persistence_fails_explicitly(self):
        with self.assertRaises(AssistantError) as error: CredentialStore(self.home).save(keys())
        self.assertEqual(error.exception.code, "CREDENTIAL_STORAGE_UNSUPPORTED")
        self.assertFalse((self.home / "credentials").exists())


@unittest.skipUnless(importlib.util.find_spec("botocore"), "Optional sigv4 dependency; installed in CI")
class SigningTests(unittest.TestCase):
    def settings(self, **values):
        return BedrockSettings(auth="access_keys", profile="must-not-be-used", **values)

    def test_explicit_keys_sign_get_and_post_without_using_ambient_identity(self):
        material = keys(temporary=True)
        client = BedrockClient(self.settings(), credentials=material)
        for method, url, body in (("GET", "https://bedrock.ap-northeast-1.amazonaws.com/inference-profiles?nextToken=a%2Bb%2F&maxResults=100", None),
                                  ("POST", "https://bedrock-runtime.ap-northeast-1.amazonaws.com/model/test/converse", b'{"messages":[]}')):
            with patch("boto3.Session", side_effect=AssertionError("Ambient identity must not be queried")):
                headers = client._headers(url, body, "bedrock", method)
            self.assertEqual(headers["X-Amz-Security-Token"], material.session_token)
            auth = headers["Authorization"]
            scope = auth.split("Credential=", 1)[1].split(",", 1)[0].split("/", 1)[1]
            self.assertIn("Credential=" + material.access_key_id + "/", auth)
            names = auth.split("SignedHeaders=", 1)[1].split(",", 1)[0]
            lowered = {k.lower(): v for k, v in headers.items()}; lowered["host"] = urlsplit(url).netloc
            canonical_headers = "".join(n + ":" + lowered[n].strip() + "\n" for n in names.split(";"))
            query = "&".join(quote(k, safe="-_.~") + "=" + quote(v, safe="-_.~") for k, v in sorted(parse_qsl(urlsplit(url).query)))
            request = "\n".join((method, urlsplit(url).path, query, canonical_headers, names, hashlib.sha256(body or b"").hexdigest()))
            string_to_sign = "AWS4-HMAC-SHA256\n" + headers["X-Amz-Date"] + "\n" + scope + "\n" + hashlib.sha256(request.encode()).hexdigest()
            derived = ("AWS4" + material.secret_access_key).encode()
            for part in scope.split("/"): derived = hmac.new(derived, part.encode(), hashlib.sha256).digest()
            expected = hmac.new(derived, string_to_sign.encode(), hashlib.sha256).hexdigest()
            self.assertEqual(auth.split("Signature=", 1)[1], expected)
            self.assertNotIn(material.secret_access_key, canonical(headers))

    def test_saved_keys_reload_and_missing_or_corrupt_key_never_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CredentialStore(Path(tmp), protector=MemoryProtector()); ref = store.save(keys())
            client = BedrockClient(self.settings(credential_ref=ref), credential_store=store)
            with patch("boto3.Session", side_effect=AssertionError("No fallback")):
                self.assertIn(keys().access_key_id, client._headers("https://bedrock.ap-northeast-1.amazonaws.com/foundation-models", None, "bedrock", "GET")["Authorization"])
                store.delete(ref)
                with self.assertRaises(AssistantError) as error: client._headers("https://bedrock.ap-northeast-1.amazonaws.com/foundation-models", None, "bedrock", "GET")
            self.assertEqual(error.exception.code, "AWS_ACCESS_KEYS_MISSING")

    def test_claude_and_responses_both_use_key_pair_signing(self):
        from test_bedrock_models import Opener, Wire, response
        for model, endpoint in (("anthropic.claude-sonnet-4-6", "https://bedrock-runtime.ap-northeast-1.amazonaws.com"),
                                ("global.openai.gpt-6-luna", "https://bedrock-runtime.ap-northeast-1.amazonaws.com/openai/v1")):
            result = response() if "anthropic" in model else {"status": "completed", "output": []}
            opener = Opener(lambda _: Wire(result))
            settings = self.settings(model=model, endpoint=endpoint, enabled=True, data_transfer_approved=True, cross_region_approved=True, streaming=False)
            BedrockClient(settings, opener, credentials=keys()).create([{"role": "user", "content": "synthetic"}], "test", [])
            self.assertIn("AWS4-HMAC-SHA256", opener.requests[0].get_header("Authorization"))
            self.assertNotIn(keys().secret_access_key.encode(), opener.requests[0].data)


if __name__ == "__main__": unittest.main()
