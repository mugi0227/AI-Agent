"""Desktop controls for durable work, schedules and explicitly saved memory."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, simpledialog

from .chat import copy_answer, error_help
from .common import AssistantError
from .ui_widgets import BG, MUTED, button, label

STATUS = {"queued": "実行待ち", "running": "実行中", "waiting": "条件待ち", "completed": "完了", "failed": "失敗", "cancelled": "取消済み"}


class WorkPanel:
    def __init__(self, desktop, page):
        self.app, self.rt = desktop, desktop.rt
        self.rows, self.detail_version = {}, 0
        self.agent_names = {}
        bar = tk.Frame(page, bg=BG); bar.pack(fill="x", padx=28, pady=(18, 10))
        button(bar, "新規タスク", self.new_task, primary=True).pack(side="left")
        button(bar, "定期実行", self.routines).pack(side="left", padx=8)
        button(bar, "保存した記憶", self.memories).pack(side="left")
        self.enabled = label(page, "", size=9, bg=BG, fg=MUTED); self.enabled.pack(anchor="w", padx=28, pady=(0, 10))
        self.tree = ttk.Treeview(page, columns=("status", "agent"), height=5, selectmode="browse")
        for key, title, width in [("#0", "依頼内容", 400), ("status", "状態", 100), ("agent", "担当", 150)]:
            self.tree.heading(key, text=title); self.tree.column(key, width=width, stretch=key == "#0")
        self.tree.pack(fill="x", padx=28); self.tree.bind("<<TreeviewSelect>>", lambda _: self.select())
        actions = tk.Frame(page, bg=BG); actions.pack(fill="x", padx=28, pady=10)
        button(actions, "実行", self.run_selected, primary=True).pack(side="left")
        button(actions, "取消", lambda: self.change("cancel")).pack(side="left", padx=7)
        button(actions, "失敗したタスクを再実行", lambda: self.change("retry")).pack(side="left")
        self.sources = tk.Frame(page, bg=BG); self.sources.pack(fill="x", padx=28)
        feedback = tk.Frame(page, bg=BG); feedback.pack(side="bottom", fill="x", padx=28, pady=(0, 12))
        self.feedback = feedback
        label(feedback, "結果の評価", size=9, bg=BG, fg=MUTED).pack(side="left", padx=(0, 10))
        button(feedback, "役立った", lambda: self.rate(True)).pack(side="left")
        button(feedback, "不要だった", lambda: self.rate(False)).pack(side="left", padx=8)
        self.detail = desktop.text(page)

    def selected(self):
        ids = self.tree.selection()
        return ids[0] if ids else None

    def register(self, goal, docs=None):
        if not self.rt.cfg.local_data_approved:
            self.app.status.set("「接続・設定」でローカルデータの利用設定を確認してください"); return None
        try:
            jid = self.rt.work.submit(goal, document_ids=docs)
        except AssistantError as error:
            self.app.status.set(str(error)); return None
        self.rt.wake.set(); self.refresh(); self.tree.selection_set(jid); self.select()
        self.app.show_page("work")
        self.app.status.set("登録しました" if self.rt.cfg.durable_work_enabled else "登録しました。自動実行は無効です。「実行」で手動実行できます")
        return jid

    def new_task(self):
        goal = simpledialog.askstring("新規タスク", "依頼内容", parent=self.app.root)
        if goal: self.register(goal)

    def refresh(self):
        self.agent_names = {p["id"]:p["name"] for p in self.rt.work.profiles()}
        rows = {j["id"]: j for j in self.rt.work.jobs()}
        if rows != self.rows:
            selected = self.selected(); self.rows = rows
            self.tree.delete(*self.tree.get_children())
            for jid, job in rows.items():
                self.tree.insert("", "end", iid=jid, text=("↳ " if job["parent_id"] else "") + " ".join(job["goal"].split())[:65],
                                 values=(STATUS[job["status"]], self.agent_names.get(job["agent_id"],job["agent_id"])))
            if selected in rows: self.tree.selection_set(selected)
        self.enabled.configure(text="継続タスクの自動実行: " + ("有効" if self.rt.cfg.durable_work_enabled else "無効") +
            "  ·  自動実行の変更は「接続・設定」から設定ファイルを開いて行います")
        if not rows: self.app.replace(self.detail, "継続タスクは登録されていません。「新規タスク」または会話の入力欄から登録できます。")

    def select(self):
        jid = self.selected()
        if not jid: return
        self.detail_version += 1; token = (jid, self.detail_version)
        def load():
            self.rt.store.expire()
            job = self.rt.work.job(jid); artifacts = self.rt.work.artifacts(jid)
            for source in job["result"].get("citations", []): self.rt.ingestor.validate_live_source(source["id"])
            for artifact in artifacts:
                for source in artifact["citations"]: self.rt.ingestor.validate_live_source(source["id"])
            return job, artifacts
        self.app._work("work_detail", load, token)

    def render(self, token, payload):
        if token != (self.selected(), self.detail_version): return
        job, artifacts = payload
        current = self.rt.work.job(job["id"])
        if current["updated_at"] != job["updated_at"]: self.select(); return
        result = job["result"]
        text = f'{STATUS[job["status"]]}  ·  担当: {self.agent_names.get(job["agent_id"],job["agent_id"])}  ·  試行: {job["attempts"]}/{job["max_attempts"]}\n\n{job["goal"]}\n\n'
        wait = job["wait_spec"]
        if job["status"] == "waiting":
            text += "待機条件: " + {"timer": "利用上限の更新 " + wait.get("at", "") if wait.get("reason")=="daily_budget" else "指定時刻 " + wait.get("at", ""), "children": "子タスクの終了", "bridge": "追加情報の到着", "approval": "承認待ち画面での承認・却下", "event": "資料の更新"}.get(wait.get("kind"), "条件の成立") + "\n\n"
        if job["error_code"]: text += "診断コード: " + job["error_code"] + "\n\n"
        if result.get("text"): text += copy_answer(result["text"], result.get("citations", [])) + "\n\n"
        for artifact in artifacts: text += f'成果物: {artifact["name"]}  第{artifact["version"]}版\n{copy_answer(artifact["content"], artifact["citations"])}\n\n'
        self.app.replace(self.detail, text)
        for child in self.sources.winfo_children(): child.destroy()
        seen, sources = set(), []
        for source in result.get("citations", []) + [s for a in artifacts for s in a["citations"]]:
            if source["id"] in seen: continue
            seen.add(source["id"]); sources.append(source)
        if len(sources)>2:
            button(self.sources, f"出典の一覧 ({len(sources)}件)", lambda values=sources: self.source_list(values)).pack(anchor="w", pady=(0,5))
        else:
            for source in sources:
                button(self.sources, "出典: " + source["title"][:40], lambda did=source["id"]: self.app.open_document(did)).pack(anchor="w", pady=(0, 5))

    def source_list(self, sources):
        dialog=tk.Toplevel(self.app.root); dialog.title("出典の一覧"); dialog.geometry("760x500"); dialog.configure(bg=BG)
        tree=ttk.Treeview(dialog,columns=("observed",),height=10)
        tree.heading("#0",text="資料名");tree.heading("observed",text="取得時刻")
        tree.column("#0",width=440);tree.column("observed",width=240)
        tree.pack(fill="both",expand=True,padx=20,pady=20)
        scroll=ttk.Scrollbar(tree,orient="vertical",command=tree.yview);tree.configure(yscrollcommand=scroll.set);scroll.pack(side="right",fill="y")
        for s in sources:tree.insert("","end",iid=s["id"],text=s["title"],values=(s["observed_at"],))
        def open_selected():
            if tree.selection():self.app.open_document(tree.selection()[0])
        button(dialog,"原文を確認",open_selected,primary=True).pack(anchor="w",padx=20,pady=(0,18))
        tree.bind("<Double-1>",lambda _:open_selected())

    def run_selected(self):
        jid = self.selected()
        if jid:
            self.app.status.set("継続タスクを実行しています")
            self.app._work("work_run", lambda: self.rt.work_runner.run_one(job_id=jid))

    def change(self, action):
        jid = self.selected()
        if not jid: return
        try: getattr(self.rt.work, action)(jid)
        except AssistantError as error: self.app.status.set(error_help(error.code)[0]); return
        self.refresh(); self.select(); self.rt.wake.set()

    def rate(self, helpful):
        jid = self.selected()
        if jid:
            self.rt.work.feedback(jid, helpful=helpful)
            self.app.status.set("評価を保存しました")

    def routines(self):
        dialog = tk.Toplevel(self.app.root); dialog.title("定期実行"); dialog.geometry("760x520"); dialog.configure(bg=BG)
        label(dialog, "登録した依頼を指定間隔で実行します。自動実行が有効な間だけ起動します。", size=9, bg=BG, fg=MUTED).pack(anchor="w", padx=20, pady=16)
        tree = ttk.Treeview(dialog, columns=("interval", "status"), height=6)
        for key, title in [("#0", "定期実行名"), ("interval", "間隔"), ("status", "状態")]: tree.heading(key, text=title)
        tree.pack(fill="both", expand=True, padx=20)
        def reload():
            tree.delete(*tree.get_children())
            for r in self.rt.work.routines(): tree.insert("", "end", iid=r["id"], text=r["name"], values=(str(r["interval_seconds"] // 60) + "分" if r["trigger_kind"] == "timer" else "イベント", "有効" if r["enabled"] else "無効"))
        def add():
            name = simpledialog.askstring("定期実行名", "名前", parent=dialog)
            if not name: return
            goal = simpledialog.askstring("依頼内容", "実行する依頼", parent=dialog)
            if not goal: return
            minutes = simpledialog.askinteger("実行間隔", "間隔（分）", parent=dialog, minvalue=1, maxvalue=43200, initialvalue=60)
            if minutes is None: return
            self.rt.work.add_routine(name, goal, interval_seconds=minutes * 60, enabled=True); reload(); self.rt.wake.set()
        def toggle():
            selected = tree.selection()
            if selected:
                row = next(r for r in self.rt.work.routines() if r["id"] == selected[0])
                self.rt.work.enable_routine(row["id"], not row["enabled"]); reload()
        bar = tk.Frame(dialog, bg=BG); bar.pack(fill="x", padx=20, pady=18)
        button(bar, "追加して有効にする", add, primary=True).pack(side="left")
        button(bar, "選択した定期実行の有効・無効を変更", toggle).pack(side="left", padx=8)
        reload()

    def memories(self):
        dialog = tk.Toplevel(self.app.root); dialog.title("保存した記憶"); dialog.geometry("760x560"); dialog.configure(bg=BG)
        namespace = tk.StringVar(value="main")
        label(dialog, "担当ごとに保存した記録を表示します。追加した内容はAIが参照できます。", size=9, fg=MUTED, bg=BG).pack(anchor="w", padx=20, pady=16)
        selector = ttk.Combobox(dialog, textvariable=namespace, values=[p["id"] for p in self.rt.work.profiles()], state="readonly")
        selector.pack(anchor="w", padx=20, pady=(0, 10))
        tree = ttk.Treeview(dialog, columns=("kind",), height=7); tree.heading("#0", text="記録内容"); tree.heading("kind", text="種類")
        tree.pack(fill="both", expand=True, padx=20)
        def reload():
            self.rt.agent.memory.prune(); tree.delete(*tree.get_children())
            with self.rt.store.connection() as cx:
                for r in cx.execute("SELECT * FROM memory_items WHERE namespace=? ORDER BY updated_at DESC LIMIT 100", (namespace.get(),)):
                    tree.insert("", "end", iid=r["id"], text=r["body"][:100], values=({"semantic": "知識・方針", "episodic": "出来事", "procedural": "作業手順"}[r["kind"]],))
        def add():
            body = simpledialog.askstring("記憶を追加", "保存する知識・方針", parent=dialog)
            if body:
                owner = namespace.get()
                self.app._work("memory_changed", lambda: self.rt.agent.memory.put(body, namespace=owner), (dialog, reload))
        def remove():
            selected = tree.selection()
            if selected:
                self.rt.agent.memory.forget(selected[0], namespace.get()); reload()
        bar = tk.Frame(dialog, bg=BG); bar.pack(fill="x", padx=20, pady=18)
        button(bar, "記憶を追加", add, primary=True).pack(side="left")
        button(bar, "選択した記憶を削除", remove).pack(side="left", padx=8)
        selector.bind("<<ComboboxSelected>>", lambda _: reload()); reload()
