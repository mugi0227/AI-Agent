# AI-Agent 詳細設計 v1

設計日: 2026-10-16。実装前に確定するベースライン。会社PCでの実証前であり、本番運用承認を意味しない。

## 1. 目的・制約・成功条件

目的は「会社PCで、仕事の文脈を理解し、必要な情報を調べ、適切なタイミングで支援する個人用秘書」。単なるチャットUIや全文検索だけでなく、案件、根拠、持ち帰り、情報の鮮度、通知の必要性を扱う。

確定条件:
- Windows 10/11 ネイティブ。Docker/WSL・管理者常駐サービスを必須にしない。
- OpenClawは導入せず、設計上の考え方を参考に独自実装する。
- 推論は承認されたAmazon Bedrockのみ。OpenAIのResponses API形式を使う。OpenAI本家へのフォールバックは禁止。
- 独自Graphアプリの権限付与とPower Automate Premiumを前提にしない。既存の標準コネクタの利用可否は会社で検証する。
- Box/OneDrive/同期可能なSharePoint文書は、明示したローカルフォルダを読む。
- 同期経由は非同期収集。対話はローカル検索とEdgeからの直接キャプチャを優先する。
- 自分以外が主催するTeams会議の文字起こしは、取得権限と通知範囲を別々に検証する。閲覧権限から保存権限を推定しない。
- 公開リポジトリにはコードと架空の検証データのみ。業務データ、接続情報、設定、DB、認証情報、実行ログをコミットしない。

受け入れの核:
1. 通信なしでも起動・取り込み・日本語検索・出典表示ができる。
2. Bedrockのツール呼び出しを実行し、その結果とreasoningを含む出力項目を正しいResponses形式で返せる。
3. 会議資料や文字起こしから、根拠のある行動候補を提示できる。未承認の候補を確定タスクにしない。
4. 情報がない/古い/取得できない状態を隠さない。
5. 静穏時間、対話優先、重複抑制、予算上限をAIの判断ではなくホストが強制する。
6. 会社PCのCodexに、検証項目・証拠・失敗時分岐を引き継げる。

## 2. 構成と技術判断

Python 3.11以上を本体に採用する。既知の会社環境に合わせ、コアを標準ライブラリ中心にしてインストール依存を減らす。C#/.NETの追加ランタイムは初版の必須条件にしない。UIはTkinter、永続化はSQLite。ブラウザ拡張はManifest V3のJavaScript。署名認証には必要時のみboto3/botocoreを追加する。

```text
標準Power Automate ─→ 承認済Box/OneDriveの専用受渡フォルダ
                            ↓ 低速・非同期
ローカル資料 ─→ 収集/正規化 ─→ SQLite原文/索引/案件/候補/通知
                            ↑                 ↕
Edge activeTab ─→ Native Messaging ─→ ローカル受信箱   Agent Runtime
                                              ↕
                                    Bedrock Responses API
                                              ↕
                                    WindowsデスクトップUI
```

外部向けHTTPサーバーを置かない。Edge連携もlocalhostの無認証APIではなくNative Messagingを使う。PC停止中はクラウド配送だけが進み、PC復帰後に処理を再開する。24時間常時応答は約束しない。

### 信頼境界
- 設定・ポリシー・ユーザー承認: ホストが管理する権限。
- メール/資料/会議記録/ブラウザページ/モデル出力: すべて非信頼データ。
- 原文に「設定を変更せよ」「外部へ送信せよ」とあっても実行権限にならない。
- モデルには任意HTTP、任意ファイルパス、シェル、任意コード実行、プラグイン導入、承認操作を渡さない。
- 同一OSアカウントの悪意あるプロセスまで防御するサンドボックスではない。OS権限、BitLocker、EDR、管理者審査は別途必要。

## 3. OpenClawからの取り込み方

参照コミット: `72d41b6f9d0bd2290a959f062b134f60664063d7`。

| 観察した仕組み | 参照ソース | 本実装の判断 |
|---|---|---|
| periodic turn、busy skip、取り残したイベントの保持 | src/infra/heartbeat-runner-run.ts | 通常対話優先。定期確認は変化があるときだけ。保留イベントを成功扱いで消さない |
| cooldownと連続起動の抑制 | src/infra/heartbeat-runner-scheduler.ts | SQLiteで通知ID/時間を保持し再起動後も重複抑制 |
| セッション単位のイベントと所有者 | src/infra/system-events.ts | スコープを明示する。本実装はPC再起動に備えて配送と処理状態を永続化する |
| compaction直前のmemory flush、サイクルごとの重複防止 | src/auto-reply/reply/memory-flush.ts | 会話を無限に投入しない。原本・承認済記憶・直近対話を分離。削除ではなく検索可能な履歴を残す |
| activeHours、quiet response、構造化heartbeat_respond | docs/gateway/heartbeat | 通知不要を正常結果として扱う。引用・重要度・回数をホストが検証 |
| inferred commitmentsの廃止、明示スケジュールへの移行 | docs/automation | 古い会話から約束を勝手に復活させない。行動候補は承認されるまで予定にしない |

現行OpenClawではHEARTBEAT.mdが廃止されmonitor scratchへ移行している。古い紹介記事をそのまま仕様にしない。巨大なマルチチャネル/プラグイン/シェル実行機構は移植しない。ソースコードのコピーはしない。

## 4. データ設計

SQLiteを同期フォルダ外に配置する。WAL、foreign_keys、busy_timeout、短いトランザクションを利用。

- documents: 安定ID、source_key、kind、title、body、URL/参照パス、source_modified、observed_at、expires_at、hash、metadata、状態。
- chunks + 全文索引: 文書ID、順番、本文。日本語は正規化した二文字単位の索引と語彙検索を組み合わせる。意味検索や外部埋め込みを必須にしない。
- events: 配送event_id、payload hash、受信時刻、処理結果。再配送は冪等。異なる内容で同じevent_idは異常扱い。
- projects: ユーザー定義の案件、目的、キーワード。
- proposals: 行動/決定/記憶の候補、source_id、根拠の原文、confidence、期限候補、pending/approved/rejected。
- tasks: 承認済み候補のみ。モデルからの直接確定は禁止。
- conversations: ユーザー発言と最終回答。reasoning内部状態をUIや監査ログに表示しない。
- notifications: 重複キー、根拠、生成時刻、既読/保留。
- bridge_requests: request_id、action、引数、期限、pending/completed/failed/expired。
- state/jobs/budget/audit: カーソル、実行状態、日次上限、本文を含まない監査メタデータ。

### 鮮度・権限変更・削除

クラウド資料のローカルコピーは現在の権限をリアルタイム証明しない。有限のキャッシュ有効期限を必須とする。期限切れ・削除通知・対象ルートへのアクセス喪失では検索/モデル提供を停止する。原本が利用不可になった際、派生記憶/候補/会話キャッシュにも失効を反映する。実運用では定期再確認/削除通知フローを合わせて用意する。

引用には文書ID、更新/取得時刻、取得範囲を含める。検索ゼロ件を「存在しない」と断定しない。OCR、暗号化文書、埋め込み画像、複雑なExcel表示、過去Teamsの完全取得は対応済みと扱わない。

## 5. 取り込みと受渡プロトコル

### ローカル資料

読み取りルートのallowlist、拡張子のallowlist、サイズ/文字数上限、秘密情報ファイル除外を適用。シンボリックリンク/ジャンクションを辿って許可外へ出ない。クラウドplaceholderは既定で自動大量ダウンロードしない。書き込み中のファイルは安定確認後に読む。

TXT/MD/CSV/HTML/EML/VTT、DOCX/PPTX/XLSXのテキストを扱う。Office ZIPには展開サイズ/要素数上限とXML制約を設け、マクロも外部参照も実行しない。PDFは承認された追加ライブラリがある場合のみテキスト抽出し、画像PDFは未対応として報告する。

### Power Automate → PC

1イベント1JSON。schema_version、event_id、kind、source_id、title、content、source_url、source_modified_at、observed_at、expires_at、metadataを持つ。時刻はRFC3339のタイムゾーン付き。kindはmail/teams_message/transcript/calendar/document/search_result/tombstone等に限定する。JSONスキーマとサンプルを提供する。

ファイルシステム通知の一回性を信頼しない。原子的書き込み、安定確認、再走査、イベントID/hashによる重複排除、失敗状態を実装する。元の同期ファイルは勝手に削除しない。エラー文に本文/アクセストークンを含めない。

### PC → Power Automate

対話中は待ち続けずpendingを返す。request_id、action、検証済引数、expires_atを専用outboxに配置。actionはsearch_mail/get_calendar/get_transcript/search_sharepointのみ。返答は既存request_idと期限に照合。未知ID・期限切れ・別actionの返答は採用しない。受け渡しフォルダに権限変更や任意実行コマンドは載せない。

## 6. Edge即時連携

activeTab/scripting/nativeMessagingを使い、明示操作時のみ選択文または現在の表示テキストをプレビューして渡す。デフォルトは選択文。Cookie、LocalStorage、認証トークン、入力欄、パスワードを収集しない。許可ホスト一覧を拡張とホスト双方で照合する。URLのquery/fragmentは既定で落とす。

Native Messagingは長さprefix付きJSON、上限付きread-exact、UTF-8、操作capture/pingのみ。ホストmanifestのallowed_originsは具体的な拡張IDだけ。Windows HKCU登録スクリプトと解除手順を提供する。拡張/ホストの設置は管理ポリシーに従う。禁止なら手動コピー/許可済ダウンロードに切り替える。

## 7. Bedrock接続とエージェントループ

既定候補: bedrock-runtime / ap-northeast-1 / global.openai.gpt-6-luna。ただし推論は初期状態OFF。globalによる国外処理の明示承認がなければ実行しない。Mantleの場合はGPT-6 Luna固有の `/openai/v1` と `openai.gpt-6-luna`、対応リージョンを明示設定する。失敗時にモデル/地域を勝手に変更しない。

API仕様:
- POST `{base_url}/responses`、OpenAI Responses互換JSON。
- `store:false` を毎回明示。サーバー保持に依存しない。企業の別設定による監査ログ等まで無効にする主張はしない。
- `previous_response_id`は使わずローカルで必要な文脈を構成。
- client-side function toolsのみ。server-side web/file searchやremote MCPは使わない。
- function_callのcall_idとfunction_call_outputを対応させる。response.output全体をreasoning項目込みで次のツール応答へ引き継ぐ。
- reasoning encrypted_content等の互換性は診断で確認し、未対応なら証拠を残して明示的に構成変更する。
- 失敗/未完了/拒否/空応答を成功扱いしない。JSON引数・ツール名・引数範囲・原文引用をホストで検証。
- ツール回数、モデル往復、実行時間、入力文字数、日次モデル呼び出し/トークン予算に上限。

認証: Bedrock APIキーは環境変数から読み込む。AWS SigV4は承認済AWSプロファイル/SSOの認証チェーンを利用。秘密を設定/ログ/リポジトリに保存しない。TLS検証は無効化しない。社内CAとproxyは明示的な設定に従う。

ツール: search_documents、read_document、get_project_context、search_conversation、propose_action、request_information、notification候補。承認/設定変更/外部送信/削除/シェルはツールにしない。

## 8. 気の利く振る舞い

- 朝夕の確認: 今日の予定、承認済期限、未読の重要変更を短く整理。
- 会議前: 次の会議と関連文書を検索し、前回の決定/未確認事項/資料の鮮度を提示。
- 会議後: 新しい文字起こしから原文付きの持ち帰り候補を作る。責任者/期限が曖昧なら曖昧のまま表示。
- 案件記憶: ユーザーが定義した目的と承認済メモを共有し、他案件に混ぜない。
- 通知制御: 営業時間、曜日、手動集中モード、上限、cooldown、既読/保留、source+typeの重複キー。
- 変化も期限もない場合はモデルを呼ばない。NO_REPLYは正常。
- AIが提案した約束をAI自身が承認したことにしない。
- foreground対話はbackgroundより優先。PC復帰時は古い定期実行を大量再生せず現在の状態へ収束。

## 9. Power Automate構築方針

標準コネクタのフロー仕様・式・入出力JSON・エラーハンドリング・会社Codex依頼文を提供する。接続参照、dynamic schema、環境ID、主催者権限はここでは取得できないため、import可能な完成パッケージと偽らない。

対象フロー:
- 新着メール/送信済みの補完 → envelope。
- 予定の期間限定同期 → calendar。
- 対象Teamsチャット/チャネル → message。返信/編集/削除と過去分は別テスト。
- 手動会議ID → ListMeetingTranscripts → GetMeetingTranscriptContent。
- TranscriptTrigger → 本文取得（他人主催通知の範囲は別検証）。
- outbox依頼 → Outlook検索またはSharePoint REST search → response。
- 定期的なアクセス/削除確認。

BoxがSSO/DLPで不可ならOneDriveへ配送先のみ切替。HTTP Premiumや独自Graph権限の取得を暗黙の前提に追加しない。SharePoint専用HTTPは汎用HTTPと区別する。

## 10. テストと現地判定

自動テスト: 攻撃的なパス、symlink、壊れた/重複/順不同イベント、期限切れ、削除、VTT話者/時刻、Office ZIP上限、日本語検索、非承認タスク、通知重複、静穏時間、予算、Responses tool loop、reasoning保持、未知tool、API誤送信、TLS、Native Messaging分割フレーム。

会社でのみ判定する項目: Python/Tk、拡張とnative hostの許可、proxy/CA、Bedrock認証/モデル/推論地域、Power Automate各アクション、Box SSO/DLP、他人主催文字起こし取得と通知、ファイル実体/同期遅延、SharePoint検索権限。

判定はPASS / FAIL / BLOCKED_POLICY / NOT_TESTED。証拠を添える。「たぶん動く」「未実行だがPASS」は禁止。修復は設定→許可された互換実装→限定フォールバックの順。認証迂回・TLS無効化・EDR停止・権限拡大・別クラウド転送は禁止。

## 11. 公開前/運用前のゲート

ローカル/モック自動テストに通ることと、会社で使えることは別に報告する。実LLM品質、長時間稼働、Windows UIとnative host、クラウドフローは現地試験が必要。初版は検証用であり、管理者レビュー・データ保存/送信承認・保管期限・ログ方針が揃うまで業務情報を投入しない。

## 12. 一次資料（2026-10-16確認）

- OpenClaw source: https://github.com/openclaw/openclaw/tree/72d41b6f9d0bd2290a959f062b134f60664063d7
- Heartbeat: https://docs.openclaw.ai/gateway/heartbeat
- Automation: https://docs.openclaw.ai/automation
- Memory: https://docs.openclaw.ai/concepts/memory
- Bedrock GPT-6 Luna model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html
- Bedrock Responses: https://docs.aws.amazon.com/bedrock/latest/userguide/inference-responses-api.html
- OpenAI function calling: https://developers.openai.com/api/docs/guides/function-calling
- OpenAI reasoning: https://developers.openai.com/api/docs/guides/reasoning
- Teams connector: https://learn.microsoft.com/en-us/connectors/teams/
- Box connector: https://learn.microsoft.com/en-us/connectors/box/
- Native Messaging: https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/native-messaging
- Flow code management: https://learn.microsoft.com/en-us/power-automate/manage-flows-with-code
