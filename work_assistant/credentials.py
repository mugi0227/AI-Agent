"""AWS key pairs, isolated from Settings, transcripts and diagnostic payloads.

Only DPAPI ciphertext is written to disk. Decryption is scoped to the current
Windows account; there is no plaintext or cross-platform persistence fallback.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .common import AssistantError, canonical
from .security import is_link_or_junction


@dataclass(frozen=True)
class AccessKeys:
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str = field(default="", repr=False)

    def validate(self):
        if not isinstance(self.access_key_id, str) or not re.fullmatch(r"[A-Z0-9]{16,128}", self.access_key_id):
            raise AssistantError("AWS_ACCESS_KEY_ID_INVALID")
        if not isinstance(self.secret_access_key, str) or not re.fullmatch(r"[A-Za-z0-9/+=]{40}", self.secret_access_key):
            raise AssistantError("AWS_SECRET_ACCESS_KEY_INVALID")
        if not isinstance(self.session_token, str) or len(self.session_token) > 16384 or any(not 33 <= ord(c) <= 126 for c in self.session_token):
            raise AssistantError("AWS_SESSION_TOKEN_INVALID")
        if self.access_key_id.startswith("ASIA") and not self.session_token:
            raise AssistantError("AWS_SESSION_TOKEN_REQUIRED")


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsProtector:
    def _convert(self, data: bytes, entropy: bytes, decrypt=False) -> bytes:
        if os.name != "nt": raise AssistantError("CREDENTIAL_STORAGE_UNSUPPORTED")
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        protect = crypt.CryptProtectData
        protect.argtypes = [ctypes.POINTER(_Blob), ctypes.c_wchar_p, ctypes.POINTER(_Blob),
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
        protect.restype = ctypes.c_int
        unprotect = crypt.CryptUnprotectData
        unprotect.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
        unprotect.restype = ctypes.c_int
        kernel.LocalFree.argtypes = [ctypes.c_void_p]; kernel.LocalFree.restype = ctypes.c_void_p
        source_buffer, entropy_buffer = ctypes.create_string_buffer(data), ctypes.create_string_buffer(entropy)
        source = _Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        extra = _Blob(len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = _Blob()
        # CRYPTPROTECT_UI_FORBIDDEN; deliberately omit LOCAL_MACHINE.
        ok = unprotect(ctypes.byref(source), None, ctypes.byref(extra), None, None, 1, ctypes.byref(target)) if decrypt else protect(ctypes.byref(source), "Work Assistant AWS credentials", ctypes.byref(extra), None, None, 1, ctypes.byref(target))
        if not ok: raise AssistantError("CREDENTIAL_DECRYPT_FAILED" if decrypt else "CREDENTIAL_STORE_FAILED")
        try: return ctypes.string_at(target.data, target.size)
        finally: kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))

    def protect(self, data, entropy): return self._convert(data, entropy)
    def unprotect(self, data, entropy): return self._convert(data, entropy, True)


class CredentialStore:
    def __init__(self, home: Path, *, protector=None):
        self.folder = home / "credentials"
        self.protector = protector or WindowsProtector()

    def _path(self, ref, *, create=False):
        if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{32}", ref):
            raise AssistantError("CREDENTIAL_REFERENCE_INVALID")
        try:
            for part in (self.folder, *self.folder.parents):
                try:
                    if is_link_or_junction(part): raise AssistantError("CREDENTIAL_PATH_INVALID")
                except FileNotFoundError: pass
            if create:
                self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self.folder / (ref + ".bin")
            if path.is_symlink() or path.exists() and is_link_or_junction(path): raise AssistantError("CREDENTIAL_PATH_INVALID")
            return path
        except OSError:
            raise AssistantError("CREDENTIAL_STORE_FAILED") from None

    @contextmanager
    def settings_lock(self):
        """Serialize config/ciphertext commits across local app processes."""
        path = self.folder.parent / ".settings.lock"
        try:
            for part in (path, *path.parents):
                try:
                    if is_link_or_junction(part): raise AssistantError("CREDENTIAL_PATH_INVALID")
                except FileNotFoundError: pass
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            raise AssistantError("CREDENTIAL_STORE_FAILED") from None
        with os.fdopen(fd, "r+b") as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0"); handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise AssistantError("SETTINGS_BUSY") from None
            try: yield
            finally:
                handle.seek(0)
                if os.name == "nt": msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _entropy(ref): return ("WorkAssistant.AWS.v1:" + ref).encode("ascii")

    def save(self, keys: AccessKeys) -> str:
        keys.validate()
        ref = uuid.uuid4().hex
        payload = canonical({"version": 1, "access_key_id": keys.access_key_id,
                             "secret_access_key": keys.secret_access_key, "session_token": keys.session_token}).encode("utf-8")
        encrypted = self.protector.protect(payload, self._entropy(ref))
        if not isinstance(encrypted, bytes) or not encrypted or len(encrypted) > 65536:
            raise AssistantError("CREDENTIAL_STORE_FAILED")
        path = self._path(ref, create=True)
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".partial-", dir=self.folder)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypted); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            raise AssistantError("CREDENTIAL_STORE_FAILED") from None
        finally:
            if temporary and os.path.exists(temporary): os.unlink(temporary)
        return ref

    def read(self, ref: str) -> AccessKeys:
        path = self._path(ref)
        try:
            with path.open("rb") as stream: encrypted = stream.read(65537)
            if len(encrypted) > 65536: raise AssistantError("CREDENTIAL_DECRYPT_FAILED")
            raw = json.loads(self.protector.unprotect(encrypted, self._entropy(ref)).decode("utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"version", "access_key_id", "secret_access_key", "session_token"} or raw["version"] != 1:
                raise AssistantError("CREDENTIAL_DECRYPT_FAILED")
            keys = AccessKeys(raw["access_key_id"], raw["secret_access_key"], raw["session_token"])
            keys.validate(); return keys
        except FileNotFoundError:
            raise AssistantError("AWS_ACCESS_KEYS_MISSING") from None
        except (OSError, ValueError, UnicodeError, TypeError):
            raise AssistantError("CREDENTIAL_DECRYPT_FAILED") from None

    def delete(self, ref: str):
        try: self._path(ref).unlink(missing_ok=True)
        except OSError: raise AssistantError("CREDENTIAL_DELETE_FAILED") from None

    def prune(self, keep="") -> bool:
        """Retry removal of retired/orphaned ciphertext after a settings commit."""
        ok = True
        try:
            for path in self.folder.iterdir():
                if path.suffix == ".bin" and path.stem != keep and re.fullmatch(r"[0-9a-f]{32}", path.stem):
                    try: self.delete(path.stem)
                    except AssistantError: ok = False
        except FileNotFoundError: pass
        except OSError: ok = False
        return ok

    def has_retired(self, keep="") -> bool:
        try:
            return any(p.suffix == ".bin" and p.stem != keep and re.fullmatch(r"[0-9a-f]{32}", p.stem) for p in self.folder.iterdir())
        except FileNotFoundError: return False
        except OSError: return True
