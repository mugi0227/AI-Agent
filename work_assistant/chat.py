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
    "BEDROCK_ACCESS_DENIED": ("Bedrockに接続できませんでした", "AWSプロファイル・SSOの有効期限、モデルの利用権限を確認してから、もう一度お試しください。"),
    "BEDROCK_NETWORK_OR_TLS": ("接続を確認してください", "ネットワーク、会社のプロキシ、証明書の設定を確認してから、もう一度お試しください。"),
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
