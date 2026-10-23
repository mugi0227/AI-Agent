"""Opt-in real Tk operations. Run under Xvfb on Linux or on a Windows desktop."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from work_assistant.__main__ import synthetic_envelope
from work_assistant.bedrock import ScriptedClient
from work_assistant.config import Settings, initialize
from work_assistant.runtime import Runtime


def message(text="回答"):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]}


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def action(widget, text):
    return next(w for w in descendants(widget) if w.winfo_class() == "Button" and w["text"] == text)


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
        tree = next(w for w in descendants(dialog) if isinstance(w, ttk.Treeview))
        tree.selection_set(did)
        choose = action(dialog, "この資料を添える")
        self.app.root.update()
        self.assertTrue(choose.winfo_ismapped())
        tree.event_generate("<<TreeviewSelect>>")
        self.settle(.05)
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

    def test_seven_pages_small_window_and_markdown_message_remain_usable(self):
        from work_assistant.ui_widgets import MessageCard
        self.app.root.geometry("960x700")
        self.rt.store.rename_session(self.app.current_session, "長い会話タイトル" * 10)
        for page in self.app.pages:
            self.app.show_page(page); self.settle(.04)
            self.assertTrue(self.app.pages[page].winfo_ismapped())
            if page == "work":
                self.assertTrue(self.app.work_panel.feedback.winfo_ismapped())
                self.assertLessEqual(self.app.work_panel.feedback.winfo_rooty()+self.app.work_panel.feedback.winfo_height(),self.app.root.winfo_rooty()+self.app.root.winfo_height())
        self.app.show_page("chat")
        self.app.message_rows = []
        self.app.thread.clear()
        text = "## 確認事項\n\n**太字**の要点\n- 条件の確認\n- 次の会議\n\n```text\n安全なテキスト\n```\n最後の行"
        card = MessageCard(self.app.thread.inner, text=text, on_copy=self.app.copy)
        self.settle(.3)
        self.assertIsNotNone(card.body.bbox("end-1c"))
        self.assertIn("最後の行", card.body.get("1.0", "end"))
        self.assertGreaterEqual(self.app.input.winfo_height(), 40)
        self.assertEqual(len(self.app.pages), 7)
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

    def test_composer_registers_durable_work_and_manual_run_displays_result(self):
        self.app.input.insert("1.0", "会議の準備を継続する")
        self.wait(lambda: str(self.app.register_work["state"]) == "normal")
        self.app.register_work.invoke()
        self.wait(lambda: self.app.current_page == "work" and bool(self.app.work_panel.rows))
        jid = self.app.work_panel.selected()
        self.assertEqual(self.rt.work.job(jid)["goal"], "会議の準備を継続する")
        self.assertEqual(self.app.input.get("1.0", "end-1c"), "")
        self.app.work_panel.run_selected()
        self.wait(lambda: self.rt.work.job(jid)["status"] == "completed" and "日本語の回答" in self.app.work_panel.detail.get("1.0", "end"))
        self.app.work_panel.rate(True)
        self.assertEqual(self.rt.work.metrics()["rated_helpful_rate"], 1)

    def test_waiting_work_is_visible_and_can_be_cancelled(self):
        from datetime import timedelta
        from work_assistant.common import now_utc, stamp, canonical
        response = {"status": "completed", "output": [{"type": "function_call", "call_id": "wait", "name": "wait_until", "arguments": canonical({"at": stamp(now_utc() + timedelta(hours=1))})}]}
        self.rt.agent.client = ScriptedClient([response])
        jid = self.app.work_panel.register("指定時刻まで待つ")
        self.app.work_panel.run_selected()
        self.wait(lambda: self.rt.work.job(jid)["status"] == "waiting" and "待機条件" in self.app.work_panel.detail.get("1.0", "end"))
        self.app.work_panel.change("cancel")
        self.assertEqual(self.rt.work.job(jid)["status"], "cancelled")

    def test_saved_memory_can_be_added_and_removed_from_its_dialog(self):
        self.app.work_panel.memories(); self.settle(.05)
        dialog = next(w for w in self.app.root.winfo_children() if w.winfo_class() == "Toplevel")
        tree = next(w for w in descendants(dialog) if w.winfo_class() == "Treeview")
        add, remove = action(dialog, "記憶を追加"), action(dialog, "選択した記憶を削除")
        with patch("work_assistant.ui_work.simpledialog.askstring", return_value="会議前に測定条件を確認する"):
            add.invoke()
        self.wait(lambda: len(tree.get_children()) == 1)
        tree.selection_set(tree.get_children()[0]); remove.invoke()
        self.assertFalse(tree.get_children())

    def test_routine_dialog_can_enable_a_saved_schedule(self):
        rid = self.rt.work.add_routine("定期確認", "資料を確認", interval_seconds=3600)
        self.app.work_panel.routines(); self.settle(.05)
        dialog = next(w for w in self.app.root.winfo_children() if w.winfo_class() == "Toplevel")
        tree = next(w for w in descendants(dialog) if w.winfo_class() == "Treeview")
        tree.selection_set(rid)
        action(dialog, "選択した定期実行の有効・無効を変更").invoke()
        self.assertTrue(self.rt.work.routines()[0]["enabled"])

    def restart_unconfigured(self):
        from work_assistant.ui import Desktop
        self.app.close()
        self.rt = Runtime(self.home)
        self.app = Desktop(self.rt, start_runtime=False)
        self.app.root.report_callback_exception = lambda kind, error, trace: self.callback_errors.append(str(error))
        self.wait(lambda: not self.app.loading)
        return self.app.settings_panel

    def permit_connection(self, panel):
        for field in ("local_data_approved", "enabled", "data_transfer_approved", "cross_region_approved"):
            panel.vars[field].set(True)

    def test_first_run_opens_settings_and_actions_fit_minimum_window(self):
        panel = self.restart_unconfigured()
        self.assertEqual(self.app.current_page, "settings")
        self.app.root.geometry("960x700"); self.settle()
        self.assertIn("1 保存と資料", [panel.tabs.tab(tab, "text") for tab in panel.tabs.tabs()])
        for widget in (panel.save_button, panel.test_button, panel.chat_button):
            self.assertTrue(widget.winfo_ismapped(), str(widget.cget("text")) if "text" in widget.keys() else str(widget))
            self.assertGreaterEqual(widget.winfo_rootx(), self.app.root.winfo_rootx())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), self.app.root.winfo_rootx() + self.app.root.winfo_width())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.app.root.winfo_rooty() + self.app.root.winfo_height())
        self.assertEqual(str(panel.chat_button["state"]), "disabled")
        for widget in self.app.nav_buttons.values():
            self.assertTrue(widget.winfo_ismapped(), str(widget.cget("text")) if "text" in widget.keys() else str(widget))
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.app.root.winfo_rooty() + self.app.root.winfo_height())

    def test_gui_setup_import_question_and_citation_without_json_edit_or_restart(self):
        from test_setup import EchoProbeClient
        from work_assistant.config import load
        panel = self.restart_unconfigured(); self.permit_connection(panel)
        root = self.home.parent / (self.home.name + "-documents"); root.mkdir()
        document = root / "meeting.txt"; document.write_text("会議では測定条件を確認する。", encoding="utf-8")
        try:
            with patch("work_assistant.ui_settings.filedialog.askdirectory", return_value=str(root)):
                panel.add_root()
            client = EchoProbeClient()
            with patch("work_assistant.setup_flow.BedrockClient", return_value=client):
                panel.test_button.invoke()
                self.wait(lambda: not panel.busy and "成功" in panel.status.get())
            self.assertEqual(len(client.calls), 2)
            self.assertTrue(self.app.ready()); self.assertTrue(load(self.home).local_data_approved)
            self.assertEqual(str(panel.chat_button["state"]), "normal")
            panel.chat_button.invoke(); self.assertEqual(self.app.current_page, "chat")
            with patch("work_assistant.ui.filedialog.askopenfilenames", return_value=[str(document)]):
                self.app.import_documents()
                self.wait(lambda: bool(self.app.attached))
            did = self.app.attached[0]
            self.rt.agent.client = ScriptedClient([message("確認事項は測定条件です。 [資料:" + did + "]")])
            self.app._set_input("会議の確認事項を整理して")
            self.wait(lambda: str(self.app.send["state"]) == "normal")
            self.app.send.invoke()
            self.wait(lambda: not self.app.loading and len(self.app.message_rows) == 2)
            self.assertEqual(self.app.message_rows[-1]["metadata"]["citations"][0]["id"], did)
            self.app.open_document(did)
            self.wait(lambda: any(w.winfo_class() == "Toplevel" for w in self.app.root.winfo_children()))
        finally:
            document.unlink(missing_ok=True); root.rmdir()

    def test_save_only_never_tests_connection_and_preserves_advanced_settings(self):
        from work_assistant.config import load
        panel = self.restart_unconfigured()
        self.rt.cfg.retention_days = 47; panel.base.retention_days = 47
        panel.vars["local_data_approved"].set(True)
        with patch("work_assistant.ui_settings.probe_connection") as probe:
            panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        probe.assert_not_called()
        self.assertTrue(load(self.home).local_data_approved)
        self.assertEqual(load(self.home).retention_days, 47)
        self.assertFalse(self.app.ready())
        self.assertIn("AI接続", panel.status.get())

    def test_invalid_setting_and_save_failure_keep_input_and_old_config(self):
        panel = self.restart_unconfigured(); self.permit_connection(panel)
        before = (self.home / "config.json").read_bytes()
        panel.vars["endpoint"].set("https://example.com/openai/v1")
        panel.save_button.invoke(); self.settle(.05)
        self.assertIn("接続先", panel.status.get()); self.assertFalse(panel.busy)
        self.assertEqual(panel.vars["endpoint"].get(), "https://example.com/openai/v1")
        panel.vars["endpoint"].set("https://bedrock-runtime.ap-northeast-1.amazonaws.com/openai/v1")
        with patch("work_assistant.runtime.atomic_json", side_effect=OSError("write failed")):
            panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertEqual((self.home / "config.json").read_bytes(), before)
        self.assertFalse(self.rt.cfg.local_data_approved)
        self.assertTrue(panel.vars["local_data_approved"].get())
        self.assertTrue(panel.dirty); self.assertIn("書き込み権限", panel.status.get())

    def test_missing_approval_does_not_call_connection_test(self):
        panel = self.restart_unconfigured()
        with patch("work_assistant.ui_settings.probe_connection") as probe:
            panel.test_button.invoke(); self.settle(.05)
        probe.assert_not_called(); self.assertFalse(panel.busy)
        self.assertIn("許可", panel.status.get())
        self.assertEqual(str(panel.tabs.index(panel.tabs.select())), "0")

    def test_connection_failure_retries_and_changed_model_invalidates_success(self):
        from work_assistant.common import AssistantError
        panel = self.restart_unconfigured(); self.permit_connection(panel)
        with patch("work_assistant.ui_settings.probe_connection", side_effect=[AssistantError("BEDROCK_ACCESS_DENIED"), {}]):
            panel.test_button.invoke(); self.wait(lambda: not panel.busy)
            self.assertIn("AWSプロファイル", panel.status.get())
            self.assertIn("失敗", panel.probe_label["text"])
            self.assertEqual(panel.vars["model"].get(), "global.openai.gpt-6-luna")
            panel.test_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertIn("成功", panel.probe_label["text"])
        panel.vars["model"].set("global.openai.other")
        self.assertEqual(str(panel.chat_button["state"]), "disabled")
        panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertIn("未確認", panel.probe_label["text"])

    def test_busy_runtime_keeps_unsaved_inputs_for_retry(self):
        panel = self.restart_unconfigured(); self.permit_connection(panel)
        with self.rt.operation():
            panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertFalse(self.rt.cfg.local_data_approved)
        self.assertTrue(panel.dirty)
        self.assertIn("処理が終わって", panel.status.get())
        panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertTrue(self.rt.cfg.local_data_approved)

    def test_long_connection_test_keeps_ui_responsive_and_blocks_duplicate_submission(self):
        panel = self.restart_unconfigured(); self.permit_connection(panel)
        started, release = threading.Event(), threading.Event()
        def probe(*_): started.set(); release.wait(5); return {}
        with patch("work_assistant.ui_settings.probe_connection", side_effect=probe) as mock:
            try:
                panel.test_button.invoke(); self.wait(started.is_set)
                self.assertEqual(str(panel.save_button["state"]), "disabled")
                self.assertEqual(str(panel.fields["model"]["state"]), "disabled")
                panel.submit(True)
                self.app.show_page("documents"); self.settle(.05)
                self.assertEqual(self.app.current_page, "documents")
            finally: release.set()
            self.wait(lambda: not panel.busy)
        self.assertEqual(mock.call_count, 1)

    def test_empty_attachment_dialog_explains_import_and_keeps_buttons_visible(self):
        self.app.choose_documents(); self.settle(.05)
        dialog = next(w for w in self.app.root.winfo_children() if w.winfo_class() == "Toplevel")
        dialog.geometry("640x400"); self.settle(.05)
        actions = next(w for w in dialog.winfo_children() if w.winfo_class() == "Frame")
        for widget in (w for w in descendants(actions) if w.winfo_class() == "Button"):
            self.assertTrue(widget.winfo_ismapped(), str(widget.cget("text")) if "text" in widget.keys() else str(widget))
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), dialog.winfo_rooty() + dialog.winfo_height())
        choose = action(dialog, "この資料を添える")
        self.assertEqual(str(choose["state"]), "disabled")
        self.assertTrue(any("資料はまだありません" in str(w.cget("text")) for w in dialog.winfo_children() if "text" in w.keys()))

    def access_key_panel(self):
        from test_credentials import MemoryProtector
        from work_assistant.ui_settings import AUTH_NAMES
        panel = self.restart_unconfigured(); self.permit_connection(panel)
        self.rt.credentials.protector = MemoryProtector()
        panel.vars["auth"].set(AUTH_NAMES["access_keys"])
        return panel

    def fill_keys(self, panel, material):
        for name in panel.secret_vars: panel.secret_vars[name].set(getattr(material, name))

    def test_access_keys_catalog_is_ephemeral_then_save_and_probe_use_only_reference(self):
        from test_credentials import keys
        from work_assistant.model_catalog import CatalogResult, ModelChoice
        from work_assistant.config import load
        panel = self.access_key_panel(); material = keys(temporary=True)
        self.fill_keys(panel, material)
        for name in panel.secret_vars:
            self.assertEqual(panel.fields[name].cget("show"), "●")
            self.assertNotIn(name, panel.vars)
        before = (self.home / "config.json").read_bytes()
        choice = ModelChoice("apac.anthropic.claude-sonnet-4-6", "Claude", "converse", "推論プロファイル", ("ap-northeast-1",), "available")
        with patch("work_assistant.ui_settings.list_models", return_value=CatalogResult([choice], [])) as catalog:
            panel.load_catalog(); self.wait(lambda: not panel.busy)
        client = catalog.call_args.kwargs["client"]
        self.assertEqual(client.credentials, material)
        self.assertEqual(catalog.call_args.args[0].auth, "access_keys")
        self.assertEqual((self.home / "config.json").read_bytes(), before)
        self.assertFalse((self.home / "credentials").exists())
        material = keys("B", temporary=True); self.fill_keys(panel, material)
        self.assertFalse(panel.catalog_choices); self.assertEqual(str(panel.model_combo["state"]), "disabled")
        with patch("work_assistant.ui_settings.list_models", return_value=CatalogResult([choice], [])):
            panel.load_catalog(); self.wait(lambda: not panel.busy)
        panel.model_combo.current(0); panel.select_model()
        def probe(cfg, _):
            self.assertTrue(cfg.bedrock.credential_ref)
            self.assertEqual(self.rt.credentials.read(cfg.bedrock.credential_ref), material)
            return {}
        with patch("work_assistant.ui_settings.probe_connection", side_effect=probe):
            panel.test_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertIn("成功", panel.status.get()); self.assertTrue(self.app.ready())
        self.assertEqual(load(self.home).bedrock.auth, "access_keys")
        self.assertEqual(panel.base.bedrock.credential_ref, load(self.home).bedrock.credential_ref)
        self.assertTrue(all(v.get() == "" for v in panel.secret_vars.values()))
        self.assertFalse(panel.dirty); self.assertIn("保存済み", panel.key_status["text"])
        for path in self.home.rglob("*"):
            if path.is_file():
                for value in (material.access_key_id, material.secret_access_key, material.session_token):
                    self.assertNotIn(value.encode(), path.read_bytes())

    def test_access_keys_failed_save_preserves_inputs_and_old_key_then_rotation_retries(self):
        from test_credentials import keys
        panel = self.access_key_panel(); self.fill_keys(panel, keys())
        panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        old = self.rt.cfg.bedrock.credential_ref
        self.fill_keys(panel, keys("B"))
        with patch("work_assistant.runtime.atomic_json", side_effect=OSError("synthetic write failure")):
            panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertEqual(self.rt.cfg.bedrock.credential_ref, old)
        self.assertEqual(self.rt.credentials.read(old), keys())
        self.assertEqual(panel.secret_vars["secret_access_key"].get(), keys("B").secret_access_key)
        self.assertIn("書き込み権限", panel.status.get())
        panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertNotEqual(self.rt.cfg.bedrock.credential_ref, old)
        self.assertEqual(self.rt.credentials.read(self.rt.cfg.bedrock.credential_ref), keys("B"))
        self.assertEqual(len(list((self.home / "credentials").glob("*.bin"))), 1)
        self.assertFalse(any(v.get() for v in panel.secret_vars.values()))

    def test_key_counter_explains_forty_character_rejection_then_trims_pasted_keys(self):
        from test_credentials import keys
        from work_assistant.model_catalog import CatalogResult
        panel = self.access_key_panel(); material = keys()
        self.fill_keys(panel, material)
        panel.secret_vars["secret_access_key"].set("Ａ" + "a" * 39)
        self.assertIn("40 / 40", panel.secret_feedback["text"])
        with patch("work_assistant.ui_settings.list_models") as catalog:
            panel.catalog_button.invoke(); self.settle(.05)
        catalog.assert_not_called(); self.assertIn("半角以外", panel.status.get())
        panel.secret_vars["access_key_id"].set(" \n" + material.access_key_id + "\t")
        panel.secret_vars["secret_access_key"].set(" \r\n" + material.secret_access_key + "\n")
        self.assertIn("形式は正しい", panel.secret_feedback["text"])
        self.assertNotIn(material.secret_access_key, panel.secret_feedback["text"])
        with patch("work_assistant.ui_settings.list_models", return_value=CatalogResult([], [])) as catalog:
            panel.catalog_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertEqual(catalog.call_args.kwargs["client"].credentials, material)
        panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertEqual(self.rt.credentials.read(self.rt.cfg.bedrock.credential_ref), material)
        self.assertIn("未入力", panel.secret_feedback["text"])

    def test_proxy_settings_apply_to_catalog_save_and_show_network_failure_guidance(self):
        from test_credentials import keys
        from work_assistant.common import AssistantError
        from work_assistant.model_catalog import CatalogResult, ModelChoice
        from work_assistant.config import load
        panel = self.access_key_panel(); self.fill_keys(panel, keys())
        choice = ModelChoice("anthropic.claude-sonnet-4-6", "Claude", "converse", "モデル", ("ap-northeast-1",), "available")
        with patch("work_assistant.ui_settings.list_models", return_value=CatalogResult([choice], [])):
            panel.load_catalog(); self.wait(lambda: not panel.busy)
        panel.vars["proxy_url"].set("http://proxy.example:8080")
        self.assertFalse(panel.catalog_choices)
        with patch("work_assistant.ui_settings.list_models", side_effect=AssistantError("BEDROCK_PROXY_AUTH_REQUIRED")) as catalog:
            panel.load_catalog(); self.wait(lambda: not panel.busy)
        self.assertEqual(catalog.call_args.args[0].proxy_url, "http://proxy.example:8080")
        self.assertEqual(catalog.call_args.kwargs["client"].settings.proxy_url, "http://proxy.example:8080")
        self.assertIn("407", panel.status.get()); self.assertIn("AWSキーのエラーではありません", panel.catalog_label["text"])
        self.assertNotIn("ListFoundationModels", panel.catalog_label["text"])
        panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertEqual(load(self.home).bedrock.proxy_url, "http://proxy.example:8080")
        panel.vars["proxy_url"].set("http://proxy.example/settings.pac")
        with patch("work_assistant.ui_settings.list_models") as catalog:
            panel.load_catalog(); self.settle(.05)
        catalog.assert_not_called(); self.assertTrue(panel.advanced.get()); self.assertIn("PAC", panel.status.get())

    def test_access_key_partial_input_and_missing_session_token_never_start_network(self):
        from test_credentials import keys
        panel = self.access_key_panel()
        with patch("work_assistant.ui_settings.list_models") as catalog, patch("work_assistant.ui_settings.probe_connection") as probe:
            panel.secret_vars["access_key_id"].set(keys().access_key_id)
            panel.catalog_button.invoke(); self.settle(.05)
            self.assertIn("Secret Access Key", panel.status.get())
            self.fill_keys(panel, keys(temporary=True)); panel.secret_vars["session_token"].set("")
            panel.test_button.invoke(); self.settle(.05)
            self.assertIn("Session Token", panel.status.get())
        catalog.assert_not_called(); probe.assert_not_called()
        self.assertFalse(panel.busy); self.assertEqual(self.rt.cfg.bedrock.credential_ref, "")

    def test_access_key_delete_is_staged_until_save_and_disables_live_connection(self):
        from test_credentials import keys
        panel = self.access_key_panel(); self.fill_keys(panel, keys())
        panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        ref = self.rt.cfg.bedrock.credential_ref; self.assertTrue(self.app.ready())
        panel.remove_keys.invoke(); self.settle(.05)
        self.assertEqual(self.rt.credentials.read(ref), keys())
        self.assertIn("削除", panel.key_status["text"])
        with patch("work_assistant.ui_settings.probe_connection") as probe:
            panel.test_button.invoke(); self.settle(.05)
        probe.assert_not_called(); self.assertEqual(self.rt.cfg.bedrock.credential_ref, ref)
        from work_assistant.common import AssistantError
        with patch.object(self.rt.credentials, "delete", side_effect=AssistantError("CREDENTIAL_DELETE_FAILED")):
            panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertTrue(panel.cleanup_pending); self.assertIn("削除に失敗", panel.status.get())
        self.assertFalse(self.app.ready()); self.assertEqual(self.rt.cfg.bedrock.credential_ref, "")
        panel = self.restart_unconfigured(); self.assertTrue(panel.cleanup_pending)
        panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertFalse(panel.cleanup_pending)
        self.assertFalse(list((self.home / "credentials").glob("*.bin")))

    def test_unused_key_inputs_are_not_saved_by_profile_auth_and_are_remasked_on_switch(self):
        from test_credentials import keys
        from work_assistant.ui_settings import AUTH_NAMES
        panel = self.access_key_panel(); self.fill_keys(panel, keys())
        panel.show_keys.set(True); panel.toggle_keys()
        self.assertEqual(panel.fields["secret_access_key"].cget("show"), "")
        panel.vars["auth"].set(AUTH_NAMES["sigv4"])
        self.assertEqual(panel.fields["secret_access_key"].cget("show"), "●")
        self.assertEqual(panel.key_frame.winfo_manager(), "")
        panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertEqual(self.rt.cfg.bedrock.auth, "sigv4")
        self.assertFalse((self.home / "credentials").exists())
        self.assertFalse(any(v.get() for v in panel.secret_vars.values()))

    def test_model_catalog_selects_claude_and_openai_without_saving_or_inference(self):
        from work_assistant.model_catalog import ModelChoice, CatalogResult
        from work_assistant.config import load
        panel = self.restart_unconfigured()
        before = (self.home / "config.json").read_bytes()
        claude = ModelChoice("apac.anthropic.claude-sonnet-4-6", "Claude Sonnet", "converse", "推論プロファイル", ("ap-northeast-1", "ap-southeast-2"), "available")
        openai = ModelChoice("global.openai.gpt-6-luna", "GPT Luna", "responses", "推論プロファイル", ("us-east-1",), "unknown")
        with patch("work_assistant.ui_settings.list_models", return_value=CatalogResult([claude, openai], [])) as catalog, patch("work_assistant.ui_settings.probe_connection") as probe:
            panel.catalog_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertEqual(catalog.call_count, 1); probe.assert_not_called()
        self.assertEqual((self.home / "config.json").read_bytes(), before)
        self.assertFalse(panel.dirty)
        panel.tabs.select(1); self.settle(.05)
        self.app.root.tk.call("ttk::combobox::Post", panel.model_combo)
        self.settle(.05)
        # Native dropdown focus has no corresponding Python widget. Repainting
        # rounded controls while it is open must remain safe.
        panel.catalog_button.configure(state="normal")
        panel.save_button.paint(); self.settle(.05)
        self.app.root.tk.call("ttk::combobox::Unpost", panel.model_combo)
        panel.model_combo.current(0); panel.model_combo.event_generate("<<ComboboxSelected>>"); self.settle(.05)
        self.assertEqual(panel.vars["model"].get(), claude.model)
        self.assertEqual(panel.vars["endpoint"].get(), "https://bedrock-runtime.ap-northeast-1.amazonaws.com")
        self.assertIn("ap-southeast-2", panel.catalog_label["text"])
        self.assertTrue(panel.dirty); self.assertEqual(load(self.home).bedrock.model, openai.model)
        self.permit_connection(panel)
        panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertEqual(load(self.home).bedrock.model, claude.model)
        self.assertEqual(self.rt.agent.client.settings.model, claude.model)
        panel.model_combo.current(1); panel.model_combo.event_generate("<<ComboboxSelected>>"); self.settle(.05)
        self.assertEqual(panel.vars["endpoint"].get(), "https://bedrock-runtime.ap-northeast-1.amazonaws.com/openai/v1")

    def test_model_catalog_failure_empty_retry_and_auth_changes_do_not_leave_stale_choices(self):
        from work_assistant.common import AssistantError
        from work_assistant.model_catalog import ModelChoice, CatalogResult
        panel = self.restart_unconfigured()
        choice = ModelChoice("anthropic.claude-sonnet-4-6", "Claude", "converse", "リージョン内", ("ap-northeast-1",), "unknown")
        with patch("work_assistant.ui_settings.list_models", side_effect=[AssistantError("BEDROCK_ACCESS_DENIED"), CatalogResult([], []), CatalogResult([choice], ["利用条件未確認"])]):
            panel.catalog_button.invoke(); self.wait(lambda: not panel.busy)
            self.assertEqual(str(panel.model_combo["state"]), "disabled")
            self.assertIn("権限", panel.catalog_label["text"])
            panel.catalog_button.invoke(); self.wait(lambda: not panel.busy)
            self.assertIn("選択可能", panel.model_selection.get())
            panel.catalog_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertEqual(str(panel.model_combo["state"]), "readonly")
        panel.vars["profile"].set("another-profile")
        self.assertFalse(panel.catalog_choices); self.assertEqual(str(panel.model_combo["state"]), "disabled")
        panel.vars["model"].set(choice.model)
        panel.vars["region"].set("us-east-1")
        self.assertEqual(panel.vars["endpoint"].get(), "https://bedrock-runtime.us-east-1.amazonaws.com")

    def test_slow_catalog_keeps_ui_responsive_blocks_duplicates_and_rejects_late_results(self):
        from work_assistant.model_catalog import ModelChoice, CatalogResult
        panel = self.restart_unconfigured(); entered, release = threading.Event(), threading.Event()
        choice = ModelChoice("anthropic.claude-sonnet-4-6", "Claude", "converse", "リージョン内", ("ap-northeast-1",), "available")
        def retrieve(_): entered.set(); release.wait(5); return CatalogResult([choice], [])
        with patch("work_assistant.ui_settings.list_models", side_effect=retrieve) as catalog:
            try:
                panel.catalog_button.invoke(); self.wait(entered.is_set)
                self.assertEqual(str(panel.fields["region"]["state"]), "disabled")
                self.assertEqual(str(panel.save_button["state"]), "disabled")
                panel.load_catalog(); self.app.show_page("documents"); self.settle(.05)
                panel.vars["region"].set("us-east-1")
            finally: release.set()
            self.wait(lambda: not panel.busy)
        self.assertEqual(catalog.call_count, 1); self.assertFalse(panel.catalog_choices)
        self.assertIn("再取得", panel.status.get())

    def test_manual_regional_claude_saves_without_cross_region_approval(self):
        from work_assistant.config import load
        panel = self.restart_unconfigured(); self.permit_connection(panel)
        panel.vars["cross_region_approved"].set(False)
        panel.vars["model"].set("anthropic.claude-3-5-sonnet-20241022-v2:0")
        self.assertEqual(panel.vars["endpoint"].get(), "https://bedrock-runtime.ap-northeast-1.amazonaws.com")
        panel.save_button.invoke(); self.wait(lambda: not panel.busy)
        self.assertTrue(self.app.ready())
        self.assertEqual(load(self.home).bedrock.model, panel.vars["model"].get())

    def test_import_without_folder_routes_to_setup_and_disallowed_file_is_not_imported(self):
        self.app.import_documents()
        self.assertEqual(self.app.current_page, "settings")
        self.assertIn("資料フォルダ", self.app.settings_panel.status.get())
        allowed = self.home / "allowed"; allowed.mkdir()
        outside = self.home / "outside.txt"; outside.write_text("outside-scope", encoding="utf-8")
        self.rt.cfg.roots = [str(allowed)]
        with patch("work_assistant.ui.filedialog.askopenfilenames", return_value=[str(outside)]), patch("work_assistant.ui.messagebox.showwarning") as warning:
            self.app.import_documents(); self.wait(lambda: warning.called)
        self.assertEqual(self.rt.store.documents(), [])
        self.assertEqual(self.app.attached, [])
        self.assertIn("フォルダを追加", warning.call_args.args[1])

    def test_region_change_updates_standard_endpoint(self):
        panel = self.app.settings_panel
        panel.vars["region"].set("us-east-1")
        self.assertEqual(panel.vars["endpoint"].get(), "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1")

    def test_settings_and_navigation_remain_usable_with_larger_tk_scaling(self):
        import tkinter as tk
        from work_assistant.ui import Desktop
        original_tk = tk.Tk
        self.app.close()
        def scaled_root():
            root = original_tk(); root.tk.call("tk", "scaling", 2.0); return root
        with patch("work_assistant.ui.tk.Tk", side_effect=scaled_root):
            self.app = Desktop(self.rt, start_runtime=False)
        self.app.root.report_callback_exception = lambda kind, error, trace: self.callback_errors.append(str(error))
        self.app.root.geometry("960x700"); self.app.show_page("settings"); self.settle()
        for widget in [*self.app.nav_buttons.values(), self.app.settings_panel.save_button, self.app.settings_panel.test_button]:
            self.assertTrue(widget.winfo_ismapped(), str(widget.cget("text")) if "text" in widget.keys() else str(widget))
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), self.app.root.winfo_rootx() + self.app.root.winfo_width())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.app.root.winfo_rooty() + self.app.root.winfo_height())
        self.app.show_page("chat"); self.settle(.1)
        self.assert_composer_fits()

    def assert_composer_fits(self):
        for widget in (self.app.attach_button, self.app.stop_button, self.app.send, self.app.register_work):
            surface = widget.surface
            self.assertTrue(widget.winfo_ismapped(), widget["text"])
            self.assertLessEqual(surface.winfo_rootx() + surface.winfo_width(), self.app.root.winfo_rootx() + self.app.root.winfo_width(), widget["text"])
            self.assertLessEqual(surface.winfo_rooty() + surface.winfo_height(), self.app.root.winfo_rooty() + self.app.root.winfo_height(), widget["text"])

    def test_composer_actions_remain_visible_in_small_window(self):
        self.app.root.geometry("960x700"); self.app.show_page("chat"); self.settle(.1)
        self.assert_composer_fits()

    def test_rounded_controls_preserve_native_roles_keyboard_focus_and_edge_click(self):
        import tkinter as tk
        from work_assistant.ui_widgets import button, entry, BG
        dialog = tk.Toplevel(self.app.root); dialog.configure(bg=BG); dialog.geometry("400x240")
        calls = []
        control = button(dialog, "操作を実行", lambda: calls.append("clicked"), primary=True)
        control.pack(fill="x", padx=20, pady=20)
        field = entry(dialog); field.pack(fill="x", padx=20)
        self.settle(.1); dialog.focus_force(); control.focus_set(); self.wait(lambda: self.app.root.focus_get() == control)
        self.assertEqual(control.winfo_class(), "Button")
        self.assertEqual(field.winfo_class(), "Entry")
        control.event_generate("<Return>"); self.settle(.05)
        self.assertEqual(calls, ["clicked"])
        control.event_generate("<Tab>"); self.wait(lambda: self.app.root.focus_get() == field)
        self.assertIs(self.app.root.focus_get(), field)
        field.insert(0, "日本語の入力"); self.assertEqual(field.get(), "日本語の入力")
        control.configure(state="disabled"); control.invoke(); self.assertEqual(len(calls), 1)
        control.configure(state="normal")
        edge = control.surface.backdrop
        edge.event_generate("<ButtonPress-1>", x=2, y=edge.winfo_height() // 2)
        edge.event_generate("<ButtonRelease-1>", x=2, y=edge.winfo_height() // 2)
        self.settle(.05); self.assertEqual(len(calls), 2)
        self.assertIs(self.app.root.focus_get(), control)
        control.destroy(); field.destroy(); self.settle(.05)
        self.assertFalse(control.surface.winfo_exists()); self.assertFalse(field.surface.winfo_exists())


if __name__ == "__main__": unittest.main()
