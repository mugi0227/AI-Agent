# AI-Agent / Work Assistant

**会社のWindows PCで動く、Bedrockを頭脳にした読み取り中心の業務秘書。** OpenClawを導入せず、定期確認・案件文脈・通知抑制・根拠付き記憶を独自実装しています。

> **検証用の初版です。** ローカル/モックテストは実施していますが、会社テナントの権限、Windows実機、実Bedrock、Power Automateのフローはこのリポジトリだけでは検証できません。初期状態は収集・クラウド推論・定期通知を無効化しています。会社の承認後に必要な機能だけ有効化してください。

> **この納品時点のGitHub反映状況:** 設計書だけがコミット済みです。実装一式の書き込みはChatGPT側のGitHubツールの安全確認でブロックされ、リモートへ反映されていません。初回は提供されたソースZIPを展開してください。通常の承認済み開発手順によるリポジトリへの統合は、[納品状況](docs/DELIVERY_STATUS.md)に記載しています。

## 最初に読むもの

- [詳細設計](docs/DESIGN.md) — 実装より先にコミットした構成・データ契約・安全境界。
- [会社PCのCodexへの依頼文](docs/COMPANY_CODEX_PROMPT.md) — 実行、PASS/FAIL判定、失敗時の修正・代替を含む引き継ぎ。
- [実装・検証状況](docs/IMPLEMENTATION_STATUS.md) — 実装したものと、未検証・未対応の区別。
- [OpenClawから取り入れたこと](docs/OPENCLAW_ANALYSIS.md) / [セキュリティ](docs/SECURITY.md)。
- [Power Automateの組み方](power_automate/README.md) — 標準コネクタ、定義ドラフト生成器、8種類のフロー仕様。

## 構成

```text
Teams / Outlook / SharePoint
        │ 標準Power Automate（会社で構築・検証）
        ▼
Box / OneDrive 専用受渡フォルダ ──→ 正規化・重複排除 ──→ ローカルSQLite
                                                              ↑       ↕
Box / OneDrive / SharePoint同期フォルダ ──→ 許可した資料のみ ───┘   自作Agent
                                                                      ↕
Edgeで選択文をプレビュー → Native Messaging → ローカル受信箱    Bedrock Responses
                                                                      ↕
                                                        デスクトップUI / CLI
```

同期経由の追加取得は非同期。対話は既存のローカル情報と、Edgeから直接渡した文脈を優先します。独自Graphアプリ、Docker、WSL、外部公開HTTPサーバー、OpenClaw本体は必須ではありません。

## 実装されている機能

| 領域 | 内容 |
|---|---|
| 対話 | OpenAI Responses形式のBedrock呼び出し、クライアント側ツール、reasoning項目の引き継ぎ、出典と鮮度、回数/文脈/使用量上限 |
| 読み取り | 許可フォルダのTXT/MD/CSV/HTML/EML/VTT、Office Open XMLの文字、追加ライブラリ利用時のPDFテキスト |
| 検索 | SQLite FTS5と日本語二文字単位の索引。クラウド埋め込みへの無断送信なし |
| 案件と記憶 | ユーザー定義の案件・目的・キーワード、原文引用付き候補、人の承認後の決定/記憶/タスク |
| 先回り | 朝夕の確認、会議前、会議記録の到着、承認済期限、案件キーワードに関連する新情報 |
| 通知制御 | 曜日/時間帯、集中モード、回数制限、cooldown、再起動をまたぐ重複抑制。何もなければ推論しない |
| ブラウザ | 明示操作時のみプレビュー、選択文または表示本文をローカルへ。Cookie/入力欄の収集なし |
| M365接続 | JSON受信、非同期取得依頼、ID照合、期限、再配送・訂正・削除・失効への対応。クラウドフロー自体は会社で構築 |
| 診断 | オフライン診断と、架空nonceを使う実Bedrockツール往復テスト |

**メール送信・削除・任意コマンド実行・勝手な権限変更は機能として持たせていません。** 自分以外が主催するTeams会議の取得権限や通知範囲は個別に検証します。

## 起動（まず架空データで）

Python 3.11以上。標準ライブラリだけで基本機能とテストが動きます。

```powershell
# 初回: 提供されたソースZIPを展開し、そのAI-Agentフォルダへ移動
# GitHubは設計書のみのため、cloneだけでは実装を取得できません。
cd <展開先>\AI-Agent
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-build-isolation -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m work_assistant demo
.\.venv\Scripts\python.exe -m work_assistant init
.\.venv\Scripts\python.exe -m work_assistant gui
```

`demo`は一時フォルダの架空情報のみ。モデルには接続しません。GUIには会話・資料検索・承認待ち・タスク/案件・通知・状態の6タブがあります。ウィンドウを閉じると常駐処理も終了します。CLI常駐は`python -m work_assistant run`、終了はCtrl+C。GUIとrunを同時に多重起動しないでください。

PowerShellのExecutionPolicyや会社の制御は変更しません。Pythonバージョン/パッケージ配布に制限がある場合は会社の承認済み経路を使用します。

## 設定

既定の保存先は **`%LOCALAPPDATA%\WorkAssistant`**。`config.json`を編集し、変更後は再起動します。サンプルは`config.example.json`。DBは同期フォルダに置かないでください。

承認後に設定するもの:
- `local_data_approved`と`roots`（読むフォルダの完全なパス）。最初は小さな専用検証フォルダ。
- `drop_folders`（PAから受け取るJSON）、`outbox`と`bridge_enabled`（追加取得の依頼を出す場合）。
- `allowed_web_hosts`と`extension_ids`（Edgeキャプチャを使う場合）。
- `bedrock.enabled`、`data_transfer_approved`、モデル・endpoint・認証・処理地域。定期推論は別途`heartbeat_enabled`。

クラウドから取得した情報は既定24時間で再確認が必要です。再確認フローがなければ期限後は利用を停止します。原文が失効/改訂した場合、以前の抽出候補や会話キャッシュも保守的に失効させます。これは完全なリアルタイム権限追従ではありません。

### BedrockでGPTを使う

OpenAI本家のAPIキーや`api.openai.com`には接続しません。**OpenAI互換のResponses形式を、Bedrockエンドポイントに送信**します。

| 設定候補 | runtime | Mantle |
|---|---|---|
| base URL | `https://bedrock-runtime.ap-northeast-1.amazonaws.com/openai/v1` | `https://bedrock-mantle.us-east-1.api.aws/openai/v1` |
| モデルID | `global.openai.gpt-6-luna` | `openai.gpt-6-luna` |
| 注意 | 東京受付でもglobal推論。処理地域の承認必須 | このモデルの対応地域と専用base pathを確認 |

上記は2026-10-16のAWSモデルカードを基にした候補で、会社アカウントでの提供・権限を保証しません。設定が合わない場合でも、別モデル/地域/プロバイダーに自動切替しません。

SigV4は承認されたAWSプロファイル/SSOと追加のboto3を利用。APIキー方式は`auth: "api_key"`、キーは`AWS_BEARER_TOKEN_BEDROCK`等の指定環境変数へ。秘密値をconfig.jsonやリポジトリへ書かないでください。すべてのResponses呼び出しで`store:false`を明示しますが、会社/AWS側の別の監査ログ設定を無効にする意味ではありません。

```powershell
# 追加依存は社内承認のある場合だけ
.\.venv\Scripts\python.exe -m pip install --no-build-isolation -e '.[sigv4,pdf]'
.\.venv\Scripts\python.exe -m work_assistant doctor
# 送信と処理地域の承認・認証設定後のみ。架空nonceによる有料の2往復以内の試験
.\.venv\Scripts\python.exe -m work_assistant doctor --live
```

## Edge連携

会社が許可する配布方式で`extension/`を読み込み、取得した拡張IDと許可ホストを本体設定へ登録します。インストール後、`scripts/register-native-host.ps1`を使用してHKCUのNative hostを登録。既存Windows .NET Frameworkコンパイラによる小さな固定用途リレーが必要です。実行/署名ポリシーでブロックされた場合は停止し、[会社PC検証手順](docs/COMPANY_CODEX_PROMPT.md)へ進みます。

拡張のPing→選択文プレビュー→保存を試します。保存はローカルへの転送で、そこから自動的にBedrockへ即時送信するものではありません。アプリの検索・対話・定期確認が、許可設定に基づいて使用します。自律的なサイト巡回やページの自動クリックは初版に含みません。

## 重要な限界

Windows実機と会社M365/Box/Bedrockでの結合試験は未実施です。Power Automateは仕様と定義ドラフトを提供しており、デザイナーでの接続設定・保存・実行が必要です。メール/Teams全履歴、OCR、暗号化文書、画像解析、任意ブラウザ操作、Windowsトースト通知、24時間稼働は未対応です。通知はアプリ内の通知タブに表示します。

既存の業務資料のコピーには会社の保存・転送・保持期間の規定が適用されます。自作だから自動的に安全/承認済みになるわけではありません。

## 開発

`python -m unittest discover -s tests -v`、`python -m compileall -q work_assistant`、`node --check extension/popup.js`。GitHub ActionsにWindows/Linux・Python3.11/3.13のテストを定義しています。実クラウド認証はCIに入れません。

一次資料と参照コミットは[詳細設計](docs/DESIGN.md)と[OpenClaw分析](docs/OPENCLAW_ANALYSIS.md)を参照してください。
