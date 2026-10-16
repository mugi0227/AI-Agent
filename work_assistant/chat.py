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
    "MODEL_EGRESS_NOT_APPROVED": ("AIへの接続設定が必要です", "「接続・設定」でBedrockの有効化と送信設定を確認してください。入力した内容は下書きに戻せます。"),
    "LOCAL_DATA_NOT_APPROVED": ("ローカルデータの利用設定が必要です", "「接続・設定」から設定ファイルを開き、利用できる資料と保存の設定を確認してください。"),
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
