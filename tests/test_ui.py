"""Opt-in real Tk operations. Run under Xvfb on Linux or on a Windows desktop."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from work_assistant.__main__ import synthetic_envelope
from work_assistant.bedrock import ScriptedClient
from work_assistant.config import Settings, initialize
from work_assistant.runtime import Runtime


def message(text="回答"):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]}


@unittest.skipUnless(os.environ.get("WORK_ASSISTANT_UI_TESTS") == "1", "Real Tk checks are enabled in the separate UI job")
class DesktopTests(unittest.TestCase):
    def setUp(self):
        from work_assistant.ui import Desktop
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name); initialize(self.home)
        self.rt = Runtime(self.home, Settings(local_data_approved=True), ScriptedClient([message("日本語の回答")]))
        self.app = Desktop(self.rt, start_runtime=False)
        self.callback_errors = []
        self.app.root.report_callback_exception = lambda kind, error, trace: self.callback_errors.append(str(error))
        self.wait(lambda: not self.app.loading)

    def wait(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.root.update()
            if condition():
                self.assertFalse(self.callback_errors, self.callback_errors)
                return
            time.sleep(.01)
        self.fail("UI did not reach the expected state")

    def settle(self, duration=.2):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.app.root.update(); time.sleep(.01)
        self.assertFalse(self.callback_errors, self.callback_errors)

    def tearDown(self):
        if not self.app.closed: self.app.close()
        self.tmp.cleanup()
        self.assertFalse(self.callback_errors, self.callback_errors)

    def test_multiline_ime_friendly_input_and_keyboard_send(self):
        self.app.root.focus_force(); self.app.input.focus_set()
        self.app._set_input("日本語の質問")
        self.app.input.mark_set("insert", "end-1c")
        self.app.input.event_generate("<Return>")
        self.settle(.05)
        self.assertIn("\n", self.app.input.get("1.0", "end-1c"))
        self.assertIsNone(self.app.controller.active)
        self.app.input.event_generate("<Control-Return>")
        self.wait(lambda: not self.app.loading and bool(self.app.message_rows))
        self.assertEqual([r["role"] for r in self.app.message_rows], ["user", "assistant"])
        self.assertEqual(self.app.message_rows[-1]["text"], "日本語の回答")

    def test_drafts_and_attachments_follow_their_session(self):
        self.rt.ingestor.envelope(synthetic_envelope())
        did = self.rt.store.documents()[0]["id"]
        first = self.app.current_session
        self.app._set_input("下書きA\n確認事項"); self.app.attached = [did]
        self.app.new_session(); self.wait(lambda: not self.app.loading)
        second = self.app.current_session
        self.app._set_input("下書きB"); self.app.save_draft()
        self.app.load_session(first); self.wait(lambda: not self.app.loading)
        self.assertEqual(self.app.input.get("1.0", "end-1c"), "下書きA\n確認事項")
        self.assertEqual(self.app.attached, [did])
        self.app.load_session(second); self.wait(lambda: not self.app.loading)
        self.assertEqual(self.app.input.get("1.0", "end-1c"), "下書きB")
        self.assertFalse(self.app.attached)

    def test_restart_restores_history_and_unsent_draft(self):
        from work_assistant.ui import Desktop
        self.app._set_input("前の質問"); self.app.ask()
        self.wait(lambda: not self.app.loading and len(self.app.message_rows) == 2)
        self.app._set_input("送信前の下書き")
        sid = self.app.current_session
        self.app.close()
        self.app = Desktop(self.rt, start_runtime=False)
        self.app.root.report_callback_exception = lambda kind, error, trace: self.callback_errors.append(str(error))
        self.wait(lambda: not self.app.loading)
        self.assertEqual(self.app.current_session, sid)
        self.assertEqual(len(self.app.message_rows), 2)
        self.assertEqual(self.app.input.get("1.0", "end-1c"), "送信前の下書き")

    def test_document_search_attach_and_read_source(self):
        self.rt.ingestor.envelope(synthetic_envelope())
        self.app.show_page("documents")
        self.wait(lambda: bool(self.app.document_rows))
        did = next(iter(self.app.document_rows))
        self.app.document_list.selection_set(did)
        self.app.attach_search_selection()
        self.assertEqual(self.app.current_page, "chat"); self.assertEqual(self.app.attached, [did])
        self.app.open_document(did)
        self.wait(lambda: any(w.winfo_class() == "Toplevel" for w in self.app.root.winfo_children()))

    def test_picker_can_attach_without_leaving_chat(self):
        from tkinter import ttk
        self.rt.ingestor.envelope(synthetic_envelope())
        did = self.rt.store.documents()[0]["id"]
        self.app.choose_documents(); self.settle(.05)
        dialog = next(w for w in self.app.root.winfo_children() if w.winfo_class() == "Toplevel")
        tree = next(w for w in dialog.winfo_children() if isinstance(w, ttk.Treeview))
        tree.selection_set(did)
        choose = next(w for w in dialog.winfo_children() if w.winfo_class() == "Button")
        choose.invoke(); self.settle(.05)
        self.assertEqual(self.app.attached, [did]); self.assertEqual(self.app.current_page, "chat")

    def test_failed_request_can_retry_from_the_screen(self):
        self.rt.agent.client = ScriptedClient([])
        self.app._set_input("再試行する質問"); self.app.ask()
        self.wait(lambda: not self.app.loading and bool(self.app.message_rows) and self.app.message_rows[-1]["status"] == "failed")
        self.rt.agent.client = ScriptedClient([message("成功した回答")])
        self.app.retry(self.app.message_rows[-1])
        self.wait(lambda: not self.app.loading and len(self.app.message_rows) == 2)
        self.assertEqual(self.app.message_rows[-1]["text"], "成功した回答")
        self.assertEqual(sum(r["role"] == "user" for r in self.app.message_rows), 1)

    def test_switching_chat_while_streaming_does_not_mix_answers(self):
        started, release = threading.Event(), threading.Event()
        class Client:
            is_live = False
            def create(inner, items, instructions, tools, on_delta):
                on_delta("途中の応答"); started.set(); release.wait(5); on_delta("です")
                return message("元の会話の回答")
        self.rt.agent.client = Client()
        first = self.app.current_session
        self.app._set_input("最初の会話の質問"); self.app.ask()
        self.wait(started.is_set)
        self.wait(lambda: self.app.pending_card and "途中" in self.app.pending_card.raw_text)
        self.app.new_session(); self.wait(lambda: not self.app.loading)
        second = self.app.current_session
        self.app._set_input("別の下書き")
        release.set(); self.wait(lambda: self.app.controller.active is None)
        self.assertEqual(self.app.current_session, second); self.assertFalse(self.app.message_rows)
        self.assertEqual(self.app.input.get("1.0", "end-1c"), "別の下書き")
        self.app.load_session(first); self.wait(lambda: not self.app.loading)
        self.assertEqual(self.app.message_rows[-1]["text"], "元の会話の回答")

    def test_stop_button_clears_provisional_response_and_allows_retry(self):
        started, release = threading.Event(), threading.Event()
        class Client:
            is_live = False
            def create(inner, items, instructions, tools, on_delta):
                on_delta("保存しない途中出力"); started.set(); release.wait(5); on_delta("続き")
                return message("完了")
        self.rt.agent.client = Client()
        self.app._set_input("止める質問"); self.app.ask(); self.wait(started.is_set)
        self.app.stop_button.invoke(); release.set()
        self.wait(lambda: not self.app.loading and bool(self.app.message_rows) and self.app.message_rows[-1]["status"] == "cancelled")
        self.assertEqual(len(self.app.message_rows), 1)
        self.assertEqual(self.app.input.get("1.0", "end-1c"), "止める質問")
        self.assertEqual(str(self.app.stop_button["state"]), "disabled")

    def test_six_pages_small_window_and_markdown_message_remain_usable(self):
        from work_assistant.ui_widgets import MessageCard
        self.app.root.geometry("960x700")
        self.rt.store.rename_session(self.app.current_session, "長い会話タイトル" * 10)
        for page in self.app.pages:
            self.app.show_page(page); self.settle(.04)
            self.assertTrue(self.app.pages[page].winfo_ismapped())
        self.app.show_page("chat")
        self.app.message_rows = []
        self.app.thread.clear()
        text = "## 確認事項\n\n**太字**の要点\n- 条件の確認\n- 次の会議\n\n```text\n安全なテキスト\n```\n最後の行"
        card = MessageCard(self.app.thread.inner, text=text, on_copy=self.app.copy)
        self.settle(.3)
        self.assertIsNotNone(card.body.bbox("end-1c"))
        self.assertIn("最後の行", card.body.get("1.0", "end"))
        self.assertGreaterEqual(self.app.input.winfo_height(), 40)
        self.assertEqual(len(self.app.pages), 6)
        self.assertTrue(self.app.connection_badge.winfo_ismapped())
        self.assertLessEqual(self.app.connection_badge.winfo_rootx() + self.app.connection_badge.winfo_width(), self.app.root.winfo_rootx() + self.app.root.winfo_width())

    def test_revoked_source_does_not_restore_old_draft_on_refresh(self):
        self.rt.ingestor.envelope(synthetic_envelope())
        did = self.rt.store.documents()[0]["id"]
        self.app._set_input("失効する資料を引用した下書き"); self.app.attached = [did]; self.app.save_draft()
        self.rt.store.invalidate(did)
        self.app.refresh(schedule=False); self.wait(lambda: not self.app.loading)
        self.assertEqual(self.app.input.get("1.0", "end-1c"), "")
        self.assertFalse(self.app.attached)

    def test_archive_restore_search_and_copy_use_saved_data(self):
        sid = self.app.current_session
        self.rt.store.rename_session(sid, "会議の準備")
        self.rt.store.remember_turn(sid, "user", "塗工条件の確認")
        self.app.session_filter.set("塗工条件"); self.app.refresh_sessions()
        self.assertEqual(self.app.session_rows[0]["id"], sid)
        self.app.archive_current(); self.wait(lambda: not self.app.loading)
        self.app.show_archived.set(True); self.app.refresh_sessions()
        self.assertIn(sid, [r["id"] for r in self.app.session_rows])
        self.app.load_session(sid); self.wait(lambda: not self.app.loading)
        self.app.archive_current(); self.wait(lambda: not self.app.loading)
        self.assertFalse(self.rt.store.session(sid)["archived"])
        self.app.copy("コピーした日本語"); self.assertEqual(self.app.root.clipboard_get(), "コピーした日本語")

    def test_offline_diagnosis_is_readable_and_does_not_call_model(self):
        self.app.show_page("settings"); self.app.diagnose()
        self.wait(lambda: "sqlite_fts5" in self.app.info_view.get("1.0", "end"))
        self.assertFalse(self.rt.agent.client.calls)

    def test_old_history_event_cannot_redraw_revoked_data(self):
        self.rt.ingestor.envelope(synthetic_envelope())
        did = self.rt.store.documents()[0]["id"]
        sid = self.app.current_session
        self.rt.store.remember_turn(sid, "assistant", "失効する回答")
        old = self.rt.store.session(sid), self.rt.store.messages(sid)
        self.rt.store.invalidate(did)
        self.app.events.put(("history", (self.app.load_version, sid), old))
        self.settle(.2); self.wait(lambda: not self.app.loading)
        self.assertFalse(self.app.message_rows)

    def test_reading_position_survives_stream_completion(self):
        started, release = threading.Event(), threading.Event()
        class Client:
            is_live = False
            def create(inner, items, instructions, tools, on_delta):
                on_delta("新しい回答"); started.set(); release.wait(5)
                return message("新しい確定回答")
        self.rt.agent.client = Client()
        for i in range(24):
            self.rt.store.remember_turn(self.app.current_session, "user" if i % 2 == 0 else "assistant", f"以前の会話 {i}\n" + "確認事項です。" * 30)
        self.app.load_session(self.app.current_session); self.wait(lambda: not self.app.loading)
        self.app._set_input("続きの質問"); self.app.ask(); self.wait(started.is_set)
        self.app.thread.canvas.yview_moveto(0); self.settle(.1)
        self.assertFalse(self.app.thread.follow)
        release.set(); self.wait(lambda: not self.app.loading and len(self.app.message_rows) == 26)
        self.settle(.1)
        self.assertLess(self.app.thread.canvas.yview()[0], .1)


if __name__ == "__main__": unittest.main()
