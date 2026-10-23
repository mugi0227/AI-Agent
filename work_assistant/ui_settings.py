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
from .bedrock import BedrockClient
from .credentials import AccessKeys
from .security import expanded, model_api, validate_bedrock
from .model_catalog import list_models
from .setup_flow import SetupError, connection_fingerprint, probe_connection, validate_setup
from .ui_widgets import BG, WHITE, INK, MUTED, ACCENT, LINE, SOFT, ERROR, ScrollFrame, RoundedPanel, button, entry, label

AUTH_NAMES = {"sigv4": "AWSプロファイル・SSO", "access_keys": "Access Key・Secret Access Key", "api_key": "環境変数のBedrock APIキー"}


class SettingsPanel:
    def __init__(self, app, parent):
        self.app, self.rt = app, app.rt
        self.base = copy.deepcopy(self.rt.cfg)
        self.busy, self.dirty = False, False
        self.probe_override = None
        self.events = queue.Queue()
        self.catalog_events = queue.Queue()
        self.catalog_choices = []
        self.catalog_context = None
        self.catalog_loading = False
        self.secret_vars = {}
        self.secret_revision = 0
        self.clearing_secrets = self.delete_credentials = False
        self.cleanup_pending = self.rt.credentials.has_retired(self.base.bedrock.credential_ref)
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
        self.roots = tk.Listbox(parent, height=3, font=(self.app.root.ui_font, 10), exportselection=False, bg=WHITE, fg=INK, selectbackground=SOFT, selectforeground=ACCENT, relief="flat", highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT, borderwidth=0)
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
        label(parent, "地域と認証を設定し、「モデル一覧を取得」から利用するAIを選んでください。", size=9, fg=MUTED, bg=WHITE, wraplength=620).pack(fill="x", pady=(0, 14))
        self.check(1, "enabled", "AIへの接続を有効にする", cfg.enabled)
        self.field(1, "region", "AWSの地域", cfg.region, "例: ap-northeast-1。地域を変えると標準の接続先も更新します。")
        label(parent, "認証方式", bold=True, bg=WHITE).pack(anchor="w")
        auth = ttk.Combobox(parent, textvariable=self.variable("auth", AUTH_NAMES[cfg.auth]), values=list(AUTH_NAMES.values()), state="readonly")
        auth.pack(fill="x", pady=(5, 12)); self.inputs.append(auth)
        self.field(1, "profile", "AWSプロファイル名", cfg.profile, "AWSプロファイル・SSO方式で使用。空欄は既定の認証設定を使用します。SSOは会社の手順で事前にログインしてください。")
        self.field(1, "api_key_env", "APIキーの環境変数名", cfg.api_key_env, "APIキー方式で使用。キー本体は入力しません。会社の手順で登録した環境変数名を指定します。")
        self.key_frame = tk.Frame(parent, bg=WHITE)
        self.key_status = label(self.key_frame, "", fg=MUTED, size=9, bg=WHITE, wraplength=620, justify="left")
        self.key_status.pack(fill="x", pady=(0, 10))
        for name, title in (("access_key_id", "Access Key ID"), ("secret_access_key", "Secret Access Key"), ("session_token", "Session Token（一時認証の場合）")):
            row = tk.Frame(self.key_frame, bg=WHITE); row.pack(fill="x", pady=(0, 10))
            label(row, title, bold=True, bg=WHITE).pack(anchor="w")
            var = tk.StringVar(value=""); self.secret_vars[name] = var
            widget = entry(row, var, show="●", exportselection=False); widget.pack(fill="x", ipady=7, pady=(4, 0))
            var.trace_add("write", self.secret_changed)
            self.fields[name], self.field_tabs[name] = widget, 1; self.inputs.append(widget)
        bar = tk.Frame(self.key_frame, bg=WHITE); bar.pack(fill="x", pady=(0, 12))
        self.show_keys = tk.BooleanVar(value=False)
        show = tk.Checkbutton(bar, text="入力値を表示", variable=self.show_keys, command=self.toggle_keys,
            bg=WHITE, fg=INK, activebackground=WHITE, selectcolor=WHITE, highlightthickness=0, font=(self.app.root.ui_font, 10))
        show.pack(side="left"); self.inputs.append(show)
        self.remove_keys = button(bar, "保存済みキーを削除", self.mark_delete_credentials)
        self.remove_keys.pack(side="right"); self.buttons.append(self.remove_keys)
        model_bar = tk.Frame(parent, bg=WHITE); model_bar.pack(fill="x", pady=(4, 6)); self.model_bar = model_bar
        label(model_bar, "利用するモデル", bold=True, bg=WHITE).pack(side="left")
        self.catalog_button = button(model_bar, "モデル一覧を取得", self.load_catalog)
        self.catalog_button.pack(side="right"); self.buttons.append(self.catalog_button)
        self.model_selection = tk.StringVar(value="地域と認証を設定して一覧を取得してください")
        self.model_combo = ttk.Combobox(parent, textvariable=self.model_selection, values=[], state="disabled", width=40)
        self.model_combo.pack(fill="x", pady=(0, 6))
        self.model_combo.bind("<<ComboboxSelected>>", self.select_model)
        self.catalog_label = label(parent, "OpenAI・Claudeに対応。一覧取得は設定を保存せず、AI推論やモデルの利用申込みも行いません。", fg=MUTED, bg=WHITE, size=9, wraplength=620, justify="left")
        self.catalog_label.pack(fill="x", pady=(0, 12))
        self.field(1, "model", "モデルID・推論プロファイルID（手入力も可）", cfg.model, "一覧で選ぶとIDと接続先を設定します。会社で許可されたモデルを使用してください。")
        self.check(1, "data_transfer_approved", "会社の承認範囲で、質問・参照資料をBedrockへ送信することを許可する", cfg.data_transfer_approved)
        self.check(1, "cross_region_approved", "推論プロファイルの処理地域を確認し、地域をまたぐ処理を許可する", cfg.cross_region_approved)
        label(parent, "地域をまたぐ推論プロファイルでは処理地域の許可も必要です。Claudeのリージョン内モデルでは不要です。", size=9, fg=MUTED, bg=WHITE, wraplength=620).pack(fill="x", pady=(0, 14))
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
        self.vars["model"].trace_add("write", self.model_changed)
        for name in ("region", "auth", "profile", "api_key_env", "ca_bundle"):
            self.vars[name].trace_add("write", self.invalidate_catalog)
        self.auth_changed()
        button(parent, "次へ：接続確認", lambda: self.tabs.select(2)).pack(anchor="e", pady=10)

    def auth_changed(self, *_):
        auth = self.vars["auth"].get()
        for name, visible in (("profile", auth == AUTH_NAMES["sigv4"]), ("api_key_env", auth == AUTH_NAMES["api_key"])):
            row = self.fields[name].field_row
            if visible: row.pack(fill="x", pady=(0, 12), before=self.model_bar)
            else: row.pack_forget()
        if auth == AUTH_NAMES["access_keys"]: self.key_frame.pack(fill="x", before=self.model_bar)
        else: self.key_frame.pack_forget()
        self.show_keys.set(False); self.toggle_keys(); self.refresh_key_status()

    def toggle_keys(self):
        for name in self.secret_vars: self.fields[name].configure(show="" if self.show_keys.get() else "●")

    def secret_changed(self, *_):
        if self.clearing_secrets: return
        self.secret_revision += 1; self.delete_credentials = False
        self.changed(); self.invalidate_catalog(); self.refresh_key_status()

    def clear_secret_inputs(self):
        self.clearing_secrets = True
        try:
            for var in self.secret_vars.values(): var.set("")
            self.show_keys.set(False); self.toggle_keys()
        finally: self.clearing_secrets = False

    def refresh_key_status(self):
        if self.delete_credentials: text = "保存済みキーは、次の「設定を保存」で削除します。"
        elif self.cleanup_pending: text = "古い暗号化キーの削除が未完了です。「設定を保存」で削除を再試行できます。"
        elif self.base.bedrock.credential_ref: text = "キーは保存済みです。空欄のまま使用できます。更新するときはAccess Key IDとSecret Access Keyを両方入力してください。"
        else: text = "Access Key IDとSecret Access Keyを入力してください。一時認証ではSession Tokenも必要です。"
        self.key_status.configure(text=text + " 保存にはWindowsのユーザー別暗号化を使用し、保存後は入力欄を消去します。")
        self.remove_keys.configure(state="normal" if not self.busy and self.base.bedrock.credential_ref and not self.delete_credentials else "disabled")

    def mark_delete_credentials(self):
        if self.busy: return
        self.clear_secret_inputs(); self.delete_credentials = True; self.secret_revision += 1
        self.changed(); self.invalidate_catalog(); self.refresh_key_status()

    def collect_credentials(self, *, allow_cleanup=False):
        if self.vars["auth"].get() != AUTH_NAMES["access_keys"]: return None
        values = [v.get() for v in self.secret_vars.values()]
        if not any(values):
            if not self.base.bedrock.credential_ref or self.delete_credentials:
                if allow_cleanup and self.cleanup_pending: return None
                raise SetupError("access_key_id", "Access Key IDとSecret Access Keyを入力してください。")
            return None
        keys = AccessKeys(*(self.secret_vars[n].get() for n in ("access_key_id", "secret_access_key", "session_token")))
        try: keys.validate()
        except AssistantError as error:
            field = "access_key_id" if "ID_INVALID" in error.code else "secret_access_key" if "SECRET" in error.code else "session_token"
            raise SetupError(field, error_help(error.code)[1]) from None
        return keys

    def region_changed(self, *_):
        value = self.vars["region"].get().strip(); endpoint = self.vars["endpoint"]
        for template in ("https://bedrock-runtime.{}.amazonaws.com/openai/v1", "https://bedrock-runtime.{}.amazonaws.com", "https://bedrock-mantle.{}.api.aws/openai/v1"):
            if endpoint.get() == template.format(self._last_region): endpoint.set(template.format(value)); break
        self._last_region = value

    def model_changed(self, *_):
        mid, region = self.vars["model"].get().strip(), self.vars["region"].get().strip()
        matches = [c for c in self.catalog_choices if c.model == mid]
        self.model_selection.set(matches[0].title if matches else "モデルIDを手入力中" if self.catalog_choices else "地域と認証を設定して一覧を取得してください")
        try: api = model_api(mid)
        except AssistantError: return
        endpoint = self.vars["endpoint"]
        root = f"https://bedrock-runtime.{region}.amazonaws.com"
        if endpoint.get().rstrip("/") in (root, root + "/openai/v1"):
            target = root if api == "converse" else root + "/openai/v1"
            if endpoint.get() != target: endpoint.set(target)

    def catalog_key(self):
        return tuple(self.vars[n].get().strip() for n in ("region", "auth", "profile", "api_key_env", "ca_bundle")) + (str(self.secret_revision),)

    def invalidate_catalog(self, *_):
        if self.catalog_context is not None and self.catalog_key() != self.catalog_context:
            self.catalog_choices = []; self.model_combo.configure(values=[], state="disabled")
            self.model_selection.set("地域・認証が変更されました。モデル一覧を再取得してください")
            self.catalog_label.configure(text="一覧は以前の接続設定のものです。「モデル一覧を取得」で更新してください。", fg=MUTED)

    def load_catalog(self):
        if self.busy: return
        cfg = copy.deepcopy(self.base.bedrock)
        for name in ("region", "profile", "api_key_env", "ca_bundle"):
            setattr(cfg, name, self.vars[name].get().strip())
        cfg.auth = next((k for k, title in AUTH_NAMES.items() if title == self.vars["auth"].get()), "")
        try: keys = self.collect_credentials()
        except SetupError as error:
            self.show_error(error.message, error.field); return
        client = BedrockClient(cfg, credential_store=self.rt.credentials, credentials=keys) if cfg.auth == "access_keys" else None
        key = self.catalog_key()
        self.busy = self.catalog_loading = True; self.set_busy()
        self.catalog_label.configure(text="AWSから対応モデルと利用条件を取得しています…", fg=ACCENT)
        self.status.set("モデル一覧を取得しています。入力内容は保存しません。")
        def work():
            try: self.catalog_events.put((key, list_models(cfg, client=client) if client else list_models(cfg), None))
            except Exception as error: self.catalog_events.put((key, None, safe_code(error)))
        threading.Thread(target=work, daemon=True, name="assistant-model-catalog").start()

    def select_model(self, _=None):
        if self.busy or self.catalog_context != self.catalog_key(): return
        choice = next((c for c in self.catalog_choices if c.title == self.model_selection.get()), None)
        if choice is None: return
        self.vars["model"].set(choice.model)
        root = f"https://bedrock-runtime.{self.vars['region'].get().strip()}.amazonaws.com"
        self.vars["endpoint"].set(root if choice.api == "converse" else root + "/openai/v1")
        api = "Claude / Converse" if choice.api == "converse" else "OpenAI / Responses"
        self.catalog_label.configure(text=f"{api}。処理先: {', '.join(choice.regions)}。一覧の状態は推論権限の保証ではありません。保存して接続テストを行ってください。", fg=MUTED)

    def pump_catalog(self):
        try: key, result, error = self.catalog_events.get_nowait()
        except queue.Empty: return
        self.busy = self.catalog_loading = False
        self.catalog_choices = []; self.model_combo.configure(values=[])
        if key != self.catalog_key():
            self.catalog_context = key; self.invalidate_catalog()
            self.status.set("取得中に接続設定が変わりました。モデル一覧を再取得してください。")
        elif error:
            title, help_text = error_help(error)
            self.catalog_label.configure(text=title + "。" + help_text + " 一覧取得にはListFoundationModels・ListInferenceProfilesの権限が必要です。IDの手入力もできます。", fg=ERROR)
            self.model_selection.set("一覧を取得できませんでした。再取得またはIDの手入力ができます")
            self.status.set("モデル一覧を取得できませんでした。認証・地域・一覧取得の権限を確認してください。")
        else:
            self.catalog_context = key; self.catalog_choices = result.choices
            self.model_combo.configure(values=[c.title for c in result.choices])
            self.model_changed()
            if not result.choices: self.model_selection.set("選択可能な対応モデルがありません。地域・権限を確認してください")
            self.catalog_label.configure(text=f"対応モデル: {len(result.choices)}件。利用不可: {result.excluded}件を除外。" + " ".join(result.warnings) + " 選択後に保存して接続テストを行ってください。", fg=MUTED)
            self.status.set("モデル一覧を取得しました。利用するモデルを選んでください。" if result.choices else "対応モデルが見つかりませんでした。地域・利用条件を確認するか、IDを手入力してください。")
        self.set_busy()
        if not error and key == self.catalog_key():
            def reveal():
                if self.app.closed: return
                scroll = self.frames[1]
                y = self.model_bar.winfo_rooty() - scroll.inner.winfo_rooty()
                scroll.canvas.yview_moveto(max(0, (y - 20) / max(1, scroll.inner.winfo_height())))
            self.app.root.after_idle(reveal)

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
        if self.delete_credentials: cfg.bedrock.credential_ref = ""
        choice = next((c for c in self.catalog_choices if c.model == cfg.bedrock.model), None)
        # Use AWS's streaming capability signal without changing the saved base
        # settings during discovery. Manual IDs keep the current preference.
        if choice is not None: cfg.bedrock.streaming = choice.streaming
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
            keys = self.collect_credentials(allow_cleanup=True) if not self.delete_credentials else None
            if test:
                if candidate.bedrock.auth == "access_keys" and keys is None and not candidate.bedrock.credential_ref:
                    raise SetupError("access_key_id", "Access Key IDとSecret Access Keyを入力してください。")
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
        self.busy = True; self.status_label.configure(fg=ACCENT)
        self.status.set("設定を保存しています…" if not test else "設定を保存し、テスト用の入力でAI接続を確認しています…")
        self.set_busy()
        clear_credentials = self.delete_credentials
        def work():
            saved, cleanup_ok = False, True
            try:
                cleanup_ok = self.rt.configure(candidate, credentials=keys, clear_credentials=clear_credentials)
                candidate_saved = copy.deepcopy(self.rt.cfg); saved = True
                if test:
                    with self.rt.operation():
                        probe_connection(candidate_saved, self.rt.store)
                    self.rt.store.set_state("ui.connection_probe", {"fingerprint": connection_fingerprint(candidate_saved), "status": "success", "at": stamp()})
                self.events.put((candidate_saved, saved, test, None, cleanup_ok))
            except Exception as error:
                code = safe_code(error)
                if saved and test:
                    try:
                        self.rt.store.set_state("ui.connection_probe", {"fingerprint": connection_fingerprint(candidate_saved), "status": "failed", "code": code, "at": stamp()})
                    except Exception:
                        pass  # Still return the failure so the UI always releases its busy state.
                self.events.put((candidate_saved if saved else candidate, saved, test, (code, error.message if isinstance(error, SetupError) else "", getattr(error, "field", None)), cleanup_ok))
        threading.Thread(target=work, daemon=True, name="assistant-setup").start()

    def set_busy(self):
        for widget in self.inputs + self.buttons + [self.save_button, self.test_button]:
            state = "disabled" if self.busy else "readonly" if isinstance(widget, ttk.Combobox) else "normal"
            widget.configure(state=state)
        self.model_combo.configure(state="readonly" if self.catalog_choices and not self.busy else "disabled")
        self.refresh_key_status()
        self.chat_button.configure(state="normal" if not self.busy and not self.dirty and self.app.ready() else "disabled")

    def pump(self):
        self.pump_catalog()
        try: candidate, saved, test, error, cleanup_ok = self.events.get_nowait()
        except queue.Empty: return
        self.busy = False
        if saved:
            self.cleanup_pending = cleanup_ok is False
            sensitive_changed = candidate.bedrock.credential_ref != self.base.bedrock.credential_ref or self.delete_credentials or any(v.get() for v in self.secret_vars.values())
            self.base = candidate; self.dirty = False; self.delete_credentials = False
            self.clear_secret_inputs()
            if sensitive_changed: self.secret_revision += 1; self.invalidate_catalog()
        if saved and test:
            self.probe_override = {"fingerprint": connection_fingerprint(candidate), "status": "failed" if error else "success", "at": stamp()}
        self.set_busy(); self.refresh()
        if error:
            code, message, field = error
            title, help_text = error_help(code)
            if code == "SETTINGS_BUSY": help_text = "回答・資料の取り込み・継続タスクの処理が終わってから、もう一度保存してください。入力内容は保持しています。"
            elif not saved and not message and code not in ("SETTINGS_CHANGED", "CREDENTIAL_PATH_INVALID", "CREDENTIAL_STORAGE_UNSUPPORTED", "CREDENTIAL_STORE_FAILED", "AWS_ACCESS_KEY_ID_INVALID", "AWS_SECRET_ACCESS_KEY_INVALID", "AWS_SESSION_TOKEN_REQUIRED", "AWS_SESSION_TOKEN_INVALID"):
                help_text = "設定を保存できませんでした。保存先の空き容量と書き込み権限を確認して再試行してください。入力内容と元の設定は保持しています。"
            self.show_error(message or (title + "。" + help_text), field)
        elif test:
            self.tabs.select(2); self.status.set("接続テストに成功しました。「質問画面へ」から質問できます。")
            self.status_label.configure(fg=ACCENT)
        else:
            suffix = "「質問画面へ」から質問できます。" if self.app.ready() else "次に「AI接続」で必要な項目を設定してください。"
            self.status.set("設定を保存して反映しました。" + suffix); self.status_label.configure(fg=ACCENT)
        if cleanup_ok is False:
            self.status.set(self.status.get() + " 古い暗号化キーの削除に失敗しました。保存先の権限を確認し、再度保存してください。")
            self.status_label.configure(fg=ERROR)
        self.app.diagnostic_report = None
        self.app.info_view.pack_forget()
        self.app.refresh(schedule=False)
        if not self.app.message_rows and not self.app.loading: self.app.render_messages()

    def refresh(self):
        cfg = self.rt.cfg
        self.summary.configure(text="ローカル保存: " + ("許可済み" if cfg.local_data_approved else "未設定") +
            f"\n資料フォルダ: {len(cfg.roots)}件（資料なしでも質問できます）\nAI接続の設定: " + ("設定済み" if self.app.ready() else "未完了") +
            f"\n保存済みのモデル: {cfg.bedrock.model}\n保存済みのAWS地域: {cfg.bedrock.region}")
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
