"""Native conversation workspace. Network calls and source validation never run on Tk."""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox, simpledialog, filedialog
from tkinter.scrolledtext import ScrolledText

from .chat import ChatController, error_help, display_answer, copy_answer, TOOL_LABELS
from .common import AssistantError, safe_code
from .runtime import Runtime
from .ui_widgets import (BG, WHITE, INK, MUTED, LINE, TEAL, SOFT, NAV, NAV_HOVER, NAV_MUTED,
                         ERROR, MessageCard, ScrollFrame, RoundedPanel, button, entry, label, badge, treeview, setup_theme)

PAGE_TITLES = {"chat": "会話", "documents": "資料・検索", "proposals": "承認待ち",
               "tasks": "タスク・案件", "work": "実行管理", "notifications": "通知", "settings": "接続・設定"}
DOCUMENT_TYPES = {"document": "文書", "transcript": "文字起こし", "capture": "Webページ",
                  "mail": "メール", "teams_message": "Teamsメッセージ", "calendar": "予定", "search_result": "検索結果"}


def short_time(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%m/%d %H:%M")
    except (ValueError, TypeError):
        return ""


class Desktop:
    def __init__(self, runtime: Runtime, *, start_runtime=True):
        self.rt = runtime
        self.controller = ChatController(runtime)
        self.events = queue.Queue()
        self.root = tk.Tk()
        self.root.ui_font, self.root.mono_font = setup_theme(self.root)
        self.root.title("Work Assistant")
        self.root.geometry("1240x860"); self.root.minsize(960, 700)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.closed, self.loading = False, False
        self.load_version = 0
        self._draft_job = None
        self._setting_input = False
        self.current_page = "chat"
        self.current_session = self.rt.store.get_state("ui.active_session", "main")
        try: self.rt.store.ensure_session(self.current_session)
        except AssistantError: self.current_session = self.rt.store.ensure_session()
        self.session_cleared = ""
        self.attached, self.message_rows, self.session_rows = [], [], []
        self.proposal_rows, self.task_rows = [], []
        self.document_rows, self.submitted, self.transient_errors = {}, {}, {}
        self.diagnostic_report = None
        self.pending_card, self.pending_request = None, None
        self.scroll_restore = None
        self.show_archived = tk.BooleanVar(value=False)
        self.focus = tk.BooleanVar(value=self.rt.store.get_state("focus", False))
        self.status = tk.StringVar(value="待機中")
        self.session_filter = tk.StringVar()
        self._build_sidebar()
        self.main = tk.Frame(self.root, bg=BG); self.main.pack(side="left", fill="both", expand=True)
        self._build_header()
        self.deck = tk.Frame(self.main, bg=BG); self.deck.pack(fill="both", expand=True)
        self.pages = {name: tk.Frame(self.deck, bg=BG) for name in PAGE_TITLES}
        self._build_chat(); self._build_documents(); self._build_other_pages(); self._build_settings()
        from .ui_work import WorkPanel
        self.work_panel = WorkPanel(self, self.pages["work"])
        self.root.bind("<Control-n>", lambda _: self.new_session())
        self.root.bind("<Control-l>", lambda _: self.focus_composer())
        self.root.bind("<Escape>", lambda _: self.stop())
        self.session_filter.trace_add("write", lambda *_: self.refresh_sessions())
        self.show_page("chat" if self.ready() else "settings"); self.load_session(self.current_session, save=False)
        if start_runtime: self.rt.start()
        self.refresh(); self.root.after(60, self.pump)

    def _build_sidebar(self):
        self.sidebar = tk.Frame(self.root, bg=NAV, width=244)
        self.sidebar.pack(side="left", fill="y"); self.sidebar.pack_propagate(False)
        # Reserve navigation before allocating the remaining height to history.
        # Windows font metrics and enlarged fonts must not push settings offscreen.
        label(self.sidebar, "履歴はこのPCに保存", size=8, bg=NAV, fg=NAV_MUTED).pack(side="bottom", anchor="w", padx=20, pady=(8, 12))
        navigation = tk.Frame(self.sidebar, bg=NAV); navigation.pack(side="bottom", fill="x")
        self.nav_buttons = {}
        for name in ("documents", "proposals", "tasks", "work", "notifications", "settings"):
            nav = button(navigation, PAGE_TITLES[name], lambda n=name: self.show_page(n), dark=True, anchor="w")
            nav.configure(bg=NAV, highlightbackground=NAV, pady=4)
            nav.pack(fill="x", padx=12, pady=2); self.nav_buttons[name] = nav
        tk.Frame(self.sidebar, bg=NAV_HOVER, height=1).pack(side="bottom", fill="x", padx=16)
        tk.Checkbutton(self.sidebar, text="保管済みを表示", variable=self.show_archived, command=self.refresh_sessions,
                       bg=NAV, fg=NAV_MUTED, activebackground=NAV, activeforeground=WHITE,
                       selectcolor=WHITE, font=(self.root.ui_font, 9), borderwidth=0, highlightthickness=0).pack(side="bottom", anchor="w", padx=14, pady=8)
        brand = tk.Frame(self.sidebar, bg=NAV); brand.pack(fill="x", padx=16, pady=(18, 16))
        logo = RoundedPanel(brand, fill=TEAL, radius=10, padding=(9, 5)); logo.pack(side="left", padx=(0, 10))
        label(logo.content, "W", size=14, bold=True, bg=TEAL, fg=WHITE).pack()
        label(brand, "Work Assistant", size=11, bold=True, bg=NAV, fg=INK).pack(side="left")
        button(self.sidebar, "新しい会話", self.new_session, primary=True).pack(fill="x", padx=16, pady=(0, 12))
        entry(self.sidebar, self.session_filter, dark=True).pack(fill="x", padx=16, ipady=7)
        label(self.sidebar, "会話を検索", size=8, bg=NAV, fg=NAV_MUTED).pack(anchor="w", padx=18, pady=(3, 8))
        self.session_caption = label(self.sidebar, "最近の会話", size=9, bold=True, bg=NAV, fg=NAV_MUTED)
        self.session_caption.pack(anchor="w", padx=18, pady=(0, 4))
        self.session_list = tk.Listbox(self.sidebar, bg=NAV, fg=INK, selectbackground=NAV_HOVER,
                                       selectforeground=TEAL, relief="flat", borderwidth=0, highlightthickness=0,
                                       activestyle="none", exportselection=False, font=(self.root.ui_font, 10),
                                       selectborderwidth=0, height=1)
        self.session_list.pack(fill="both", expand=True, padx=12)
        self.session_list.bind("<<ListboxSelect>>", self.select_session)

    def _build_header(self):
        header = tk.Frame(self.main, bg=WHITE, padx=28, pady=17); header.pack(fill="x")
        text = tk.Frame(header, bg=WHITE)
        self.section_label = label(text, "会話", size=8, bold=True, fg=MUTED)
        self.heading = label(text, "新しい会話", size=17, bold=True); self.heading.pack(anchor="w")
        tk.Checkbutton(header, text="集中モード", variable=self.focus, command=self.set_focus,
                       bg=WHITE, fg=MUTED, activebackground=WHITE, selectcolor=SOFT,
                       font=(self.root.ui_font, 9), borderwidth=0, highlightthickness=0).pack(side="right", padx=(14, 0))
        self.connection_badge = badge(header)
        self.connection_badge.pack(side="right")
        text.pack(side="left", fill="x", expand=True)
        tk.Frame(self.main, bg=LINE, height=1).pack(fill="x")
        footer = tk.Frame(self.main, bg=BG); footer.pack(side="bottom", fill="x", padx=27, pady=(0, 10))
        label(footer, textvariable=self.status, size=8, bg=BG, fg=MUTED).pack(side="left")

    def _build_chat(self):
        page = self.pages["chat"]
        tools = tk.Frame(page, bg=BG); tools.pack(fill="x", padx=27, pady=(12, 8))
        label(tools, "履歴と下書きはこのPCに保存", size=8, fg=MUTED, bg=BG).pack(side="left")
        self.archive_button = button(tools, "保管する", self.archive_current); self.archive_button.pack(side="right", padx=(7, 0))
        button(tools, "名前を変更", self.rename_current).pack(side="right")
        self.notice = RoundedPanel(page, fill="#fff5df", radius=12, padding=(16, 10))
        self.notice_label = label(self.notice.content, "", size=9, bg="#fff5df", fg="#795924", wraplength=620)
        self.notice_label.pack(side="left", fill="x", expand=True)
        button(self.notice.content, "接続・設定へ", lambda: self.show_page("settings")).pack(side="right", padx=(10, 0))
        self.composer_shell = tk.Frame(page, bg=BG, padx=28, pady=12); self.composer_shell.pack(side="bottom", fill="x")
        self.jump = button(self.composer_shell, "最新のメッセージへ ↓", lambda: self.thread.bottom(force=True))
        self.attachment_strip = tk.Frame(self.composer_shell, bg=BG); self.attachment_strip.pack(fill="x", pady=(0, 6))
        self.composer_surface = RoundedPanel(self.composer_shell, fill=WHITE, border=LINE, radius=18, padding=(18, 14))
        self.composer_surface.pack(fill="x")
        shell = self.composer_surface.content
        self.input = tk.Text(shell, wrap="word", height=3, width=1, relief="flat", borderwidth=0,
                             undo=True, maxundo=80, font=(self.root.ui_font, 11), bg=WHITE, fg=INK,
                             insertbackground=TEAL, padx=0, pady=3, highlightthickness=0, selectbackground="#d2ece1")
        self.input.pack(fill="x")
        self.placeholder = label(self.input, "質問を入力してください。資料の添付もできます。", size=10, fg=MUTED, wraplength=560, justify="left")
        self.placeholder.place(x=2, y=3)
        self.placeholder.bind("<Button-1>", lambda _: self.input.focus_set())
        self.input.bind("<<Modified>>", self.draft_changed)
        self.input.bind("<Control-Return>", self.send_shortcut)
        self.input.bind("<Control-KP_Enter>", self.send_shortcut)
        bar = tk.Frame(shell, bg=WHITE); bar.pack(fill="x", pady=(7, 0))
        self.attach_button = button(bar, "資料を添える", self.choose_documents); self.attach_button.pack(side="left")
        self.counter = label(bar, "0 / 10,000", size=8, fg=MUTED); self.counter.pack(side="left", padx=12)
        secondary = tk.Frame(self.composer_shell, bg=BG)
        self.register_work = button(secondary, "継続タスクに登録", self.register_durable_work)
        self.register_work.configure(pady=4)
        self.register_work.pack(side="left")
        label(secondary, "依頼を保存して実行状態を管理", size=8, fg=MUTED, bg=BG).pack(side="left", padx=10)
        self.send = button(bar, "送信", self.ask, primary=True); self.send.pack(side="right")
        self.stop_button = button(bar, "停止", self.stop); self.stop_button.pack(side="right", padx=7)
        secondary.pack(fill="x", pady=(10, 0))
        label(self.composer_shell, "Enterで改行  ·  Ctrl+Enterで送信  ·  Ctrl+Nで新しい会話  ·  Escで停止", size=8, fg=MUTED, bg=BG).pack(anchor="w", pady=(7, 0))
        self.thread = ScrollFrame(page, on_scroll=self.thread_scrolled); self.thread.pack(fill="both", expand=True)

    def _build_documents(self):
        page = self.pages["documents"]
        toolbar = tk.Frame(page, bg=BG); toolbar.pack(fill="x", padx=28, pady=22)
        self.query = entry(toolbar); self.query.pack(side="left", fill="x", expand=True, ipady=9)
        self.query.bind("<Return>", lambda _: self.search())
        button(toolbar, "検索", self.search).pack(side="left", padx=8)
        button(toolbar, "資料を追加", self.import_documents, primary=True).pack(side="left", padx=(0, 8))
        button(toolbar, "再走査", self.scan).pack(side="left")
        label(page, "取得済みの資料を検索し、選んだ資料を会話に添えられます。", fg=MUTED, bg=BG).pack(anchor="w", padx=28)
        self.document_list = treeview(page, columns=("kind", "observed"), selectmode="extended", height=9)
        for name, title, width in [("#0", "資料名", 360), ("kind", "種類", 90), ("observed", "取得", 110)]:
            self.document_list.heading(name, text=title, anchor="w"); self.document_list.column(name, width=width, stretch=name == "#0")
        self.document_list.pack(fill="x", padx=28, pady=16)
        self.document_list.bind("<<TreeviewSelect>>", self.preview_search)
        self.document_list.bind("<Double-1>", lambda _: self.open_selected_document())
        actions = tk.Frame(page, bg=BG); actions.pack(fill="x", padx=28, pady=(0, 12))
        button(actions, "選んだ資料を会話へ", self.attach_search_selection, primary=True).pack(side="left")
        button(actions, "原文を確認", self.open_selected_document).pack(side="left", padx=8)
        self.search_view = self.text(page)

    def _build_other_pages(self):
        page = self.pages["proposals"]
        label(page, "AIが見つけた候補を、原文と照らして確認できます。", fg=MUTED, bg=BG).pack(anchor="w", padx=28, pady=22)
        self.proposal_list = self.listbox(page, 7)
        self.proposal_list.bind("<<ListboxSelect>>", lambda _: self.show_proposal())
        actions = tk.Frame(page, bg=BG); actions.pack(fill="x", padx=28, pady=12)
        button(actions, "内容を確認して承認", lambda: self.decide(True), primary=True).pack(side="left")
        button(actions, "却下", lambda: self.decide(False)).pack(side="left", padx=8)
        self.proposal_view = self.text(page)
        page = self.pages["tasks"]
        actions = tk.Frame(page, bg=BG); actions.pack(fill="x", padx=28, pady=22)
        button(actions, "+ 案件を追加", self.add_project, primary=True).pack(side="left")
        button(actions, "選択タスクを完了", self.complete_task).pack(side="left", padx=8)
        self.task_list = self.listbox(page, 8); self.project_view = self.text(page)
        page = self.pages["notifications"]
        bar = tk.Frame(page, bg=BG); bar.pack(fill="x", padx=28, pady=22)
        label(bar, "資料の変更や期限の通知を表示します。", fg=MUTED, bg=BG).pack(side="left")
        button(bar, "すべて既読にする", self.read_notifications).pack(side="right")
        self.notification_view = self.text(page)

    def _build_settings(self):
        from .ui_settings import SettingsPanel
        self.settings_panel = SettingsPanel(self, self.pages["settings"])

    def text(self, parent, *, surface=True):
        if surface:
            panel = RoundedPanel(parent, fill=WHITE, border=LINE, padding=16)
            panel.pack(fill="both", expand=True, padx=24, pady=(0, 20)); parent = panel.content
        widget = ScrolledText(parent, wrap="word", relief="flat", borderwidth=0,
                              bg=WHITE, fg=INK, padx=0, pady=0, font=(self.root.ui_font, 10), highlightthickness=0)
        widget.vbar.configure(bg="#c8d8cf", activebackground="#adC4b6", troughcolor=WHITE,
                              relief="flat", borderwidth=0, highlightthickness=0, width=11, elementborderwidth=0)
        widget.pack(fill="both", expand=True); widget.configure(state="disabled")
        return widget

    def listbox(self, parent, height):
        panel = RoundedPanel(parent, fill=WHITE, border=LINE, padding=10)
        panel.pack(fill="x", padx=24)
        widget = tk.Listbox(panel.content, height=height, font=(self.root.ui_font, 10), relief="flat", borderwidth=0,
                            bg=WHITE, fg=INK, selectbackground=SOFT, selectforeground=TEAL,
                            highlightthickness=0, exportselection=False, activestyle="none")
        widget.pack(fill="x"); return widget

    @staticmethod
    def replace(widget, text):
        widget.configure(state="normal"); widget.delete("1.0", "end"); widget.insert("end", text); widget.configure(state="disabled")

    def copy(self, text):
        self.root.clipboard_clear(); self.root.clipboard_append(text); self.status.set("クリップボードにコピーしました")

    def _work(self, kind, function, token=None):
        def work():
            try:
                with self.rt.operation():
                    result = function()
                self.events.put((kind, token, result))
            except Exception as error: self.events.put(("worker_error", (kind, token), safe_code(error)))
        threading.Thread(target=work, daemon=True).start()

    def show_page(self, name):
        if self.current_page == "chat" and name != "chat" and not self.loading: self.save_draft()
        for page in self.pages.values(): page.pack_forget()
        self.pages[name].pack(fill="both", expand=True); self.current_page = name
        for key, nav in self.nav_buttons.items():
            nav.configure(bg=NAV_HOVER if key == name else NAV, fg=TEAL if key == name else INK)
        title = self.rt.store.session(self.current_session)["title"] if name == "chat" else PAGE_TITLES[name]
        self.section_label.configure(text=PAGE_TITLES[name])
        self.heading.configure(text=title[:26] + ("…" if len(title) > 26 else ""))
        if name == "documents" and not self.document_rows: self.search()

    def refresh_sessions(self):
        self.session_rows = self.rt.store.sessions(self.session_filter.get(), archived=self.show_archived.get())
        self.session_list.delete(0, "end")
        for i, row in enumerate(self.session_rows):
            busy = self.controller.active and self.controller.active.session == row["id"]
            self.session_list.insert("end", ("◌  " if busy else "  ") + row["title"])
            if row["id"] == self.current_session: self.session_list.selection_set(i)
        self.session_caption.configure(text=("保管した会話" if self.show_archived.get() else "最近の会話") + f"  {len(self.session_rows)}")

    def select_session(self, _=None):
        selected = self.session_list.curselection()
        if selected:
            sid = self.session_rows[selected[0]]["id"]
            if sid != self.current_session: self.load_session(sid)
            self.show_page("chat")

    def load_session(self, session, *, save=True, preserve_scroll=False):
        if save and not self.loading and not self.save_draft(): return
        self.scroll_restore = self.thread.canvas.yview()[0] if preserve_scroll and session == self.current_session and not self.thread.follow else None
        self._cancel_draft_job(); self.current_session = session
        self.rt.store.set_state("ui.active_session", session)
        self.load_version += 1; self.loading = True
        token = (self.load_version, session)
        self.pending_card, self.pending_request = None, None
        self.thread.clear(); label(self.thread.inner, "会話を読み込んでいます…", fg=MUTED, bg=BG).pack(pady=50)
        self._set_input(""); self.attached = []; self.render_attachments(); self.update_controls()
        def read():
            messages = self.rt.agent.display_history(session)
            return self.rt.store.session(session), messages
        self._work("history", read, token); self.refresh_sessions()

    def new_session(self):
        if not self.loading and not self.save_draft(): return
        self.show_archived.set(False); self.session_filter.set("")
        self.load_session(self.rt.store.new_session(), save=False); self.show_page("chat")
        return "break"

    def rename_current(self):
        name = simpledialog.askstring("会話の名前", "後から見つけやすい名前", initialvalue=self.rt.store.session(self.current_session)["title"], parent=self.root)
        if name:
            try: self.rt.store.rename_session(self.current_session, name)
            except AssistantError as error: self.status.set(str(error)); return
            self.refresh_sessions(); self.show_page("chat")

    def archive_current(self):
        if self.controller.active and self.controller.active.session == self.current_session:
            self.status.set("この会話の処理が終わるか、停止してから保管できます"); return
        if not self.save_draft(): return
        old = self.rt.store.session(self.current_session)
        self.rt.store.archive_session(self.current_session, not bool(old["archived"]))
        self.show_archived.set(False); available = self.rt.store.sessions()
        self.load_session(available[0]["id"] if available else self.rt.store.new_session(), save=False)

    def _set_input(self, value):
        self._setting_input = True
        self.input.configure(state="normal"); self.input.delete("1.0", "end"); self.input.insert("1.0", value)
        self.input.edit_reset(); self.input.edit_modified(False); self._setting_input = False

    def _cancel_draft_job(self):
        if self._draft_job: self.root.after_cancel(self._draft_job); self._draft_job = None

    def draft_changed(self, _=None):
        if not self.input.edit_modified(): return
        self.input.edit_modified(False)
        if self._setting_input or self.loading: return
        self.update_controls(); self._cancel_draft_job(); self._draft_job = self.root.after(400, self.save_draft)

    def save_draft(self):
        self._cancel_draft_job()
        if self.loading: return True
        try:
            self.rt.store.save_draft(self.current_session, self.input.get("1.0", "end-1c"), self.attached, expected_cleared_at=self.session_cleared)
            return True
        except AssistantError as error:
            if error.code == "CHAT_CHANGED":
                self.status.set("資料の更新・失効により会話を更新します"); self.load_session(self.current_session, save=False)
            else: self.status.set("下書きは50,000文字まで保存できます。内容を短くしてください")
            return False

    def focus_composer(self):
        self.show_page("chat"); self.input.focus_set(); return "break"

    def send_shortcut(self, _=None):
        self.ask(); return "break"

    def register_durable_work(self):
        text = self.input.get("1.0", "end-1c").strip()
        if not text or len(text) > 10000: return
        if self.work_panel.register(text, list(self.attached)):
            self._set_input(""); self.attached = []; self.render_attachments(); self.save_draft(); self.update_controls()

    def ask(self, *, prompt=None, document_ids=None, retry_message_id=None):
        if self.loading or self.settings_panel.busy: return
        text = self.input.get("1.0", "end-1c").strip() if prompt is None else prompt
        ids = list(self.attached if document_ids is None else document_ids)
        if not text or len(text) > 10000 or self.controller.active: return
        try: job = self.controller.send(self.current_session, text, ids, retry_message_id)
        except AssistantError as error: self.status.set(error_help(error.code)[0]); return
        self.submitted[job.request_id] = job; self.transient_errors.pop(self.current_session, None)
        if prompt is None or self.input.get("1.0", "end-1c").strip() == text:
            self._set_input(""); self.attached = []; self.render_attachments(); self.save_draft()
        self.pending_card, self.pending_request = None, None
        if retry_message_id is None:
            if not self.message_rows: self.thread.clear()
            MessageCard(self.thread.inner, role="user", text=text, status="送信済み")
        else:
            for row in self.message_rows:
                if row["id"] == retry_message_id: row["status"] = "running"
            self.render_messages()
        self.render_pending(); self.thread.bottom(force=True); self.refresh_sessions(); self.update_controls()

    def stop(self):
        self.controller.stop(); self.update_controls(); self.render_pending()
        if self.controller.active: self.status.set("停止しています。通信中の場合は応答またはタイムアウトを待ちます")
        return "break"

    def ready(self):
        from .security import validate_bedrock
        cfg = self.rt.cfg
        if not cfg.local_data_approved: return False
        if not self.rt.agent.client.is_live: return True
        try: validate_bedrock(cfg.bedrock)
        except AssistantError: return False
        return True

    def update_controls(self):
        count = len(self.input.get("1.0", "end-1c")); busy = self.controller.active is not None
        self.counter.configure(text=f"{count:,} / 10,000", fg=ERROR if count > 10000 else MUTED)
        if count: self.placeholder.place_forget()
        else: self.placeholder.place(x=2, y=3)
        self.send.configure(state="normal" if not self.loading and not busy and not self.settings_panel.busy and self.ready() and 0 < count <= 10000 else "disabled")
        self.stop_button.configure(state="normal" if busy and not self.controller.active.cancel.is_set() else "disabled")
        configuring = self.settings_panel.busy
        self.attach_button.configure(state="disabled" if self.loading or configuring else "normal")
        self.register_work.configure(state="normal" if not self.loading and not self.settings_panel.busy and self.rt.cfg.local_data_approved and 0 < count <= 10000 else "disabled")
        self.input.configure(state="disabled" if self.loading else "normal")
        if busy: self.status.set(("" if self.controller.active.session == self.current_session else "別の会話で ") + self.controller.active.progress)

    def thread_scrolled(self, at_bottom):
        if at_bottom: self.jump.pack_forget()
        elif not self.jump.winfo_manager(): self.jump.pack(before=self.attachment_strip, pady=(0, 6))

    def render_messages(self, *, scroll_to_end=True):
        self.thread.clear(); self.pending_card, self.pending_request = None, None
        if not self.message_rows: self.render_welcome()
        if self.session_cleared and not self.message_rows:
            MessageCard(self.thread.inner, text="資料の更新・失効に伴い、履歴と下書きをリセットしました。最新の資料を選んで会話を始めてください。", status="資料が更新されました")
        for index, item in enumerate(self.message_rows):
            meta = item["metadata"]
            citations = meta.get("citations", [])
            card = MessageCard(self.thread.inner, role=item["role"], text=display_answer(item["text"], citations), timestamp=short_time(item["created_at"]),
                               on_copy=(lambda _, row=item: self.copy(copy_answer(row["text"], row["metadata"].get("citations", [])))) if item["role"] == "assistant" else None)
            if item["role"] == "user" and meta.get("document_ids"):
                for did in meta["document_ids"]:
                    try: title = self.rt.store.get_document(did)["title"]
                    except AssistantError: title = "利用できない資料"
                    source_button = button(card.content, "添付資料  " + title[:40], lambda d=did: self.open_document(d))
                    source_button.configure(wraplength=540); source_button.pack(anchor="w", pady=(5, 0))
            if item["role"] == "user" and item["status"] == "completed":
                button(card.footer, "質問を編集", lambda row=item: self.edit_question(row)).pack(side="left")
            if item["status"] in ("failed", "cancelled", "interrupted"):
                title, help_text = error_help("TURN_INTERRUPTED" if item["status"] == "interrupted" else meta.get("error", "TURN_PREEMPTED"))
                label(card.content, title, bold=True, fg=ERROR if item["status"] == "failed" else MUTED, bg=card.bg).pack(anchor="w", pady=(10, 4))
                label(card.content, help_text, size=9, fg=MUTED, bg=card.bg, wraplength=620, justify="left").pack(anchor="w")
                if index == len(self.message_rows) - 1:
                    button(card.footer, "もう一度", lambda row=item: self.retry(row)).pack(side="left", padx=(8, 0))
                    button(card.footer, "質問を編集", lambda row=item: self.edit_question(row)).pack(side="left", padx=8)
            for number, source in enumerate(citations, 1):
                source_button = button(card.content, f"［{number}］ " + source["title"][:60], lambda did=source["id"]: self.open_document(did))
                source_button.configure(wraplength=540); source_button.pack(anchor="w", pady=(7, 0))
                label(card.content, "取得 " + short_time(source["observed_at"]), size=8, fg=MUTED, bg=card.bg).pack(anchor="w")
            if meta.get("trace"):
                steps = "\n".join(TOOL_LABELS.get(s["tool"], "情報の確認").replace("しています", "") + ("  ✓" if s["status"] == "ok" else "  確認できませんでした") for s in meta["trace"])
                details = label(card.content, steps, size=8, fg=MUTED, bg=card.bg, justify="left", wraplength=620)
                button(card.footer, f"確認の手順 ({len(meta['trace'])})", lambda w=details: w.pack_forget() if w.winfo_manager() else w.pack(anchor="w", pady=8)).pack(side="left", padx=8)
        if self.current_session in self.transient_errors and (not self.message_rows or self.message_rows[-1]["status"] == "completed"):
            title, help_text = error_help(self.transient_errors[self.current_session])
            card = MessageCard(self.thread.inner, text=help_text, status=title)
            button(card.footer, "接続・設定を確認", lambda: self.show_page("settings")).pack(side="left")
        self.render_pending(); self.thread.bind_wheel(self.thread.inner)
        if scroll_to_end: self.thread.bottom(force=True)

    def render_welcome(self):
        welcome = tk.Frame(self.thread.inner, bg=BG, padx=28, pady=24); welcome.pack(fill="x")
        if not self.ready():
            label(welcome, "初期設定が必要です", size=21, bold=True, bg=BG).pack(anchor="w")
            label(welcome, "「接続・設定」でローカル保存とAI接続を設定すると、質問を送信できます。入力した下書きは保持します。", fg=MUTED, bg=BG, wraplength=600, justify="left").pack(anchor="w", pady=(12, 18))
            button(welcome, "初期設定を開く", lambda: self.show_page("settings"), primary=True).pack(anchor="w")
            return
        has_documents = bool(self.rt.store.stats().get("documents", 0))
        label(welcome, "AIに質問する", size=22, bold=True, bg=BG).pack(anchor="w")
        label(welcome, "質問を入力して送信してください。資料を使う場合は「資料を追加」で取り込めます。", fg=MUTED, bg=BG, wraplength=600, justify="left").pack(anchor="w", pady=(12, 18))
        actions = tk.Frame(welcome, bg=BG); actions.pack(fill="x", pady=(0, 12))
        button(actions, "資料を追加", self.import_documents, primary=not has_documents).pack(side="left", padx=(0, 8))
        prompts = [("会議の準備", "取得済み資料から、次の会議で確認する事項を整理して。"),
                   ("資料の要約", "添えた資料の要点と不明な点を出典付きで整理して。")]
        if not has_documents:
            prompts = [("文章の整理", "次の文章を箇条書きに整理して。\n\n")]
        for title, prompt in prompts:
            button(actions, title, lambda p=prompt: self.fill_prompt(p)).pack(side="left", padx=(0, 8))
        label(welcome, "取得済みの資料やブラウザから渡した文章は「資料を添える」で選べます。", size=9, fg=MUTED, bg=BG, wraplength=600).pack(anchor="w", pady=(10, 0))

    def fill_prompt(self, prompt):
        self._set_input(prompt); self.save_draft(); self.update_controls(); self.focus_composer()

    def render_pending(self):
        job = self.controller.active
        if not job or job.session != self.current_session or self.loading: return
        if self.pending_request != job.request_id:
            self.pending_card = MessageCard(self.thread.inner, text="", status="生成中・未確定")
            self.pending_request = job.request_id
            self.pending_label = label(self.pending_card.footer, job.progress, size=9, fg=TEAL); self.pending_label.pack(side="left")
            self.thread.bind_wheel(self.pending_card)
        self.pending_card.set_text(job.text or "…"); self.pending_label.configure(text=job.progress)
        self.pending_card.set_status("停止中" if job.cancel.is_set() else "生成中・未確定"); self.thread.bottom()

    def retry(self, row):
        self.ask(prompt=row["text"], document_ids=row["metadata"].get("document_ids", []), retry_message_id=row["id"])

    def edit_question(self, row):
        if self.input.get("1.0", "end-1c").strip() and not messagebox.askyesno("下書きを置き換える", "現在の下書きをこの質問で置き換えますか？", parent=self.root): return
        self._set_input(row["text"]); self.attached = row["metadata"].get("document_ids", [])
        self.render_attachments(); self.save_draft(); self.update_controls(); self.focus_composer()

    def render_attachments(self):
        for child in self.attachment_strip.winfo_children(): child.destroy()
        for index, did in enumerate(self.attached):
            try: title = self.rt.store.get_document(did)["title"]
            except AssistantError: title = "利用できない資料"
            chip = RoundedPanel(self.attachment_strip, fill=SOFT, radius=12, padding=(8, 4)); chip.grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 6), pady=2)
            label(chip.content, title[:12] + ("…" if len(title) > 12 else ""), size=8, fg=TEAL, bg=SOFT).pack(side="left")
            remove = button(chip.content, "外す", lambda d=did: self.remove_attachment(d)); remove.configure(padx=4, pady=2); remove.pack(side="left")

    def remove_attachment(self, did):
        self.attached = [d for d in self.attached if d != did]; self.render_attachments(); self.save_draft()

    def import_documents(self):
        if self.settings_panel.busy: return
        if self.settings_panel.dirty or not self.rt.cfg.local_data_approved or not self.rt.cfg.roots:
            self.show_page("settings"); self.settings_panel.tabs.select(0)
            self.settings_panel.show_error("「保存と資料」でローカル保存の許可と資料フォルダを設定し、保存してください。", "roots")
            return
        from .security import ALLOWED_SUFFIXES, expanded
        initial = expanded(self.rt.cfg.roots[0])
        paths = filedialog.askopenfilenames(parent=self.root, title="許可した資料フォルダ内のファイルを選択",
            initialdir=str(initial) if initial.is_dir() else str(self.rt.home.parent),
            filetypes=[("対応する資料", " ".join("*" + suffix for suffix in sorted(ALLOWED_SUFFIXES))), ("すべてのファイル", "*")])
        if not paths: return
        if len(paths) > 20:
            messagebox.showinfo("資料は一度に20件まで", "20件以下に分けて追加してください。", parent=self.root); return
        from pathlib import Path
        def ingest():
            ids, failures = [], []
            for raw in paths:
                path = Path(raw)
                try: ids.append(self.rt.ingestor.file(path, return_id=True))
                except Exception as error: failures.append((path.name, safe_code(error)))
            return ids, failures
        self.status.set("資料を取り込んでいます…"); self._work("import", ingest)

    def choose_documents(self):
        dialog = tk.Toplevel(self.root); dialog.title("取得済みの資料を添える"); dialog.geometry("760x520"); dialog.minsize(640, 400)
        dialog.configure(bg=BG); dialog.transient(self.root); dialog.grab_set()
        label(dialog, "質問に添える資料", size=16, bold=True, bg=BG).pack(anchor="w", padx=22, pady=(18, 5))
        label(dialog, "最大6件。選んだ資料は質問の送信時にAIが参照します。", fg=MUTED, bg=BG, wraplength=680).pack(anchor="w", padx=22, pady=(0, 10))
        actions = tk.Frame(dialog, bg=BG); actions.pack(side="bottom", fill="x", padx=22, pady=(8, 16))
        query = tk.StringVar()
        label(dialog, "資料名・本文を検索", size=9, fg=MUTED, bg=BG).pack(anchor="w", padx=22)
        field = entry(dialog, query); field.pack(fill="x", padx=22, ipady=7, pady=(4, 0))
        empty = label(dialog, "", size=9, fg=MUTED, bg=BG, wraplength=680, justify="left"); empty.pack(fill="x", padx=22, pady=8)
        tree = treeview(dialog, show="tree", selectmode="extended", height=5); tree.pack(fill="both", expand=True, padx=22)
        def selected(*_): choose_button.configure(state="normal" if tree.selection() else "disabled")
        def populate(*_):
            for item in tree.get_children(): tree.delete(item)
            rows = self.rt.store.search(query.get(), 20) if query.get().strip() else self.rt.store.documents(100)
            seen = set()
            for row in rows:
                if row["id"] not in seen:
                    tree.insert("", "end", iid=row["id"], text=row["title"] + "  ·  " + short_time(row["observed_at"]))
                    seen.add(row["id"])
            empty.configure(text="" if rows else "一致する資料がありません。検索語を変えてください。" if query.get().strip() else "資料はまだありません。「資料を追加」からファイルを取り込んでください。")
            selected()
        def choose():
            if not tree.selection(): return
            ids = list(dict.fromkeys(self.attached + list(tree.selection())))
            if len(ids) > 6:
                messagebox.showinfo("資料は6件まで", "資料を減らして選んでください。", parent=dialog); return
            self.attached = ids; self.render_attachments(); self.save_draft(); dialog.destroy(); self.input.focus_set()
        def add(): dialog.destroy(); self.import_documents()
        choose_button = button(actions, "この資料を添える", choose, primary=True); choose_button.pack(side="right")
        button(actions, "取消", dialog.destroy).pack(side="right", padx=8)
        button(actions, "資料を追加", add).pack(side="left")
        tree.bind("<<TreeviewSelect>>", selected)
        query.trace_add("write", populate); field.bind("<Return>", lambda _: populate()); populate(); field.focus_set()

    def search(self):
        query = self.query.get().strip(); self.status.set("資料を検索しています…")
        def find():
            rows = self.rt.store.search(query, 20) if query else self.rt.store.documents(100)
            result, seen = [], set()
            for hit in rows:
                if hit["id"] in seen: continue
                try: doc = self.rt.ingestor.validate_live_source(hit["id"])
                except AssistantError: continue
                result.append({**hit, "text": hit.get("text", doc["body"][:3000])}); seen.add(hit["id"])
            return result
        self._work("search", find, query)

    def preview_search(self, _=None):
        selected = self.document_list.selection()
        if selected:
            doc = self.document_rows[selected[0]]
            self.replace(self.search_view, doc["title"] + "\n取得: " + short_time(doc["observed_at"]) + "\n\n" + doc["text"])

    def open_selected_document(self):
        selected = self.document_list.selection()
        if selected: self.open_document(selected[0])

    def attach_search_selection(self):
        ids = list(dict.fromkeys(self.attached + list(self.document_list.selection())))
        if len(ids) > 6: self.status.set("添える資料は6件までです"); return
        self.attached = ids; self.render_attachments(); self.save_draft(); self.show_page("chat"); self.input.focus_set()

    def open_document(self, did):
        self.status.set("資料の原文を確認しています…"); self._work("document", lambda: self.rt.ingestor.validate_live_source(did), did)

    def scan(self):
        self.status.set("新しい資料を確認しています…"); self._work("scan", self.rt.scan)

    def show_proposal(self):
        selected = self.proposal_list.curselection()
        if selected:
            row = self.proposal_rows[selected[0]]
            self.replace(self.proposal_view, f'{row["title"]}\n\n種類: {row["kind"]}\n期限候補: {row["due_at"] or "未設定"}\n\n原文の引用\n{row["quote"]}\n\n承認するまで確定タスクではありません。')

    def decide(self, approve):
        selected = self.proposal_list.curselection()
        if not selected: return
        item = self.proposal_rows[selected[0]]
        if approve and not messagebox.askyesno("承認の確認", f'{item["title"]}\n\n期限候補: {item["due_at"] or "未設定"}\n\n原文: {item["quote"]}\n\n自分の案件の確定事項として承認しますか？', parent=self.root): return
        def decide():
            self.rt.ingestor.validate_live_source(item["document_id"]); self.rt.store.decide_proposal(item["id"], approve)
        self._work("decided", decide)

    def add_project(self):
        name = simpledialog.askstring("案件", "案件名", parent=self.root)
        if not name: return
        goal = simpledialog.askstring("目的", "この案件で達成したいこと", parent=self.root) or ""
        keywords = simpledialog.askstring("検索の手掛かり", "カンマ区切りのキーワード", parent=self.root) or ""
        self.rt.store.add_project(name, goal, [x.strip() for x in keywords.split(",") if x.strip()]); self.refresh(schedule=False)

    def complete_task(self):
        selected = self.task_list.curselection()
        if selected and messagebox.askyesno("完了", "選択したタスクを完了にしますか？", parent=self.root):
            self.rt.store.complete_task(self.task_rows[selected[0]]["id"]); self.refresh(schedule=False)

    def read_notifications(self):
        for item in self.rt.store.notifications(unread=True): self.rt.store.mark_read(item["id"])
        self.refresh(schedule=False)

    def set_focus(self):
        self.rt.store.set_state("focus", self.focus.get())
        if self.focus.get(): self.rt.background_cancel.set()

    def open_config(self):
        path = str(self.rt.home / "config.json")
        try:
            if os.name == "nt": os.startfile(path)
            else: subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError: self.copy(path); self.status.set("設定ファイルの場所をコピーしました。テキストエディターで開いてください")

    def diagnose(self):
        from .doctor import diagnose
        self.status.set("オフライン診断中です…"); self._work("diagnosis", lambda: diagnose(self.rt.home, False))

    def refresh(self, schedule=True):
        if self.closed: return
        self.refresh_sessions()
        self.work_panel.refresh()
        for target, key, fetch in [(self.proposal_list, "proposal_rows", self.rt.store.proposals), (self.task_list, "task_rows", self.rt.store.tasks)]:
            rows = fetch()
            if rows != getattr(self, key):
                setattr(self, key, rows); target.delete(0, "end")
                for row in rows: target.insert("end", "  " + row["title"])
        projects = self.rt.store.projects()
        self.replace(self.project_view, "\n\n".join(p["name"] + "\n" + p["goal"] + "\n検索キーワード: " + "、".join(p["keywords"]) for p in projects) or "案件は登録されていません。「案件を追加」から名前・目的・検索キーワードを登録できます。")
        notifications = self.rt.store.notifications(); unread = sum(not n["read_at"] for n in notifications)
        self.nav_buttons["notifications"].configure(text=f"通知  {unread}" if unread else "通知")
        self.nav_buttons["proposals"].configure(text=f"承認待ち  {len(self.proposal_rows)}" if self.proposal_rows else "承認待ち")
        self.replace(self.notification_view, "\n\n".join(f'{"未読" if not n["read_at"] else "既読"}  ·  {short_time(n["created_at"])}\n{n["title"]}\n{n["text"]}' for n in notifications) or "通知はまだありません。重要な変化があれば、ここに表示します。")
        cfg = self.rt.cfg; live = self.rt.agent.client.is_live
        self.connection_badge.configure(text="Bedrock · 設定済み" if live and self.ready() else "接続設定が必要" if live else "オフライン試験")
        self.settings_panel.refresh()
        if not self.ready():
            self.notice_label.configure(text="AIへの接続設定が必要です。ローカル検索は先に使えます。" if cfg.local_data_approved else "まず「接続・設定」から、利用する資料とAIの設定を確認してください。")
            if not self.notice.winfo_manager(): self.notice.pack(fill="x", padx=28, pady=(0, 8), before=self.thread)
        else: self.notice.pack_forget()
        if not self.loading and (self.rt.store.session(self.current_session)["cleared_at"] or "") != self.session_cleared:
            if self.controller.active and self.controller.active.session == self.current_session: self.stop()
            self.load_session(self.current_session, save=False)
        if schedule: self.root.after(3000, self.refresh)

    def pump(self):
        if self.closed: return
        self.settings_panel.pump()
        changed = False
        for event in self.controller.drain():
            if event.kind in ("answer", "error"):
                job = self.submitted.pop(event.request_id, None)
                if event.kind == "error": self.transient_errors[event.session] = str(event.payload)
                if event.session == self.current_session:
                    if event.kind == "error" and job and not self.input.get("1.0", "end-1c").strip():
                        self._set_input(job.prompt); self.attached = job.document_ids; self.render_attachments()
                    self.save_draft(); self.load_session(event.session, save=False, preserve_scroll=True)
                self.status.set("回答が届きました" if event.kind == "answer" else error_help(str(event.payload))[0]); self.refresh_sessions()
            else: changed = True
        if changed: self.render_pending()
        for _ in range(100):
            try: kind, token, payload = self.events.get_nowait()
            except queue.Empty: break
            if kind == "history":
                if token != (self.load_version, self.current_session): continue
                state, self.message_rows = payload
                if state["cleared_at"] != self.rt.store.session(self.current_session)["cleared_at"]:
                    self.load_session(self.current_session, save=False); continue
                self.session_cleared = state["cleared_at"] or ""; self.loading = False
                self._set_input(state["draft"]); self.attached = state["document_ids"]
                self.archive_button.configure(text="保管から戻す" if state["archived"] else "保管する")
                if self.current_page == "chat": self.heading.configure(text=state["title"][:26] + ("…" if len(state["title"]) > 26 else ""))
                self.render_attachments(); self.render_messages(scroll_to_end=self.scroll_restore is None); self.update_controls()
                if self.scroll_restore is not None:
                    self.root.after(30, lambda target=token, position=self.scroll_restore: self.thread.canvas.yview_moveto(position) if target == (self.load_version, self.current_session) else None)
            elif kind == "search":
                if token != self.query.get().strip(): continue
                self.document_rows = {r["id"]: r for r in payload}
                for item in self.document_list.get_children(): self.document_list.delete(item)
                for row in payload: self.document_list.insert("", "end", iid=row["id"], text=row["title"], values=(DOCUMENT_TYPES.get(row["kind"], "資料"), short_time(row["observed_at"])))
                empty = "資料はまだありません。「資料を追加」から、許可したフォルダ内のファイルを選んでください。" if not self.rt.store.stats().get("documents", 0) else "一致する資料がありません。検索語を変えるか、「資料を追加」で対象を追加してください。"
                self.replace(self.search_view, "資料を選ぶと内容を確認できます。" if payload else empty)
                self.status.set(f"資料 {len(payload)}件")
            elif kind == "document":
                dialog = tk.Toplevel(self.root); dialog.title(payload["title"]); dialog.geometry("820x640"); dialog.configure(bg=BG)
                label(dialog, payload["title"], size=15, bold=True, bg=BG, wraplength=760).pack(anchor="w", padx=28, pady=20)
                label(dialog, "取得 " + short_time(payload["observed_at"]) + "  ·  確認期限 " + short_time(payload["expires_at"]), size=9, fg=MUTED, bg=BG).pack(anchor="w", padx=28, pady=(0, 12))
                self.replace(self.text(dialog), payload["body"]); self.status.set("資料の原文を表示しました")
            elif kind == "work_detail": self.work_panel.render(token, payload)
            elif kind == "memory_changed":
                dialog, reload_memory = token
                if dialog.winfo_exists(): reload_memory()
                self.status.set("記憶を保存しました")
            elif kind == "work_run":
                self.work_panel.refresh(); self.work_panel.select()
                self.status.set("継続タスク: " + {"completed": "完了", "waiting": "条件待ち", "idle": "実行できるタスクはありません", "disabled": "接続設定が必要です", "failed": "失敗"}.get(payload["status"], payload["status"]))
            elif kind == "import":
                ids, failures = payload
                all_ids = list(dict.fromkeys(self.attached + ids))
                if ids:
                    self.attached = all_ids[:6]
                    self.show_page("chat"); self.render_attachments(); self.save_draft()
                    if not self.message_rows: self.render_messages()
                if failures:
                    details = "\n".join(name + ": " + error_help(code)[1] for name, code in failures)
                    messagebox.showwarning("取り込めなかった資料", details, parent=self.root)
                self.status.set(f"資料を{len(ids)}件取り込みました" + (f"。{len(failures)}件は取り込めませんでした" if failures else ""))
                if len(all_ids) > 6:
                    messagebox.showinfo("資料を取り込みました", "質問には6件まで添えました。ほかの資料は「資料を添える」で選べます。", parent=self.root)
                self.document_rows = {}; self.search(); self.refresh(schedule=False)
            elif kind == "scan": self.status.set("資料の確認が完了しました"); self.search(); self.refresh(schedule=False)
            elif kind == "decided": self.status.set("候補を更新しました"); self.refresh(schedule=False)
            elif kind == "diagnosis":
                self.diagnostic_report = "オフライン診断\n\n" + "\n".join(f'{x["check"]}: {x["status"]}\n{x.get("evidence", "")}' for x in payload["checks"])
                self.info_view.pack(fill="both", expand=True, pady=(12, 0))
                self.replace(self.info_view, self.diagnostic_report); self.status.set("オフライン診断が完了しました")
            elif kind == "worker_error":
                operation, worker_token = token
                if operation == "history" and worker_token == (self.load_version, self.current_session):
                    self.loading = False; self.thread.clear(); MessageCard(self.thread.inner, text=error_help(payload)[1], status=error_help(payload)[0])
                self.status.set(error_help(payload)[0])
        self.update_controls(); self.root.after(60, self.pump)

    def close(self):
        if self.closed: return
        self.save_draft(); self.controller.stop(); self.closed = True; self.rt.close()
        for job in self.root.tk.call("after", "info"): self.root.after_cancel(job)
        self.root.destroy()

    def run(self):
        self.root.mainloop()
