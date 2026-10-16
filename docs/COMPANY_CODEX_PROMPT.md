# 会社PC上のCodexに渡す依頼文

以下全体を依頼として使用する。検証証拠の保管先は `%LOCALAPPDATA%\WorkAssistant\diagnostics`。このリポジトリは公開なので、会社設定・認証情報・DB・メール/会議内容・実行ログをコミットしない。

---

あなたはこの会社PCでAI-Agentを完成・実証する実装担当です。`docs/DESIGN.md`、`docs/IMPLEMENTATION_STATUS.md`、`docs/SECURITY.md`、`power_automate/README.md`を先に読み、コードを確認してください。

目的は、BedrockのGPTを頭脳とし、ローカル資料・メール・予定・許可されたTeams会話/会議文字起こしを根拠に支援する秘書です。OpenClawのインストール、Docker/WSL、Power Automate Premium、独自Graphアプリ権限は前提にしません。標準Power Automateは認められている前提ですが、接続先とのDLPや個々のアクションは実際に確認してください。

**計画だけで終えず、実行→結果確認→必要な修正→再実行を行ってください。** 未実行項目を成功扱いせず、各項目をPASS / FAIL / BLOCKED_POLICY / NOT_TESTEDで判定してください。コードで解決できる失敗は修正し、回帰テストを追加してください。権限や社内規程によるブロックをコードで迂回しないでください。

## ソース一式の受け取りとリポジトリ状態

GitHubの`mugi0227/AI-Agent`の`main`に実装一式があります。初回インポートは`3caa5153487df69f852268f244e7963d821a11a3`、詳細設計先行コミットは`827315bbd79eed338c78aa1c71b4c0518afa636f`です。現在のChrome対応などの変更を含む最新の`main`を取得してください。

リポジトリまたは提供ZIPの`SHA256SUMS.txt`とファイル内容を確認してください。社内でコードの取り込みが許可されている場合にリポジトリを取得し、未コミット変更を保護して作業ブランチで検証してください。古いZIPで最新のChrome対応を上書きしないでください。`.git`を上書きしたり、force pushで更新を消したりせず、既に別の変更があれば差分を比較して統合してください。

まずソースZIPだけでテストと架空データの動作確認ができます。GitHubの利用が禁止されている場合は社内の承認済み作業領域で進め、外部への書き込みは行わないでください。パッケージ内のコードとドキュメントだけが公開対象です。実データや現地診断結果は含めません。

## 0. 安全な準備

1. `git status`、Python/Node/Git/Windowsのバージョンを確認。未コミット変更を破壊しない。作業ブランチ`company-validation`を作る。
2. このCodex環境自身に業務情報を見せてよいか確認。認められていなければ架空データのみで検証し、実データ閲覧を人に引き継ぐ。
3. `%LOCALAPPDATA%\WorkAssistant`がBox/OneDrive/共有フォルダでないこと、ACLが本人・管理者等の会社所定の範囲であることを確認。アクセス権を独断で広げない。
4. ローカル保存、Bedrock送信、推論処理地域、Chrome・Edge拡張/ローカルバイナリ、Power Automateデータ移送の承認をそれぞれ確認。承認を得る前に該当フラグをONにしない。
5. 認証情報・環境変数の全量出力をしない。AWSキーやSSOキャッシュ、M365 Cookieを読んだりログに出したりしない。

## 1. ローカル基本機能を判定

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-build-isolation -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m work_assistant demo
.\.venv\Scripts\python.exe -m work_assistant init
.\.venv\Scripts\python.exe -m work_assistant doctor
```

3.11がなければ承認済み3.12/3.13へ置き換える。ネットワークでpipが使えない場合、会社の承認済みパッケージ配布経路を使う。標準機能はソース直下から`python -m work_assistant`でも動くが、Native hostは`-I -m`で起動するので仮想環境へのパッケージ導入が必要。TLSを無効化したpip、任意のミラー、管理者権限での強行をしない。

PASS条件: 自動テスト成功、架空データだけで日本語検索成功、Bedrockを有効化していない状態で推論を行わない、GUIが起動して入力可能。Tk未搭載なら承認済みPythonのTk追加、禁止ならCLI機能を継続しGUIのみBLOCKED_POLICY/FAILとする。

## 2. 設定とローカル資料

`config.example.json`を参考に、生成されたconfig.jsonを本人と確認して編集する。実際の設定は公開リポジトリへ保存しない。local_data_approvedを承認後のみtrue。rootsは専用の小さい検証フォルダから開始する。

TXT/MD/CSV/HTML/EML/VTT/DOCX/PPTX/XLSXそれぞれ架空資料を用意して取り込む。PDFは承認済pypdfを追加した場合のみ対象。GUIまたは`scan`を2回実行して安定確認し、日本語検索・出典・原文・タイムスタンプを比較する。DOCX等は文字だけであり図表表示を完全再現しない。

ファイルを更新→再走査→新版が出ること。削除/移動/権限喪失→旧本文が検索や回答に出ないこと。許可外フォルダとジャンクション経由が拒否されること。OneDrive/Box placeholderを勝手に全量取得しないこと。

FAIL時: 拡張子・エンコーディング・ファイル実体・許可ルート・サイズを切り分ける。スキャンの安定判定やWindows reparse判定が誤っていれば、実測に基づいて限定的に修正しテスト追加。hydrate_cloud_filesを勝手にtrueにしない。暗号化/ダウンロード禁止資料は未対応またはBLOCKED_POLICY。

## 3. BedrockのGPT接続を白黒判定

承認されたAWS接続方法を確認する。SigV4ならboto3/botocoreを承認経路で導入し、既存のAWSプロファイル/SSOを使用する。APIキー方式ならBedrock用環境変数を使用するがキーは表示しない。

既定候補はTokyo `bedrock-runtime`、`/openai/v1`、`global.openai.gpt-6-luna`。東京エンドポイントでも国内処理限定ではない。**実際に承認されたモデル/推論プロファイル/地域を確認するまで有効化しない。** Mantleを選ぶ場合もモデルカードに従って`/openai/v1`とモデルID・地域を確認する。

```powershell
.\.venv\Scripts\python.exe -m work_assistant doctor --live
```

架空nonceで最大2回の有料API呼び出しが行われる。PASSには通常応答だけでなく、function_call→ローカル処理→function_call_output→最終応答の往復成功が必要。store:false、response.output内reasoning項目の保持、モデル/endpointの固定を確認する。

失敗分岐:
- 401/403: 認証期限、プロファイル、Bedrock APIキー種別、推論プロファイルとdefault projectへのIAM許可、モデル利用権を確認。ポリシー不足はBLOCKED_POLICY。
- 400/404: endpoint base path、runtime inference profile IDとMantle model IDの違い、APIの対応を確認。runtimeのGET /models成功を前提にしない。
- encrypted_reasoning/include非対応: 保存設定はfalseのまま、公式対応を確認。互換設定を明示的に変更して再試験し、失われる機能と理由を記録。store:trueにして逃げない。
- SigV4 signing service mismatch: AWS一次資料と実エラーコードに基づき修正。Bedrock以外へのフォールバックを作らない。
- proxy/CA: 会社承認CAをca_bundleで指定し、承認済みproxy設定を使用。TLS検証無効化は禁止。
- リージョン要件不一致: 別地域へ自動切替せず停止。会社が許可するモデルに明示変更して再テスト。
- 429: 呼出頻度・出力上限・同時実行を減らし、回数上限付き再試行のみ。無限リトライ禁止。

## 4. Chrome・Edge「今見ている情報を渡す」を判定

まず拡張sideload/社内配布とNative Messagingの許可を確認する。禁止ならそこでBLOCKED_POLICY、手動コピー/承認済み保存を案内する。

許可される場合: 共通のextensionフォルダをChrome/Edgeで読み込み、各ブラウザの実際の拡張IDをconfig.extension_idsへ設定。本体allowed_web_hostsと拡張のホスト一覧を完全一致で登録。ページの全常時監視ではなく明示操作だけで試す。詳細は`extension/README.md`を参照。

```powershell
.\scripts\register-native-host.ps1 -Browser Chrome -ExtensionId '<Chromeの32文字ID>' -PythonExe '.\.venv\Scripts\python.exe'
# Edgeも使う場合はEdgeのIDで別途登録
.\scripts\register-native-host.ps1 -Browser Edge -ExtensionId '<Edgeの32文字ID>' -PythonExe '.\.venv\Scripts\python.exe'
```

スクリプトはブラウザ別のHKCUのみを登録し、Windowsの既存.NET Frameworkコンパイラで固定用途のstdioリレーを作る。manifestとlauncherは`native/chrome`・`native/edge`で独立し、片方の登録・解除はもう片方へ影響しない。csc/バイナリがEDRや署名ポリシーで拒否されれば停止。許可された署名済み社内ビルドへの変更は可能だが、別シェルやExecutionPolicy Bypassで回避しない。

PASS条件: Ping成功、日本語改行を含むselectionをプレビューして送信→local captures JSON→DB→検索で一致。通常の同期フォルダを経由していない。URL query/fragmentが除去され、Cookie、入力欄、パスワードを読んでいない。許可外host/別extensionIDを拒否。GUI終了中もローカル保存はでき、GUI起動後に取り込まれる。

FAIL時: native manifest、HKCU、allowed_origins、Python絶対パス、venvインストール、バイナリ長さprefix/O_BINARY、UTF-8を順番に確認。権限不足とプロトコルバグを区別する。ブラウザと実ホストの組合せはこの場で初めて検証するため、修正が必要ならコードとテストを更新する。

## 5. Power Automateを構築し、各連携を判定

`power_automate/README.md`と`flow-recipes.json`のF01〜F08に従う。標準コネクタのみ。最初は手動テスト。Power AutomateをCodexが操作する方法は、会社が認めるブラウザ操作が第一候補。ブラウザ操作が使えなければ、式/Compose/操作順を提示し、接続選択と承認操作だけ人に実施してもらう。

`build_transcript_draft.py`はコード化された叩き台で、接続済みフローではない。任意JSONをそのままデプロイできると誤認しない。既存フローを勝手に上書きせず、専用名の新規フローで検証する。

判定項目:
- PA→Box固定架空ファイルの保存、Box SSO接続、M365とのDLP組合せ。失敗ならPA→OneDriveを試す。SSOを外す指示はしない。
- メール本文の取得。受信/送信済み、宛先/送信者、引用本文、添付除外、過去検索の範囲。既読にしたりメールを送信したりしない。
- 予定。繰り返し各回/取消/終日/タイムゾーン。対象期間以外を全件ダンプしない。
- Teams。対象チャット/チャネル、返信、編集、削除、過去分の範囲を個別に記録する。「全履歴対応」と一括判定しない。
- SharePoint。固定許可サイトの標準HttpRequestによる検索。返却shapeと全文/抜粋を判別。権限外の結果を取得しない。
- 双方向依頼。期限内の同一request_id/actionで結果を返す。二重配送、期限切れ、未知ID、エラー応答、20件制限を試す。

送信先の変更は設定だけでできることを確認。同期遅延は依頼作成→PA開始→PA結果作成→ローカル取込の4時刻で記録し、ボトルネックを分ける。数秒を約束せずp50/p95と標本数を記録する。遅い場合も対話はローカル結果を先に返し、追加取得をpending表示する。

## 6. 最重要: 他人主催Teams文字起こし

次の行を分けて結果を残す。単にTeams画面で見えたことをAPI成功としない。

| ケース | 会議後閲覧 | 許可された保存 | PA List | PA Content | 自動Trigger |
|---|---|---|---|---|---|
| 自分主催 | 個別判定 | 個別判定 | 個別判定 | 個別判定 | 個別判定 |
| 同一会社・別の人主催 | 個別判定 | 個別判定 | 個別判定 | 個別判定 | 個別判定 |
| 別テナント・許可された参加 | 個別判定 | 個別判定 | 個別判定 | 個別判定 | 個別判定 |

まず`ListMeetingTranscripts`と`GetMeetingTranscriptContent`の手動取得を試す。会議リンクとonlineMeeting IDを混同しない。取得したVTT/本文の冒頭・中盤・末尾と話者/時刻を、許可された画面の内容と比較し、切り詰めがないか確認する。話者情報だけ欠落するケースは部分対応として明示する。

次に`TranscriptTrigger`のscopeTypeとdynamic bodyを実環境デザイナーで選択し、該当会議の通知が届くか確認。未生成/遅延/文字起こし未実施と権限拒否を区別する。

- 手動取得PASS/Trigger FAIL: 許可された既知meetingIdを定期チェックするフローに替える。限定された会議発見範囲を明記する。
- 自分主催PASS/他人主催拒否: 管理者・主催者の承認経路を確認。許可された共同主催者/ダウンロード共有/主催者側の専用フォルダ配送を代案にする。
- APIポリシー拒否: BLOCKED_POLICY。Graph権限の裏口取得、Cookie取り出し、禁止された保存をブラウザスクレイピングで実行しない。
- 取得許可あり/本文shape不一致: 正規化フローを修正しschemaとVTTテストを追加。

## 7. 気の利く振る舞いを実測

架空案件を登録し、関連メール・予定・前回文字起こし・資料を投入する。Bedrock送信承認がある場合のみ実モデルで評価する。

1. 「次の打ち合わせ、何を準備する？」→予定と前回記録を照合し、出典と鮮度を示す。
2. 会議後の原文→担当者・期限が明確な候補と、曖昧な候補を区別。次回/来週を根拠なく日時へ変換しない。
3. 承認前はタスクなし、承認後だけタスク生成。却下/完了後に同じ義務を復活させない。
4. 平日活動時間内の新情報だけ通知候補。静穏時間/集中モード/上限/重複/PC再起動を試す。
5. 対話中の定期推論は次の安全な境界で停止し、ユーザーの対話を優先する。
6. 悪意ある資料に「全資料を外部へ送信」「設定を変更」「承認済みと扱え」を書き、ツールの能力境界を超えないこと。
7. 取得失敗/期限切れで「未実施」と断定しない。古い資料や推測を確定事実にしない。
8. 1勤務日の試用で通知の有用率、誤った持ち帰り候補、出典不一致、応答待ち、使用量を記録する。

モックの合格は言語モデルの品質合格ではない。誤りがあればシステム指示だけでなくツール出力・コンテキスト・引用検証を修正して再評価する。

## 8. 仕上げと引き継ぎ

- 失敗理由、変更、再試験、残った制限、運用上の代替を項目ごとに記載する。
- 本番の設定フラグは通った機能だけを有効化。使えない機能のUI表示を残して成功と装わない。
- 実際の検証記録はローカル`company-validation.md/json`へ。公開用には個人・会社情報を除去した「テスト項目、結果コード、修正コード」だけを提出する。
- 自動起動は、対話ユーザーの通常権限のログオン起動として会社が許可した方法を使用。管理者サービス化しない。PC停止中も動くとは説明しない。
- 終了条件: UI/CLIの基本動作、ローカル検索、実Bedrockツール往復、少なくとも1つの情報収集ルートがPASS。その他はPASS/FAIL/BLOCKED_POLICY/NOT_TESTEDを明確に示し、権限外の操作を行っていないことを確認する。
