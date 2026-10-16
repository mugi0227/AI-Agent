"""Plain-text Tk desktop UI. Network and indexing never run on the Tk event thread."""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from tkinter.scrolledtext import ScrolledText

from .common import AssistantError, safe_code
from .runtime import Runtime


class Desktop:
    def __init__(self, runtime: Runtime):
        self.rt, self.events = runtime, queue.Queue()
        self.root = tk.Tk()
        self.root.title("Work Assistant — ローカル業務秘書")
        self.root.geometry("1080x780")
        self.root.minsize(760, 540)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Work Assistant", font=("Segoe UI", 18, "bold")).pack(side="left")
        self.focus = tk.BooleanVar(value=self.rt.store.get_state("focus", False))
        ttk.Checkbutton(top, text="集中モード（能動通知を抑制）", variable=self.focus, command=self.set_focus).pack(side="right")
        self.status = tk.StringVar(value="設定: " + str(self.rt.home / "config.json"))
        ttk.Label(self.root, textvariable=self.status, padding=8, wraplength=1000).pack(fill="x")
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=8)
        self.chat_page = self.page("会話")
        self.chat = ScrolledText(self.chat_page, wrap="word", font=("Yu Gothic UI", 11))
        self.chat.pack(fill="both", expand=True)
        self.chat.configure(state="disabled")
        bar = ttk.Frame(self.chat_page, padding=6)
        bar.pack(fill="x")
        self.input = ttk.Entry(bar, font=("Yu Gothic UI", 11))
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Return>", lambda _: self.ask())
        self.send = ttk.Button(bar, text="送信", command=self.ask)
        self.send.pack(side="left", padx=6)
        self.search_page = self.page("資料・検索")
        row = ttk.Frame(self.search_page); row.pack(fill="x")
        self.query = ttk.Entry(row); self.query.pack(side="left", fill="x", expand=True)
        self.query.bind("<Return>", lambda _: self.search())
        ttk.Button(row, text="ローカル検索", command=self.search).pack(side="left")
        ttk.Button(row, text="再走査", command=self.scan).pack(side="left")
        self.search_view = self.text(self.search_page)
        self.proposals_page = self.page("承認待ち")
        self.proposal_list = tk.Listbox(self.proposals_page, height=8)
        self.proposal_list.pack(fill="x")
        self.proposal_list.bind("<<ListboxSelect>>", lambda _: self.show_proposal())
        row = ttk.Frame(self.proposals_page); row.pack(fill="x")
        ttk.Button(row, text="内容を確認して承認", command=lambda: self.decide(True)).pack(side="left")
        ttk.Button(row, text="却下", command=lambda: self.decide(False)).pack(side="left")
        self.proposal_view = self.text(self.proposals_page)
        self.tasks_page = self.page("タスク・案件")
        row = ttk.Frame(self.tasks_page); row.pack(fill="x")
        ttk.Button(row, text="案件を追加", command=self.add_project).pack(side="left")
        ttk.Button(row, text="選択タスクを完了", command=self.complete_task).pack(side="left")
        self.task_list = tk.Listbox(self.tasks_page, height=10); self.task_list.pack(fill="x")
        self.project_view = self.text(self.tasks_page)
        self.notifications_page = self.page("通知")
        ttk.Button(self.notifications_page, text="表示中を既読にする", command=self.read_notifications).pack(anchor="w")
        self.notification_view = self.text(self.notifications_page)
        self.info_page = self.page("状態・設定")
        self.info_view = self.text(self.info_page)
        self.proposal_rows, self.task_rows = [], []
        self.write_chat("秘書", "ローカル検索はBedrock接続なしで使えます。承認待ちの候補は、承認するまで確定タスクではありません。設定変更後はアプリを再起動してください。")
        self.rt.start()
        self.refresh()
        self.root.after(100, self.pump)

    def page(self, label):
        frame = ttk.Frame(self.tabs, padding=8); self.tabs.add(frame, text=label); return frame

    def text(self, parent):
        widget = ScrolledText(parent, wrap="word", font=("Yu Gothic UI", 10))
        widget.pack(fill="both", expand=True); widget.configure(state="disabled"); return widget

    @staticmethod
    def replace(widget, text):
        widget.configure(state="normal"); widget.delete("1.0", "end"); widget.insert("end", text); widget.configure(state="disabled")

    def write_chat(self, who, text):
        self.chat.configure(state="normal"); self.chat.insert("end", f"\n{who}\n{text}\n"); self.chat.configure(state="disabled"); self.chat.see("end")

    def set_focus(self):
        self.rt.store.set_state("focus", self.focus.get())
        if self.focus.get(): self.rt.background_cancel.set()

    def ask(self):
        text = self.input.get().strip()
        if not text or str(self.send["state"]) == "disabled": return
        self.input.delete(0, "end"); self.send.configure(state="disabled"); self.write_chat("あなた", text)
        def work():
            try:
                result = self.rt.ask(text, progress=lambda x: self.events.put(("status", x)))
                self.events.put(("answer", result))
            except Exception as error:
                self.events.put(("error", safe_code(error)))
        threading.Thread(target=work, daemon=True).start()

    def scan(self):
        def work():
            try: self.events.put(("scan", self.rt.scan()))
            except Exception as error: self.events.put(("scan", {"error": safe_code(error)}))
        threading.Thread(target=work, daemon=True).start()

    def search(self):
        result = []
        for hit in self.rt.store.search(self.query.get(), 12):
            try: doc = self.rt.ingestor.validate_live_source(hit["id"])
            except AssistantError: continue
            result.append(f'{doc["title"]}\nID: {doc["id"]}\n取得: {doc["observed_at"]}\n期限: {doc["expires_at"]}\n出典: {doc["source_url"]}\n{hit["text"]}')
        self.replace(self.search_view, "\n\n────────\n\n".join(result) or "取得済みの範囲では見つかりません。存在しないことの証明ではありません。")

    def show_proposal(self):
        selected = self.proposal_list.curselection()
        if selected:
            self.replace(self.proposal_view, json.dumps(self.proposal_rows[selected[0]], ensure_ascii=False, indent=2))

    def decide(self, approve):
        selected = self.proposal_list.curselection()
        if not selected: return
        item = self.proposal_rows[selected[0]]
        if approve and not messagebox.askyesno("承認の確認", f'{item["title"]}\n\n期限候補: {item["due_at"] or "未設定"}\n\n原文: {item["quote"]}\n\n自分の案件の確定事項として承認しますか？'): return
        try:
            self.rt.ingestor.validate_live_source(item["document_id"])
            self.rt.store.decide_proposal(item["id"], approve)
        except AssistantError as error: messagebox.showerror("処理できません", error.code)
        self.refresh(schedule=False)

    def add_project(self):
        name = simpledialog.askstring("案件", "案件名")
        if not name: return
        goal = simpledialog.askstring("目的", "この案件で達成したいこと") or ""
        keywords = simpledialog.askstring("検索の手掛かり", "カンマ区切りのキーワード") or ""
        self.rt.store.add_project(name, goal, [x.strip() for x in keywords.split(",") if x.strip()])
        self.refresh(schedule=False)

    def complete_task(self):
        selected = self.task_list.curselection()
        if selected and messagebox.askyesno("完了", "選択したタスクを完了にしますか？"):
            self.rt.store.complete_task(self.task_rows[selected[0]]["id"]); self.refresh(schedule=False)

    def read_notifications(self):
        for item in self.rt.store.notifications(unread=True): self.rt.store.mark_read(item["id"])
        self.refresh(schedule=False)

    def refresh(self, schedule=True):
        rows = self.rt.store.proposals()
        if rows != self.proposal_rows:
            self.proposal_rows = rows; self.proposal_list.delete(0, "end")
            for item in rows: self.proposal_list.insert("end", f'{item["kind"]}: {item["title"]}')
        rows = self.rt.store.tasks()
        if rows != self.task_rows:
            self.task_rows = rows; self.task_list.delete(0, "end")
            for item in rows: self.task_list.insert("end", f'{item["due_at"] or "期限なし"} | {item["title"]}')
        self.replace(self.project_view, json.dumps(self.rt.store.projects(), ensure_ascii=False, indent=2))
        notifications = self.rt.store.notifications()
        self.tabs.tab(self.notifications_page, text=f'通知 ({sum(not x["read_at"] for x in notifications)})')
        self.replace(self.notification_view, "\n\n".join(f'{"未読" if not n["read_at"] else "既読"} | {n["created_at"]}\n{n["title"]}\n{n["text"]}' for n in notifications) or "通知はありません。")
        info = {"runtime": self.rt.status, "local_data_approved": self.rt.cfg.local_data_approved,
                "bedrock_enabled": self.rt.cfg.bedrock.enabled, "heartbeat_enabled": self.rt.cfg.heartbeat_enabled,
                "counts": self.rt.store.stats(), "last_scan": self.rt.store.get_state("last_scan", {}),
                "config": str(self.rt.home / "config.json"), "notice": "業務データと設定は公開リポジトリへ保存しないでください。"}
        self.replace(self.info_view, json.dumps(info, ensure_ascii=False, indent=2))
        if schedule: self.root.after(3000, self.refresh)

    def pump(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status": self.status.set(payload)
                elif kind == "answer":
                    sources = "\n\n確認した出典:\n" + "\n".join(f'{x["title"]} | {x["source_url"]} | 取得 {x["observed_at"]}' for x in payload.citations) if payload.citations else ""
                    self.write_chat("秘書", payload.text + sources); self.send.configure(state="normal"); self.status.set("完了")
                elif kind == "error":
                    self.write_chat("システム", "処理を完了できませんでした: " + payload + "\n設定と会社PC検証手順を確認してください。自動的な認証・モデル切替はしません。")
                    self.send.configure(state="normal"); self.status.set(payload)
                elif kind == "scan": self.status.set("走査結果: " + json.dumps(payload, ensure_ascii=False))
        except queue.Empty: pass
        self.root.after(100, self.pump)

    def close(self):
        self.rt.close(); self.root.destroy()

    def run(self):
        self.root.mainloop()
