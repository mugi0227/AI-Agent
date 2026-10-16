from __future__ import annotations

import json
from contextlib import closing
import sqlite3
import threading
import time
from pathlib import Path

from test_core import Fixture, message
from work_assistant.agent import Agent
from work_assistant.bedrock import ScriptedClient
from work_assistant.chat import ChatController, ChatEvent, copy_answer, display_answer
from work_assistant.common import AssistantError, stamp
from work_assistant.runtime import Runtime
from work_assistant.store import Store


class ChatStorageTests(Fixture):
    def test_migrate_existing_history_without_losing_it(self):
        path = self.home / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as cx:
            cx.executescript("CREATE TABLE conversations(id INTEGER PRIMARY KEY AUTOINCREMENT,session TEXT,role TEXT,text TEXT,created_at TEXT); PRAGMA user_version=1;")
            cx.execute("INSERT INTO conversations(session,role,text,created_at) VALUES('main','user','以前の質問',?)", (stamp(),))
            cx.commit()
        store = Store(path)
        self.assertEqual(store.history("main")[0]["content"], "以前の質問")
        self.assertEqual(store.sessions()[0]["id"], "main")
        with store.connection() as cx: self.assertEqual(cx.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_sessions_drafts_and_archive_survive_restart(self):
        first, second = self.store.new_session(), self.store.new_session()
        self.store.rename_session(first, "会議の準備")
        self.store.save_draft(first, "条件を整理して\n確認事項も", ["doc-1"])
        self.store.save_draft(second, "別の下書き", [])
        self.store.archive_session(first)
        store = Store(self.store.path)
        self.assertEqual(store.session(first)["draft"], "条件を整理して\n確認事項も")
        self.assertEqual(store.session(first)["document_ids"], ["doc-1"])
        self.assertEqual(store.session(second)["draft"], "別の下書き")
        self.assertNotIn(first, {s["id"] for s in store.sessions()})
        store.archive_session(first, False)
        self.assertEqual(store.sessions("会議")[0]["id"], first)

    def test_failed_and_interrupted_messages_are_not_model_history(self):
        sid = self.store.new_session()
        mid = self.store.remember_turn(sid, "user", "途中の依頼", status="running")
        store = Store(self.store.path)
        self.assertEqual(store.messages(sid)[0]["status"], "running")
        store.recover_interrupted()
        self.assertEqual(store.messages(sid)[0]["status"], "interrupted")
        self.assertFalse(store.history(sid)); self.assertFalse(store.search_conversations("依頼", sid))
        store.retry_message(sid, mid, "途中の依頼")
        store.finish_message(mid, "completed")
        self.assertEqual(store.history(sid)[0]["content"], "途中の依頼")

    def test_source_revocation_also_clears_titles_drafts_and_metadata(self):
        _, did = self.add()
        sid = self.store.new_session()
        self.store.remember_turn(sid, "assistant", "資料の要約", metadata={"citations": [{"id": did}]})
        self.store.rename_session(sid, "資料の内容")
        self.store.save_draft(sid, "資料を引用した下書き", [did], expected_cleared_at="")
        self.store.invalidate(did)
        state = self.store.session(sid)
        self.assertEqual(state["draft"], ""); self.assertEqual(state["title"], "新しい会話")
        self.assertFalse(self.store.messages(sid)); self.assertFalse(state["document_ids"])
        self.assertCode("CHAT_CHANGED", self.store.save_draft, sid, "古い下書き", [did], expected_cleared_at="")

    def test_complete_history_is_session_scoped(self):
        a, b = self.store.new_session(), self.store.new_session()
        self.store.remember_turn(a, "user", "案件Aの会話")
        self.store.remember_turn(b, "user", "案件Bの会話")
        self.assertEqual([r["text"] for r in self.store.messages(a)], ["案件Aの会話"])
        self.assertFalse(self.store.search_conversations("案件B", a))
        self.store.rename_session(a, "別のタイトル")
        self.assertEqual(self.store.sessions("案件A")[0]["id"], a)

    def test_draft_longer_than_send_limit_is_preserved(self):
        sid = self.store.new_session()
        draft = "長い下書き\n" * 2000
        self.store.save_draft(sid, draft, [])
        self.assertEqual(self.store.session(sid)["draft"], draft)

    def test_copy_keeps_numbered_sources_with_the_response(self):
        sources = [{"id": "doc-a", "title": "会議資料", "source_url": "https://example.invalid/a"}]
        self.assertEqual(display_answer("確認した条件 [資料:doc-a]", sources), "確認した条件 ［1］")
        copied = copy_answer("確認した条件 [資料:doc-a]", sources)
        self.assertIn("会議資料", copied); self.assertIn("https://example.invalid/a", copied)


class AgentChatTests(Fixture):
    def test_selected_document_is_reference_data_and_citations_are_saved(self):
        raw, did = self.add()
        agent, client = self.agent([message("確認しました [資料:" + did + "]")])
        sid = self.store.new_session()
        result = agent.turn("この資料を整理して", session=sid, document_ids=[did])
        reference = next(x["content"] for x in client.calls[0]["input"] if "ATTACHED_REFERENCE_DATA" in x.get("content", ""))
        self.assertIn(raw["content"], reference.replace('\\n', '\n'))
        rows = self.store.messages(sid)
        self.assertEqual(rows[0]["metadata"]["document_ids"], [did])
        self.assertEqual(rows[1]["metadata"]["citations"], result.citations)
        self.assertEqual(rows[0]["status"], "completed")

    def test_stop_during_stream_never_saves_partial_assistant_text(self):
        cancel = threading.Event()
        class Client:
            is_live = False
            def create(inner, items, instructions, tools, on_delta):
                on_delta("未確定の途中出力"); cancel.set(); on_delta("続き")
        agent = Agent(self.store, self.cfg, self.ingest, self.bridge, Client())
        self.assertCode("TURN_PREEMPTED", agent.turn, "質問", cancel=cancel)
        rows = self.store.messages("main")
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]["status"], "cancelled")
        self.assertNotIn("未確定", json.dumps(rows, ensure_ascii=False)); self.assertFalse(self.store.history("main"))

    def test_failed_question_can_retry_without_duplicate_user_message(self):
        agent, _ = self.agent([])
        self.assertCode("MOCK_RESPONSES_EXHAUSTED", agent.turn, "同じ質問")
        mid = self.store.messages("main")[0]["id"]
        agent.client = ScriptedClient([message("再実行した回答")])
        agent.turn("同じ質問", retry_message_id=mid)
        rows = self.store.messages("main")
        self.assertEqual([r["role"] for r in rows], ["user", "assistant"])
        self.assertEqual(rows[0]["id"], mid); self.assertEqual(rows[0]["status"], "completed")

    def test_other_session_cannot_retry_a_message(self):
        agent, _ = self.agent([])
        self.assertCode("MOCK_RESPONSES_EXHAUSTED", agent.turn, "質問", session="alpha")
        mid = self.store.messages("alpha")[0]["id"]
        self.assertCode("RETRY_NOT_AVAILABLE", agent.turn, "質問", session="beta", retry_message_id=mid)

    def test_revoked_attachment_cannot_reach_model(self):
        _, did = self.add(); self.store.invalidate(did)
        agent, client = self.agent([message()])
        self.assertCode("SOURCE_UNAVAILABLE", agent.turn, "資料を読んで", document_ids=[did])
        self.assertFalse(client.calls)

    def test_invalidation_while_model_runs_cannot_repopulate_chat(self):
        _, did = self.add()
        class Client:
            is_live = False
            def create(inner, *_):
                self.store.invalidate(did)
                return message("古い資料を使った回答")
        agent = Agent(self.store, self.cfg, self.ingest, self.bridge, Client())
        self.assertCode("SOURCE_CHANGED_DURING_TURN", agent.turn, "質問")
        self.assertFalse(self.store.messages("main"))


class ControllerTests(Fixture):
    def test_stream_stop_and_late_events_keep_their_original_session(self):
        started, release = threading.Event(), threading.Event()
        class Client:
            is_live = False
            def create(inner, items, instructions, tools, on_delta):
                on_delta("途中の回答"); started.set(); release.wait(5); on_delta("続き")
                return message("最終回答")
        rt = Runtime(self.home, self.cfg, Client())
        controller = ChatController(rt)
        job = controller.send("alpha", "依頼", [])
        self.assertTrue(started.wait(5))
        self.assertCode("FOREGROUND_BUSY", controller.send, "beta", "別の依頼", [])
        controller.events.put(ChatEvent("delta", "beta", "stale-request", "混ざらない文字列"))
        controller.drain()
        self.assertEqual(job.text, "途中の回答")
        controller.stop(); release.set()
        deadline = time.monotonic() + 5
        events = []
        while controller.active and time.monotonic() < deadline:
            events.extend(controller.drain()); time.sleep(.01)
        self.assertIsNone(controller.active)
        self.assertTrue(any(e.kind == "error" and e.session == "alpha" for e in events))
        self.assertEqual(rt.store.messages("alpha")[0]["status"], "cancelled")
        self.assertFalse(rt.store.messages("beta"))
