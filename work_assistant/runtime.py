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
from .execution import WorkRunner, CombinedCancel, ForegroundLease


class Runtime:
    def __init__(self, home: Path, cfg: Settings | None = None, client=None):
        self.home, self.cfg = home, cfg or load(home)
        self.store = Store(home / "assistant.sqlite3")
        self.store.recover_interrupted()
        self.ingestor = Ingestor(self.store, self.cfg, home)
        self.bridge = Bridge(self.store, self.cfg, self.ingestor)
        self.agent = Agent(self.store, self.cfg, self.ingestor, self.bridge, client or BedrockClient(self.cfg.bedrock))
        self.work = self.agent.state
        self.work_runner = WorkRunner(self.work, self.cfg, self.agent)
        self.scheduler = Scheduler(self.store, self.cfg, self.agent)
        self.stop_event, self.background_cancel, self.foreground = threading.Event(), threading.Event(), threading.Event()
        self.chat_lock = threading.Lock()
        self.active_cancel: threading.Event | None = None
        self.foreground_lease = None
        self.wake = threading.Event()
        self.threads: list[threading.Thread] = []
        self.status = "起動済み（承認・設定が未完了なら外部通信しません）"

    def scan(self) -> dict:
        self.store.expire()
        self.store.recover_interrupted()
        self.work.prune(self.cfg.retention_days)
        self.agent.memory.prune()
        result = {"files": self.ingestor.scan_files(), "drops": self.ingestor.scan_drops(self.bridge.receive)}
        if self.cfg.bridge_enabled:
            try:
                result["requests_written"] = self.bridge.flush()
            except AssistantError as error:
                result["bridge_error"] = error.code
        self.store.set_state("last_scan", result)
        self.wake.set()
        return result

    def start(self) -> None:
        if self.threads:
            return
        for fn in (self._ingest_loop, self._monitor_loop, *([self._work_loop] * self.cfg.background_workers)):
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
                    self.wake.set()
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

    def _work_loop(self):
        while not self.stop_event.is_set():
            if not self.foreground.is_set() and (self.cfg.durable_work_enabled or self.cfg.heartbeat_enabled):
                try:
                    if self.cfg.durable_work_enabled: self.work.schedule()
                    self.background_cancel.clear()
                    if not self.foreground.is_set():
                        outcome = self.work_runner.run_one(cancel=CombinedCancel(self.stop_event, self.background_cancel),
                            allowed_kinds=None if self.cfg.durable_work_enabled else {"monitor"})
                        if outcome["status"] not in ("idle", "skipped", "disabled"): continue
                except Exception as error: self.status = "継続タスクの処理エラー: " + safe_code(error)
            self.wake.wait(1); self.wake.clear()

    def ask(self, text: str, **kwargs):
        if not self.chat_lock.acquire(blocking=False):
            raise AssistantError("FOREGROUND_BUSY")
        self.foreground.set()
        self.background_cancel.set()
        self.active_cancel = kwargs.pop("cancel", None) or threading.Event()
        try:
            lease = ForegroundLease(self.work, "main", kwargs.get("session", "main"), self.cfg.background_workers + 1)
            self.foreground_lease = lease
            with lease:
                return self.agent.turn(text, cancel=self.active_cancel, **kwargs)
        finally:
            self.foreground_lease = None
            self.active_cancel = None
            self.foreground.clear()
            self.chat_lock.release()

    def close(self) -> None:
        self.stop_event.set()
        self.background_cancel.set()
        if self.active_cancel:
            self.active_cancel.set()
        if self.foreground_lease:
            self.foreground_lease.release()
        self.wake.set()
        # No join on the UI thread: outstanding HTTP requests have a finite timeout.
