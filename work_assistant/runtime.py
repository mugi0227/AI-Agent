"""Local lifetime owner. Background work yields between model calls to human requests."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .agent import Agent
from .bedrock import BedrockClient
from .bridge import Bridge
from .common import AssistantError, safe_code
from .config import Settings, load
from .ingest import Ingestor
from .scheduler import Scheduler
from .store import Store


class Runtime:
    def __init__(self, home: Path, cfg: Settings | None = None, client=None):
        self.home, self.cfg = home, cfg or load(home)
        self.store = Store(home / "assistant.sqlite3")
        self.ingestor = Ingestor(self.store, self.cfg, home)
        self.bridge = Bridge(self.store, self.cfg, self.ingestor)
        self.agent = Agent(self.store, self.cfg, self.ingestor, self.bridge, client or BedrockClient(self.cfg.bedrock))
        self.scheduler = Scheduler(self.store, self.cfg, self.agent)
        self.stop_event, self.background_cancel, self.foreground = threading.Event(), threading.Event(), threading.Event()
        self.chat_lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.status = "起動済み（承認・設定が未完了なら外部通信しません）"

    def scan(self) -> dict:
        self.store.expire()
        result = {"files": self.ingestor.scan_files(), "drops": self.ingestor.scan_drops(self.bridge.receive)}
        if self.cfg.bridge_enabled:
            try:
                result["requests_written"] = self.bridge.flush()
            except AssistantError as error:
                result["bridge_error"] = error.code
        self.store.set_state("last_scan", result)
        return result

    def start(self) -> None:
        if self.threads:
            return
        for fn in (self._ingest_loop, self._monitor_loop):
            worker = threading.Thread(target=fn, daemon=True)
            self.threads.append(worker)
            worker.start()

    def _ingest_loop(self) -> None:
        next_scan = 0.0
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= next_scan:
                    self.scan()
                    next_scan = time.monotonic() + self.cfg.scan_interval_seconds
                else:
                    self.ingestor.scan_drops(self.bridge.receive)
            except Exception as error:
                self.status = "収集エラー: " + safe_code(error)
            self.stop_event.wait(1)

    def _monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            if not self.foreground.is_set():
                self.background_cancel.clear()
                # Recheck after clearing to avoid discarding a concurrent foreground cancellation.
                if self.foreground.is_set():
                    self.background_cancel.set()
                else:
                    try:
                        self.scheduler.tick(cancel=self.background_cancel)
                    except Exception as error:
                        self.status = "定期確認エラー: " + safe_code(error)
            self.stop_event.wait(5)

    def ask(self, text: str, **kwargs):
        if not self.chat_lock.acquire(blocking=False):
            raise AssistantError("FOREGROUND_BUSY")
        self.foreground.set()
        self.background_cancel.set()
        try:
            return self.agent.turn(text, **kwargs)
        finally:
            self.foreground.clear()
            self.chat_lock.release()

    def close(self) -> None:
        self.stop_event.set()
        self.background_cancel.set()
        # No join on the UI thread: outstanding HTTP requests have a finite timeout.
