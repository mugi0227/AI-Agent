# OpenClawの「気が利く」をどう取り入れたか

参照日: 2026-10-16。参照ソースのコミットは `72d41b6f9d0bd2290a959f062b134f60664063d7`。OpenClawのコードをコピーしたり、OpenClawを依存関係にしたりしていない。以下は読んだ実装から得た設計上の判断を、本プロジェクトで別実装したもの。

## 1. 魔法のプロンプトではなく、実行条件の制御

`src/infra/heartbeat-runner-run.ts`は、wakeの判定、準備、送信先の準備状態、キャンセル、共有セッションの状態を確認してから実行する。保留イベントを早すぎる段階で消費しない配慮もある。`heartbeat-runner-scheduler.ts`ではcooldownと短時間の連続起動を抑制する。

本実装では`Scheduler`が候補を作り、活動時間・曜日・集中モード・前回実行時刻を確認する。候補がなければモデルを呼ばない。`Runtime`はユーザーの対話開始時に定期確認のキャンセルを要求し、次の安全な境界で譲る。SQLiteのjob状態と通知fingerprintで、再起動後の二重処理・通知連打を抑制する。

違い: OpenClawのCron/複数セッション/チャネル配信機構を再現したわけではない。初版は単一利用者、ローカル通知、GUIまたはCLIの単一プロセス運用。

## 2. 「何も言わない」を正常な成果にする

Heartbeatのドキュメントは、通知を出す応答と静かに終了する応答を区別し、活動時間や既定の配信抑制を扱う。常に何かを生成させる設計では、不要通知が増える。

本実装は監視用にだけ`notify`ツールを渡す。原文一致の引用、confidence、時間帯、集中モード、日次回数、cooldown、重複キーをホストが検証する。通常対話からnotifyを勝手に呼べない。モデルの最終回答がNO_REPLYなら通知を作らない。通知はアプリ内タブで確認し、OSトーストは未実装。

## 3. 長い会話と長期記憶を分ける

`src/auto-reply/reply/memory-flush.ts`は、文脈量を予測し、compaction前のmemory flushをサイクル単位で重複防止する。単に全部の会話を毎回投入する方式ではない。

本実装では原本文書、会話履歴、ユーザー定義の案件、承認済みの決定/記憶/タスクを別テーブルにする。直近会話は有界で投入し、過去の会話は専用検索ツールで参照する。長期記憶は原文を引用した候補として登録し、人が承認してから使用する。

違い: **OpenClawのLLM memory flush/自動要約compactionそのものは未実装**。会話を消して要約だけに置き換えるのではなく、文脈は有界、履歴は検索可能にする方式を採用した。原本失効時は派生キャッシュも保守的に失効させる。

## 4. イベントは実行命令ではない

`src/infra/system-events.ts`は、明示されたセッションキー・所有者・重複・イベント上限を扱う。外部イベントの文字列を無条件にグローバル指示へ流し込む構成ではない。

本実装は同期フォルダ/ブラウザからの情報を非信頼データとして読み、イベントID・payload hash・source更新時刻・observed時刻を保持する。外部JSONでローカルファイルの検証済みメタデータを偽装できない。AIには任意シェル、任意URL取得、メール送信、承認、設定変更を公開しない。

違い: OpenClawの対象ソースでは一部イベントキューを意図的に揮発性としている。本実装では、PC停止・同期の遅延・重複配送の都合から、配送receiptとjobをSQLiteに永続化する。これは同一実装の移植ではなく、環境に合わせた判断。

## 5. 古い機能紹介をそのまま再現しない

確認した現行ドキュメントでは、旧HEARTBEAT.md運用は廃止され、monitor scratchへ移行している。また、会話から暗黙の約束を自動推定する実験は廃止と記載されている。

そこで、古い会話の一文から予定・義務を勝手に復活させる機構は採用しない。候補、承認済みタスク、完了済みタスクを区別し、期限が曖昧な場合はnullにする。スケジュールは設定と承認済みデータが根拠。

## 対応表

| 狙い | 本実装 | 自動試験 |
|---|---|---|
| 対話を邪魔しない | runtime.py / agent.py cancellation | キャンセル・監視スキップ |
| 空振りでモデルを呼ばない | scheduler.py candidates | no-change/no-inference |
| 同じ通知を繰り返さない | store.notify / persistent jobs | 重複・cooldown・再生成claim |
| 根拠のある記憶 | tools.propose_action / UI承認 | 偽引用拒否・承認前タスクなし |
| 完了事項を復活させない | open_tasksと記憶を分離 | completed_task_not_revived |
| 期限切れ情報を使わない | source leases / tombstones | 失効・順不同・再配送 |
| 予算と能力を守る | typed tools / daily budget | 未知tool・引数・予算・外部API拒否 |

## 一次資料

- https://github.com/openclaw/openclaw/blob/72d41b6f9d0bd2290a959f062b134f60664063d7/src/infra/heartbeat-runner-run.ts
- https://github.com/openclaw/openclaw/blob/72d41b6f9d0bd2290a959f062b134f60664063d7/src/infra/heartbeat-runner-scheduler.ts
- https://github.com/openclaw/openclaw/blob/72d41b6f9d0bd2290a959f062b134f60664063d7/src/auto-reply/reply/memory-flush.ts
- https://github.com/openclaw/openclaw/blob/72d41b6f9d0bd2290a959f062b134f60664063d7/src/infra/system-events.ts
- https://docs.openclaw.ai/gateway/heartbeat
- https://docs.openclaw.ai/automation
- https://docs.openclaw.ai/concepts/memory
