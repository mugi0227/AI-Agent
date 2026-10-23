"""Conversation jobs and presentation text, independent of the Tk event loop."""
from __future__ import annotations

import queue
import re
import threading
import uuid
from dataclasses import dataclass, field

from .common import AssistantError, safe_code

TOOL_LABELS = {
    "search_documents": "資料を検索しています",
    "read_document": "原文を確認しています",
    "get_project_context": "案件とタスクを確認しています",
    "search_conversation": "過去の会話を確認しています",
    "propose_action": "承認待ちの候補を整理しています",
    "request_information": "追加の情報を依頼しています",
    "get_request_status": "情報の取得状況を確認しています",
}

ERROR_HELP = {
    "SETTINGS_BUSY": ("設定を変更できるまでお待ちください", "回答・資料の取り込み・継続タスクの処理が終わってから、もう一度保存してください。"),
    "PATH_NOT_ALLOWED": ("許可したフォルダ外の資料です", "「接続・設定」の「保存と資料」で、利用するフォルダを追加して保存してください。"),
    "SENSITIVE_PATH_BLOCKED": ("認証情報などのファイルは取り込めません", "通常の資料ファイルを選んでください。"),
    "STATE_DIRECTORY_BLOCKED": ("アプリの保存先は取り込めません", "資料フォルダ内のファイルを選んでください。"),
    "FILE_TYPE_BLOCKED": ("このファイル形式は取り込めません", "TXT・Markdown・Office文書など、対応する形式の資料を選んでください。"),
    "FILE_TOO_LARGE": ("ファイルが大きすぎます", "資料を分割するか、必要な部分を対応形式で保存して取り込んでください。"),
    "PDF_DEPENDENCY_MISSING": ("PDF用ライブラリが必要です", "社内の配布手順でPDF用の追加依存を導入するか、テキスト形式で取り込んでください。"),
    "PROBE_TOOL_NOT_CALLED": ("AIのツール呼び出しを確認できませんでした", "モデルが使用するAPIのツール呼び出しに対応するか確認し、再度テストしてください。"),
    "MODEL_CATALOG_INVALID": ("モデル一覧の応答を確認できませんでした", "地域と接続設定を確認し、一覧を再取得してください。"),
    "MODEL_CATALOG_TIMEOUT": ("モデル一覧の取得がタイムアウトしました", "接続状況を確認し、もう一度取得してください。"),
    "MODEL_STREAM_INVALID": ("AIの応答を確認できませんでした", "未完了の応答は確定していません。接続とモデルの対応状況を確認し、再試行してください。"),
    "PROBE_TOOL_RESULT_NOT_USED": ("ツール結果を使った応答を確認できませんでした", "モデルIDとAPIの対応状況を確認し、再度テストしてください。"),
    "MEMORY_CAPACITY": ("保存できる記憶の上限です", "不要な記憶を削除してから追加してください。保存件数の上限は設定ファイルで確認できます。"),
    "RETRY_NOT_AVAILABLE": ("このタスクは再実行できません", "子タスクは親の依頼から確認してください。資料の失効や処理上限の場合は、資料と依頼内容を確認して新しく登録してください。"),
    "SOURCE_SCOPE_DENIED": ("この役割では資料を利用できません", "役割に許可された資料を選ぶか、役割の設定を確認してください。"),
    "SESSION_BUSY": ("この会話は別のプロセスで処理中です", "処理の完了を待ってから再実行してください。異常終了した場合は実行権の期限後に再開できます。"),
    "RUNTIME_BUSY": ("同時実行数の上限です", "現在の処理の完了を待ってから再実行してください。"),
    "STALE_RUN": ("実行状態が更新されました", "古い実行からの書き込みを停止しました。現在の状態を確認してください。"),
    "POLICY_CHANGED": ("役割の権限設定が変更されました", "変更後の設定を確認してから再実行してください。"),
    "MODEL_EGRESS_NOT_APPROVED": ("AIへの接続設定が必要です", "「接続・設定」でBedrockの有効化と送信設定を確認してください。入力した内容は下書きに戻せます。"),
    "LOCAL_DATA_NOT_APPROVED": ("ローカルデータの利用設定が必要です", "「接続・設定」の「保存と資料」で、ローカル保存の許可と資料フォルダを設定してください。"),
    "BEDROCK_ACCESS_DENIED": ("Bedrockに接続できませんでした", "AWSプロファイル・SSO、Access Key・Session Tokenの有効期限、モデルの利用権限を確認してから、もう一度お試しください。"),
    "AWS_ACCESS_KEY_ID_INVALID": ("Access Key IDを確認してください", "Access Key IDを半角英大文字・数字で入力してください。Secret Access Keyとは別の値です。"),
    "AWS_SECRET_ACCESS_KEY_LENGTH": ("Secret Access Keyの文字数を確認してください", "Secret Access Keyは半角40文字です。入力欄の文字数を確認し、発行元から値だけをコピーしてください。前後の空白・改行は除いて使用します。"),
    "AWS_SECRET_ACCESS_KEY_INVALID": ("Secret Access Keyの文字種を確認してください", "40文字でも、全角文字・途中の空白・不可視文字・引用符があると使用できません。半角英数字と / + = の値だけを発行元からコピーしてください。"),
    "AWS_SESSION_TOKEN_REQUIRED": ("Session Tokenが必要です", "ASIAで始まる一時認証のAccess Key IDでは、同時に発行されたSession Tokenも入力してください。"),
    "AWS_SESSION_TOKEN_INVALID": ("Session Tokenを確認してください", "発行されたSession Tokenを空白・改行なしで入力してください。"),
    "AWS_ACCESS_KEYS_MISSING": ("保存済みキーが見つかりません", "「AI接続」でAccess Key ID・Secret Access Keyを入力して保存してください。一時認証ではSession Tokenも必要です。"),
    "CREDENTIAL_STORAGE_UNSUPPORTED": ("この環境ではキーを暗号化保存できません", "キーの画面入力と保存はWindowsで使用してください。この環境ではAWSプロファイル・環境変数の認証が利用できます。"),
    "CREDENTIAL_STORE_FAILED": ("キーを保存できませんでした", "Windowsの暗号化機能とアプリ保存先の空き容量・アクセス権を確認し、再度保存してください。元の設定と入力内容は保持しています。"),
    "CREDENTIAL_DECRYPT_FAILED": ("保存済みキーを読み取れませんでした", "保存したWindowsユーザーで起動するか、Access Key ID・Secret Access Keyを再入力して保存してください。"),
    "CREDENTIAL_PATH_INVALID": ("キーの保存先を確認してください", "アプリの保存先を実体のあるローカルフォルダにしてください。"),
    "SETTINGS_CHANGED": ("別のアプリでキー設定が変更されました", "このアプリを起動し直して最新の設定を読み込み、再度設定してください。"),
    "BEDROCK_NETWORK_OR_TLS": ("接続を確認してください", "ネットワーク、会社のプロキシ、証明書の設定を確認してから、もう一度お試しください。"),
    "BEDROCK_PROXY_INVALID": ("プロキシURLを確認してください", "「AI接続」の「接続先・プロキシ・証明書・利用上限」で、http://proxy.example:8080 の形式で指定してください。ID・パスワードやPACのURLは入力できません。"),
    "BEDROCK_PROXY_AUTH_REQUIRED": ("社内プロキシの認証が必要です（407）", "AWSキーのエラーではありません。社内ITにPythonアプリから利用できる認証済みプロキシ接続方法を確認してください。ブラウザの統合認証は自動で引き継ぎません。"),
    "BEDROCK_PROXY_BLOCKED": ("社内プロキシが接続を拒否しました", "社内ITにBedrockの接続先へのHTTPS通信許可を確認してください。モデル一覧は bedrock.<地域>.amazonaws.com、推論は bedrock-runtime.<地域>.amazonaws.com または設定したMantleホストを使用します。"),
    "BEDROCK_CA_BUNDLE_INVALID": ("CA証明書ファイルを読み込めません", "「AI接続」の詳細設定で、会社指定のPEM形式のCA証明書ファイルとアクセス権を確認してください。"),
    "BEDROCK_TLS_CERTIFICATE": ("接続先の証明書を検証できません", "社内プロキシがTLS通信を検査する場合は、会社指定のCA証明書を「AI接続」の詳細設定で指定してください。PCの日時も確認してください。"),
    "BEDROCK_TLS_ERROR": ("TLS接続に失敗しました", "社内プロキシの通信方式と会社のCA証明書を確認してください。TLS証明書の検証は有効のまま使用します。"),
    "BEDROCK_DNS_FAILED": ("接続先の名前を解決できません", "社内ネットワーク・VPNの接続状態と、プロキシのサーバー名を確認してください。"),
    "BEDROCK_TIMEOUT": ("接続がタイムアウトしました", "社内プロキシ・ファイアウォールと、設定したAWS地域への通信許可を確認してください。"),
    "BEDROCK_API_OR_MODEL_MISMATCH": ("モデルの設定を確認してください", "BedrockのモデルID・地域・エンドポイントの組み合わせを確認してください。"),
    "CROSS_REGION_NOT_APPROVED": ("推論する地域の設定が必要です", "「接続・設定」でモデルの処理地域の設定を確認してください。"),
    "DAILY_BUDGET_EXCEEDED": ("今日の利用上限に達しました", "資料のローカル検索は引き続き使えます。AIへの依頼は利用上限が更新されてからお試しください。"),
    "CONTEXT_LIMIT": ("一度に扱う内容が多すぎます", "添える資料を減らすか、新しい会話で対象を絞って質問してください。"),
    "SOURCE_UNAVAILABLE": ("選んだ資料を利用できません", "資料が更新・失効している可能性があります。資料を選び直してください。"),
    "SOURCE_EXPIRED": ("資料の確認期限が切れています", "資料を再取得してから、選び直してください。"),
    "SOURCE_CHANGED_DURING_TURN": ("回答中に資料が更新されました", "最新の資料を確認してから、もう一度お試しください。未確定の応答は保存していません。"),
    "CITATION_NOT_VERIFIED": ("回答の出典を確認できませんでした", "未確認の回答は保存していません。質問する資料を指定して、もう一度お試しください。"),
    "TURN_PREEMPTED": ("応答を停止しました", "生成途中の応答は保存していません。質問を編集するか、もう一度実行できます。"),
    "TURN_INTERRUPTED": ("前回の処理は完了しませんでした", "アプリの終了などで処理が中断されました。質問を確認して、もう一度実行できます。"),
    "FOREGROUND_BUSY": ("別の依頼を処理しています", "処理の完了を待つか、「停止」を押してください。"),
    "PROMPT_INVALID": ("入力を確認してください", "質問は1〜10,000文字で入力してください。"),
    "AWS_CREDENTIALS_UNAVAILABLE": ("AWSの認証を確認してください", "AWSプロファイル・SSOログイン・有効期限を確認してから、もう一度お試しください。"),
    "AWS_CREDENTIALS_MISSING": ("AWSの認証設定が見つかりません", "「接続・設定」でAWSプロファイルとSSOの設定を確認してください。"),
    "SIGV4_DEPENDENCY_MISSING": ("AWS接続用のライブラリが必要です", "社内の配布手順に従い、sigv4用の追加依存をインストールしてください。"),
    "BEDROCK_KEY_MISSING": ("Bedrockの認証キーを確認してください", "設定した環境変数へBedrock用のキーが登録されているか確認してください。"),
    "MODEL_STREAM_TRUNCATED": ("応答の途中で接続が切れました", "未確定の応答は保存していません。接続を確認してから、もう一度お試しください。"),
}


def error_help(code: str) -> tuple[str, str]:
    return ERROR_HELP.get(code, ("依頼を完了できませんでした", "質問を下書きに戻して再実行できます。繰り返す場合は「接続・設定」の診断と検証手順を確認してください。"))


def progress_label(value: str) -> str:
    if value.startswith("情報を確認中: "):
        return TOOL_LABELS.get(value.split(": ", 1)[1], "情報を確認しています")
    return "回答を考えています" if "検討中" in value or "検証中" in value else value


def display_answer(text: str, citations: list[dict]) -> str:
    numbers = {source["id"]: index for index, source in enumerate(citations, 1)}
    return re.sub(r"\[資料:([^\]]+)\]", lambda m: f"［{numbers[m[1]]}］" if m[1] in numbers else m[0], text)


def copy_answer(text: str, citations: list[dict]) -> str:
    answer = display_answer(text, citations)
    if citations:
        answer += "\n\n参照資料\n" + "\n".join(f'［{i}］{s["title"]}\n{s["source_url"]}' for i, s in enumerate(citations, 1))
    return answer


@dataclass
class ChatEvent:
    kind: str
    session: str
    request_id: str
    payload: object


@dataclass
class Submission:
    session: str
    prompt: str
    document_ids: list[str]
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancel: threading.Event = field(default_factory=threading.Event)
    text: str = ""
    progress: str = "依頼を受け付けました"
    steps: list[str] = field(default_factory=list)


class ChatController:
    """Workers never call Tk. Tokens keep late events attached to the originating chat."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.events: queue.Queue[ChatEvent] = queue.Queue()
        self.active: Submission | None = None

    def send(self, session: str, prompt: str, document_ids: list[str], retry_message_id: int | None = None) -> Submission:
        if self.active:
            raise AssistantError("FOREGROUND_BUSY")
        if not prompt.strip() or len(prompt) > 10000:
            raise AssistantError("PROMPT_INVALID")
        job = Submission(session, prompt, list(document_ids))
        self.active = job

        def emit(kind, payload):
            self.events.put(ChatEvent(kind, session, job.request_id, payload))

        def run():
            try:
                result = self.runtime.ask(prompt, session=session, document_ids=job.document_ids,
                                          retry_message_id=retry_message_id, cancel=job.cancel,
                                          progress=lambda value: emit("progress", progress_label(value)),
                                          on_delta=lambda value: emit("delta", value))
                emit("answer", result)
            except Exception as error:
                emit("error", safe_code(error))

        threading.Thread(target=run, daemon=True, name="assistant-chat").start()
        return job

    def stop(self) -> None:
        if self.active:
            self.active.cancel.set()
            self.active.progress = "停止しています…"

    def drain(self) -> list[ChatEvent]:
        pending = []
        for _ in range(1000):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            job = self.active
            if not job or event.request_id != job.request_id:
                continue
            if event.kind == "delta":
                job.text = (job.text + str(event.payload))[:30000]
            elif event.kind == "progress" and not job.cancel.is_set():
                job.progress = str(event.payload)
                if not job.steps or job.steps[-1] != job.progress:
                    job.steps.append(job.progress)
            elif event.kind in ("answer", "error"):
                self.active = None
            pending.append(event)
        return pending
