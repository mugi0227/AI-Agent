"""Desktop setup form. Worker threads return data; only the Tk thread draws."""
from __future__ import annotations

import copy
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog

from .chat import error_help
from .common import AssistantError, safe_code, stamp
from .config import minute_of_day
from .security import expanded, validate_bedrock
from .setup_flow import SetupError, connection_fingerprint, probe_connection, validate_setup
from .ui_widgets import BG, WHITE, INK, MUTED, TEAL, LINE, SOFT, ERROR, ScrollFrame, RoundedPanel, button, entry, label

AUTH_NAMES = {"sigv4": "AWSプロファイル・SSO", "api_key": "環境変数のBedrock APIキー"}


class SettingsPanel:
    def __init__(self, app, parent):
        self.app, self.rt = app, app.rt
        self.base = copy.deepcopy(self.rt.cfg)
        self.busy, self.dirty = False, False
        self.probe_override = None
        self.events = queue.Queue()
        self.vars, self.fields, self.field_tabs = {}, {}, {}
        self.inputs, self.buttons = [], []
        self.status = tk.StringVar(value="設定は保存後すぐに反映されます。定期実行は任意です。")
        label(parent, "保存と資料、AI接続、接続確認の順に設定してください。", size=10, fg=MUTED, bg=BG).pack(anchor="w", padx=24, pady=(20, 16))
        footer_panel = RoundedPanel(parent, fill=WHITE, border=None, radius=14, padding=(16, 12))
        footer_panel.pack(side="bottom", fill="x", padx=24, pady=(12, 15))
        footer = footer_panel.content
        self.status_label = label(footer, textvariable=self.status, size=9, bg=WHITE, wraplength=650, justify="left")
        self.status_label.pack(fill="x", pady=(0, 8))
        actions = tk.Frame(footer, bg=WHITE); actions.pack(fill="x")
        self.save_button = button(actions, "設定を保存", lambda: self.submit(False)); self.save_button.pack(side="left")
        self.test_button = button(actions, "保存して接続テスト", lambda: self.submit(True), primary=True); self.test_button.pack(side="left", padx=8)
        self.chat_button = button(actions, "質問画面へ", self.go_chat); self.chat_button.pack(side="right")
        self.tabs = ttk.Notebook(parent); self.tabs.pack(fill="both", expand=True, padx=24)
        self.frames = []
        for title in ("1 保存と資料", "2 AI接続", "3 接続確認", "定期実行（任意）"):
            panel = RoundedPanel(self.tabs, fill=WHITE, border=LINE, padding=16)
            scroll = ScrollFrame(panel.content, bg=WHITE); scroll.pack(fill="both", expand=True)
            self.tabs.add(panel, text=title); self.frames.append(scroll)
        self._build_local(); self._build_connection(); self._build_review(); self._build_automation()
        for scroll in self.frames: scroll.bind_wheel(scroll.inner)
        self.refresh()

    def variable(self, name, value, boolean=False):
        var = tk.BooleanVar(value=value) if boolean else tk.StringVar(value=str(value))
        self.vars[name] = var
        var.trace_add("write", lambda *_: self.changed())
        return var

    def field(self, tab, name, title, value, hint=""):
        parent = self.frames[tab].inner
        row = tk.Frame(parent, bg=WHITE); row.pack(fill="x", pady=(0, 12))
        label(row, title, size=10, bold=True, bg=WHITE).pack(anchor="w")
        widget = entry(row, self.variable(name, value)); widget.pack(fill="x", ipady=7, pady=(4, 0))
        if hint: label(row, hint, size=9, fg=MUTED, bg=WHITE, wraplength=620, justify="left").pack(fill="x", pady=(3, 0))
        widget.field_row = row
        self.fields[name], self.field_tabs[name] = widget, tab
        self.inputs.append(widget)
        return widget

    def check(self, tab, name, title, value):
        widget = tk.Checkbutton(self.frames[tab].inner, text=title, variable=self.variable(name, value, True),
            bg=WHITE, fg=INK, activebackground=WHITE, selectcolor=WHITE, anchor="w", justify="left", wraplength=610,
            font=(self.app.root.ui_font, 10), padx=0, takefocus=True, highlightthickness=0)
        widget.pack(fill="x", pady=(0, 12)); self.inputs.append(widget)
        self.fields[name], self.field_tabs[name] = widget, tab
        return widget

    def _build_local(self):
        parent = self.frames[0].inner
        label(parent, "会話と取得した資料は、このPCのアプリ保存先に保存します。", fg=MUTED, bg=WHITE, wraplength=620).pack(fill="x", pady=(0, 12))
        self.check(0, "local_data_approved", "会社の利用条件を確認し、会話と選択した資料のローカル保存を許可する", self.base.local_data_approved)
        label(parent, "利用する資料フォルダ（資料を使う場合）", bold=True, bg=WHITE).pack(anchor="w")
        label(parent, "選んだフォルダ内の対応ファイルを検索対象にします。", size=9, fg=MUTED, bg=WHITE).pack(anchor="w", pady=(4, 8))
        self.roots = tk.Listbox(parent, height=3, font=(self.app.root.ui_font, 10), exportselection=False, bg=WHITE, fg=INK, selectbackground=SOFT, selectforeground=TEAL, relief="flat", highlightthickness=1, highlightbackground=LINE, highlightcolor=TEAL, borderwidth=0)
        self.roots.pack(fill="x"); self.inputs.append(self.roots)
        self.fields["roots"], self.field_tabs["roots"] = self.roots, 0
        for root in self.base.roots: self.roots.insert("end", root)
        bar = tk.Frame(parent, bg=WHITE); bar.pack(fill="x", pady=(8, 14))
        for title, command in (("フォルダを追加", self.add_root), ("選択を削除", self.remove_root)):
            widget = button(bar, title, command); widget.pack(side="left", padx=(0, 8)); self.buttons.append(widget)
        label(parent, "対応形式: TXT・Markdown・CSV・JSON・HTML・EML・VTT・Word・Excel・PowerPoint。PDFは追加ライブラリが必要です。", size=9, fg=MUTED, bg=WHITE, wraplength=620, justify="left").pack(fill="x", pady=(0, 12))
        label(parent, "資料なしでも質問できます。資料の追加は設定後の「資料を追加」から行います。", size=9, fg=MUTED, bg=WHITE, wraplength=620, justify="left").pack(fill="x", pady=(0, 16))
        button(parent, "次へ：AI接続", lambda: self.tabs.select(1)).pack(anchor="e")

    def add_root(self):
        if self.busy: return
        path = filedialog.askdirectory(parent=self.app.root, title="利用する資料フォルダを選択", mustexist=True)
        if path and path not in self.roots.get(0, "end"):
            self.roots.insert("end", path); self.changed()

    def remove_root(self):
        if self.busy: return
        for index in reversed(self.roots.curselection()): self.roots.delete(index)
        self.changed()

    def _build_connection(self):
        parent = self.frames[1].inner; cfg = self.base.bedrock
        label(parent, "会社から指定された地域・モデルID・認証方式を入力してください。", size=9, fg=MUTED, bg=WHITE, wraplength=620).pack(fill="x", pady=(0, 14))
        self.check(1, "enabled", "AIへの接続を有効にする", cfg.enabled)
        self.field(1, "region", "AWSの地域", cfg.region, "例: ap-northeast-1。地域を変えると標準の接続先も更新します。")
        self.field(1, "model", "モデルID・推論プロファイルID", cfg.model, "会社で利用を許可されたIDを指定してください。")
        label(parent, "認証方式", bold=True, bg=WHITE).pack(anchor="w")
        auth = ttk.Combobox(parent, textvariable=self.variable("auth", AUTH_NAMES[cfg.auth]), values=list(AUTH_NAMES.values()), state="readonly")
        auth.pack(fill="x", pady=(5, 12)); self.inputs.append(auth)
        self.field(1, "profile", "AWSプロファイル名", cfg.profile, "AWSプロファイル・SSO方式で使用。空欄は既定の認証設定を使用します。SSOは会社の手順で事前にログインしてください。")
        self.field(1, "api_key_env", "APIキーの環境変数名", cfg.api_key_env, "APIキー方式で使用。キー本体は入力しません。会社の手順で登録した環境変数名を指定します。")
        self.check(1, "data_transfer_approved", "会社の承認範囲で、質問・参照資料をBedrockへ送信することを許可する", cfg.data_transfer_approved)
        self.check(1, "cross_region_approved", "推論プロファイルの処理地域を確認し、地域をまたぐ処理を許可する", cfg.cross_region_approved)
        label(parent, "Bedrock Runtimeの推論プロファイルを使う場合、処理地域の許可も必要です。", size=9, fg=MUTED, bg=WHITE, wraplength=620).pack(fill="x", pady=(0, 14))
        self.advanced = tk.BooleanVar(value=False)
        advanced = tk.Frame(parent, bg=WHITE)
        toggle = tk.Checkbutton(parent, text="接続先・証明書・利用上限を表示", variable=self.advanced, bg=WHITE, font=(self.app.root.ui_font, 10), command=lambda: advanced.pack(fill="x") if self.advanced.get() else advanced.pack_forget())
        toggle.pack(anchor="w", pady=(0, 10)); self.inputs.append(toggle)
        # Optional fields use the same validation and persistence as the main form.
        for name, title, value, hint in (
            ("endpoint", "接続先URL", cfg.endpoint, "会社指定のBedrock Runtime / Mantle URL。HTTPSの検証は常に有効です。"),
            ("ca_bundle", "会社のCA証明書ファイル（任意）", cfg.ca_bundle, "空欄はOS・Pythonの既定の信頼設定を使用します。"),
            ("daily_calls", "1日のAI呼び出し上限", cfg.daily_calls, "接続テストも最大2回分を使用します。"),
            ("daily_tokens", "1日のトークン上限", cfg.daily_tokens, "")):
            row = tk.Frame(advanced, bg=WHITE); row.pack(fill="x", pady=(0, 12))
            label(row, title, bold=True, bg=WHITE).pack(anchor="w")
            widget = entry(row, self.variable(name, value)); widget.pack(fill="x", ipady=7, pady=4)
            if hint: label(row, hint, size=9, fg=MUTED, bg=WHITE, wraplength=620).pack(fill="x")
            self.fields[name], self.field_tabs[name] = widget, 1; self.inputs.append(widget)
        self.advanced_frame = advanced
        self._last_region = cfg.region
        self.vars["region"].trace_add("write", self.region_changed)
        self.vars["auth"].trace_add("write", self.auth_changed)
        self.auth_changed()
        button(parent, "次へ：接続確認", lambda: self.tabs.select(2)).pack(anchor="e", pady=10)

    def auth_changed(self, *_):
        is_profile = self.vars["auth"].get() == AUTH_NAMES["sigv4"]
        for name, visible in (("profile", is_profile), ("api_key_env", not is_profile)):
            row = self.fields[name].field_row
            if visible: row.pack(fill="x", pady=(0, 12), before=self.fields["data_transfer_approved"])
            else: row.pack_forget()

    def region_changed(self, *_):
        value = self.vars["region"].get().strip(); endpoint = self.vars["endpoint"]
        for template in ("https://bedrock-runtime.{}.amazonaws.com/openai/v1", "https://bedrock-mantle.{}.api.aws/openai/v1"):
            if endpoint.get() == template.format(self._last_region): endpoint.set(template.format(value)); break
        self._last_region = value

    def _build_review(self):
        parent = self.frames[2].inner
        self.summary = label(parent, "", size=11, bg=WHITE, wraplength=620, justify="left"); self.summary.pack(fill="x", pady=(0, 15))
        label(parent, "下の「保存して接続テスト」で、AIの応答とツール呼び出しを確認します。テスト用の短い文字列のみを送り、業務資料や会話履歴は送信しません。最大2回のAI呼び出しが発生し、利用料金と利用上限の対象になります。", fg=MUTED, size=9, bg=WHITE, wraplength=620, justify="left").pack(fill="x", pady=(0, 15))
        self.probe_label = label(parent, "", bold=True, bg=WHITE, wraplength=620, justify="left"); self.probe_label.pack(fill="x", pady=(0, 15))
        button(parent, "オフライン診断", self.app.diagnose).pack(anchor="w")
        self.app.info_view = self.app.text(parent, surface=False)
        self.app.info_view.configure(height=10)
        self.app.info_view.pack_forget()

    def _build_automation(self):
        cfg = self.base
        self.check(3, "durable_work_enabled", "保存した継続タスク・定期実行を自動で実行する", cfg.durable_work_enabled)
        self.check(3, "heartbeat_enabled", "資料の変化や期限を定期的に確認する", cfg.heartbeat_enabled)
        label(self.frames[3].inner, "アプリを起動している間に動作します。自動実行を無効にしても、継続タスクは「実行管理」から手動実行できます。", size=9, fg=MUTED, bg=WHITE, wraplength=620).pack(fill="x", pady=(0, 15))
        self.field(3, "active_start", "定期確認の開始時刻", cfg.active_start, "HH:MM形式。例: 08:00。")
        self.field(3, "active_end", "定期確認の終了時刻", cfg.active_end)
        self.field(3, "notification_daily_limit", "1日の通知上限", cfg.notification_daily_limit)

    def changed(self):
        self.dirty = True
        if hasattr(self, "chat_button"): self.chat_button.configure(state="disabled")
        self.status.set("変更は未保存です。「設定を保存」または「保存して接続テスト」で反映してください。")

    def collect(self):
        cfg = copy.deepcopy(self.base)
        for name in ("local_data_approved", "durable_work_enabled", "heartbeat_enabled"):
            setattr(cfg, name, self.vars[name].get())
        cfg.roots = list(self.roots.get(0, "end"))
        for name in ("active_start", "active_end"):
            value = self.vars[name].get().strip()
            try: minute_of_day(value)
            except AssistantError: raise SetupError(name, "時刻をHH:MM形式で入力してください。例: 08:00。") from None
            setattr(cfg, name, value)
        if cfg.active_start == cfg.active_end: raise SetupError("active_end", "終了時刻は開始時刻と異なる時刻を指定してください。")
        for name, low, high, target in (("notification_daily_limit", 1, 50, cfg), ("daily_calls", 1, 10000, cfg.bedrock), ("daily_tokens", 1000, 10000000, cfg.bedrock)):
            try:
                value = int(self.vars[name].get())
                if not low <= value <= high: raise ValueError()
            except ValueError: raise SetupError(name, f"{low:,}〜{high:,}の整数を入力してください。") from None
            setattr(target, name, value)
        for name in ("enabled", "data_transfer_approved", "cross_region_approved", "region", "model", "profile", "api_key_env", "endpoint", "ca_bundle"):
            value = self.vars[name].get(); setattr(cfg.bedrock, name, value.strip() if isinstance(value, str) else value)
        cfg.bedrock.auth = next((key for key, title in AUTH_NAMES.items() if title == self.vars["auth"].get()), "")
        validate_setup(cfg, self.rt.home)
        if cfg.bedrock.ca_bundle and not expanded(cfg.bedrock.ca_bundle).is_file():
            raise SetupError("ca_bundle", "CA証明書ファイルが見つかりません。ファイルの場所を確認してください。")
        return cfg

    def show_error(self, message, field=None):
        self.status.set(message); self.status_label.configure(fg=ERROR)
        if field in self.fields:
            index = self.field_tabs[field]; self.tabs.select(index)
            if field in ("endpoint", "ca_bundle", "daily_calls", "daily_tokens"):
                self.advanced.set(True); self.advanced_frame.pack(fill="x")
            widget = self.fields[field]
            def reveal():
                if self.app.closed: return
                scroll = self.frames[index]
                y = widget.winfo_rooty() - scroll.inner.winfo_rooty()
                scroll.canvas.yview_moveto(max(0, (y - 40) / max(1, scroll.inner.winfo_height())))
                widget.focus_set()
            self.app.root.after_idle(reveal)

    def submit(self, test=False):
        if self.busy: return
        try:
            candidate = self.collect()
            if test:
                if not candidate.local_data_approved:
                    raise SetupError("local_data_approved", "会話と資料のローカル保存の許可を確認してください。")
                try: validate_bedrock(candidate.bedrock)
                except AssistantError as error:
                    field = "cross_region_approved" if error.code == "CROSS_REGION_NOT_APPROVED" else "enabled" if not candidate.bedrock.enabled else "data_transfer_approved"
                    raise SetupError(field, error_help(error.code)[1]) from None
        except SetupError as error:
            self.show_error(error.message, error.field); return
        except AssistantError:
            self.show_error("設定の値を確認してください。入力内容は保持しています。"); return
        self.busy = True; self.status_label.configure(fg=TEAL)
        self.status.set("設定を保存しています…" if not test else "設定を保存し、テスト用の入力でAI接続を確認しています…")
        self.set_busy()
        def work():
            saved = False
            try:
                self.rt.configure(candidate); saved = True
                if test:
                    with self.rt.operation():
                        probe_connection(candidate, self.rt.store)
                    self.rt.store.set_state("ui.connection_probe", {"fingerprint": connection_fingerprint(candidate), "status": "success", "at": stamp()})
                self.events.put((candidate, saved, test, None))
            except Exception as error:
                code = safe_code(error)
                if saved and test:
                    try:
                        self.rt.store.set_state("ui.connection_probe", {"fingerprint": connection_fingerprint(candidate), "status": "failed", "code": code, "at": stamp()})
                    except Exception:
                        pass  # Still return the failure so the UI always releases its busy state.
                self.events.put((candidate, saved, test, (code, error.message if isinstance(error, SetupError) else "", getattr(error, "field", None))))
        threading.Thread(target=work, daemon=True, name="assistant-setup").start()

    def set_busy(self):
        for widget in self.inputs + self.buttons + [self.save_button, self.test_button]:
            state = "disabled" if self.busy else "readonly" if isinstance(widget, ttk.Combobox) else "normal"
            widget.configure(state=state)
        self.chat_button.configure(state="normal" if not self.busy and not self.dirty and self.app.ready() else "disabled")

    def pump(self):
        try: candidate, saved, test, error = self.events.get_nowait()
        except queue.Empty: return
        self.busy = False
        if saved: self.base = candidate; self.dirty = False
        if saved and test:
            self.probe_override = {"fingerprint": connection_fingerprint(candidate), "status": "failed" if error else "success", "at": stamp()}
        self.set_busy(); self.refresh()
        if error:
            code, message, field = error
            title, help_text = error_help(code)
            if code == "SETTINGS_BUSY": help_text = "回答・資料の取り込み・継続タスクの処理が終わってから、もう一度保存してください。入力内容は保持しています。"
            elif not saved and not message: help_text = "設定を保存できませんでした。保存先の空き容量と書き込み権限を確認して再試行してください。入力内容と元の設定は保持しています。"
            self.show_error(message or (title + "。" + help_text), field)
        elif test:
            self.tabs.select(2); self.status.set("接続テストに成功しました。「質問画面へ」から質問できます。")
            self.status_label.configure(fg=TEAL)
        else:
            suffix = "「質問画面へ」から質問できます。" if self.app.ready() else "次に「AI接続」で必要な項目を設定してください。"
            self.status.set("設定を保存して反映しました。" + suffix); self.status_label.configure(fg=TEAL)
        self.app.diagnostic_report = None
        self.app.info_view.pack_forget()
        self.app.refresh(schedule=False)
        if not self.app.message_rows and not self.app.loading: self.app.render_messages()

    def refresh(self):
        cfg = self.rt.cfg
        self.summary.configure(text="ローカル保存: " + ("許可済み" if cfg.local_data_approved else "未設定") +
            f"\n資料フォルダ: {len(cfg.roots)}件（資料なしでも質問できます）\nAI接続の設定: " + ("設定済み" if self.app.ready() else "未完了"))
        result = self.rt.store.get_state("ui.connection_probe", {})
        if self.probe_override and self.probe_override["fingerprint"] == connection_fingerprint(cfg): result = self.probe_override
        current = result.get("fingerprint") == connection_fingerprint(cfg)
        if current and result.get("status") == "success":
            text = "前回の接続テスト: 成功  " + result.get("at", "")[:16].replace("T", " ")
        elif current and result.get("status") == "failed": text = "前回の接続テスト: 失敗。設定と認証を確認し、再試行してください。"
        else: text = "接続は未確認です。設定済みでも、認証や通信が成功するかは接続テストで確認してください。"
        if self.dirty: text += "\n変更中の設定は、保存して接続テストを行うまで未確認です。"
        self.probe_label.configure(text=text)
        self.chat_button.configure(state="normal" if not self.busy and not self.dirty and self.app.ready() else "disabled")

    def go_chat(self):
        if self.busy or self.dirty or not self.app.ready(): return
        self.app.show_page("chat"); self.app.focus_composer()
