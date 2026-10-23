# 納品ソースの実行テスト結果

## キー入力・社内プロキシ v0.5.1の検証（2026-10-23）

- コア178件・実Tk操作43件、計221件。Linux / Python3.13.5 / Tk8.6 / Xvfbと実botocoreで220件が成功し、Windows DPAPIの1件はOS対象外のため除外。Windowsでは非Windows保存拒否・symlink作成の2件を除外する。
- 半角40文字（大文字・小文字・数字・`/+=`）が通ること、全角・途中の空白・不可視文字・引用符を含む40文字は文字種エラーになること、文字数だけの誤りと区別することを検証。前後の空白・改行だけを除去し、入力・エラー・表示にキー本体を出さない。
- 実Tkで文字数表示→40文字の文字種エラー→コピーした値の前後を除去→モデル一覧→暗号化保存の流れを試験。プロキシ変更による一覧失効、取得・保存への反映、不正なPAC指定時の通信抑止、407の案内も操作した。
- 実際のローカルHTTPプロキシにHTTPS CONNECTし、407を分類すること、TLS確立前のプロキシ要求へBedrockのBearer認証を漏らさないことを確認。模擬HTTP境界では407・CONNECT 403・AWS 403・証明書検証・TLS・DNS・タイムアウトを区別し、応答本文・例外の秘匿情報を表示しないことを確認。
- 明示プロキシとOS/環境変数による既定経路を切り替え、ホスト名確認・証明書検証が有効のままであること、不正なCAファイルでは通信しないこと、ID・パスワードやPAC付きURLを拒否することを検証。
- 前版v0.5.0のCIはWindows/Linux・Python3.11/3.13の4構成が成功。今回のコミットの同一試験とNative Messagingの結果は[Actions](https://github.com/mugi0227/AI-Agent/actions)で確認する。会社のPAC・統合認証・実AWSを検証済みとは扱わない。

操作・社内ITに確認する設定・エラーの対応は[Access Keyと社内ネットワーク](ACCESS_KEYS.md)に記載。

## Access Key認証 v0.5.0の検証（2026-10-23）

- 検証対象はコア171件・実Tk操作41件、計212件。OS専用試験は対象外のOSでスキップする。LinuxではWindows DPAPIの1件を除外し、Windowsでは非Windows保存拒否・symlink作成の2件を除外する。
- Linux / Python3.13.5 / Tk8.6 / Xvfbでは211件が成功。Windowsでの実DPAPIを含む結果は、下記Actionsの実行記録で確認する。
- 実際のbotocoreでGET/POSTを署名し、別実装のHMAC計算と一致すること、一時認証のSession Tokenを付けること、ClaudeとResponsesの両経路を確認。Access Key方式からAWSプロファイル・既定認証へフォールバックしないことを検証。
- 保存・再読込・更新・削除、設定と暗号化の失敗時の元データ保持、古い暗号化ファイルの整理と再試行、別Runtimeのロック競合と古い参照での更新拒否を試験。設定・DB・保存ファイルにキー本体がないこと、例外とreprで入力値を出さないことも確認。
- Windows CIでは実DPAPIを使用し、長いSession Tokenの暗号化・再読込、異なる追加エントロピーでの復号拒否、削除を検証する。Linuxの保存試験は明示的な模擬Protectorを使い、製品実装は平文保存へフォールバックしない。
- 実Tkでマスク入力、入力値の表示と再マスク、未保存キーによる一覧取得、保存後の接続テスト、入力欄の消去、更新失敗からの再試行、Token不足時の通信抑止、削除の保存時反映を操作。1240×980と960×700、Token不足の表示を描画して確認。
- すべて架空のキーとHTTP応答を使用。実AWSキーによる接続・IAMの権限・一時キーの有効期限は会社PCの接続テストで確認する。CI結果は[Actions](https://github.com/mugi0227/AI-Agent/actions)を参照。

操作と保存の境界は[Access Keyの設定](ACCESS_KEYS.md)に記載。

## 水色の配色と光沢 v0.4.1の検証（2026-10-21）

- Linux / Python3.13.5 / Tk8.6 / Xvfbでコア157件・実Tk操作36件、計193件が成功。コンパイルと差分の整合性も確認。
- 会話・初期画面・初期設定・モデル選択・実行管理に加え、各一覧の空状態、資料選択、生成中、エラー、960×700を実描画で確認。保存済みの6枚の画面例を更新した。
- 白と淡い水色、濃い青の文字、主操作とロゴの上辺の反射線を使用。ホバー・押下・フォーカス・無効・選択・スクロールバーまで配色を統一。標準部品の操作を継続試験し、新規UI依存は追加していない。
- 文字のコントラストは本文11.93:1、補足5.38:1、主操作7.60:1、サイドバー4.88:1、選択中5.13:1。主操作のホバー7.03:1、押下6.19:1。画面全体のアクセシビリティ認証ではない。
- Windows/Linux・Python3.11/3.13のCIで既存のコア・GUI・Native Messaging試験を継続する。[Actions](https://github.com/mugi0227/AI-Agent/actions)で結果を確認できる。

色・部品・表示状態の基準は[UIデザイン](UI_DESIGN.md)に記載。

## Claude・モデル選択 v0.4.0の検証（2026-10-21）

- コア157件・実Tk操作36件、計193件がLinux / Python3.13.5 / Tk8.6 / Xvfbで成功。Windows/Linux・Python3.11/3.13の同一試験は[Actions](https://github.com/mugi0227/AI-Agent/actions)で確認できる。
- ClaudeのConverse要求形式、ツール定義、複数のツールと結果の対応、署名付きreasoningの保持、Responses固有項目を送らないこと、地域と接続先の制約を検証。
- 実アダプターと模擬HTTP境界で、合成接続テストの2回の推論、使用量計上、資料の取得→出典付き回答の保存、待機→別Runnerによる復元と完了を試験。
- ConverseStreamのテキスト差分とツールJSON、断片化されたread、reasoning署名を確認。CRC破損、途中切断、例外イベント、未完了、ブロック／メッセージ終端欠落では確定・ツール実行を拒否。
- カタログのGET認証、固定AWSホスト、ページ送り・反復tokenの打切り、モデルの対応範囲、利用条件による除外、利用条件を取得できない場合の未確認表示、一部成功・全失敗・0件の区別を試験。受付地域にモデルがないことだけでクロスリージョンのプロファイルを除外しないことも検証。
- 実Tkで一覧取得→Claude選択→保存と即時反映→OpenAI切替、取得失敗・0件・再取得、認証変更による一覧失効、取得中の画面応答と重複操作抑止、遅延結果の棄却、リージョン内Claudeの手入力を操作。
- モデル一覧を実際に開いた状態、960×700、取得失敗も描画して確認。Tclが所有するドロップダウンのフォーカスによる角丸ボタン描画例外を修正し、回帰試験を追加。
- 画面と通信試験は架空のカタログ・モデル応答。社内AWSへの接続・モデル提供状況・回答品質を確認済みとは扱わない。実接続は会社の認証・権限で「保存して接続テスト」から確認する。

操作・必要なIAM権限・対応範囲・AWS公式仕様は[Bedrockのモデル選択](BEDROCK_MODELS.md)に記載。

## デスクトップの明るいUI v0.3.2の検証（2026-10-21）

- Linux / Python3.13.5 / Tk8.6 / Xvfbでコア143件・実Tk操作32件、計175件が成功。Python3.12.14でもコア143件が成功。
- 明るいサイドバー、角丸のパネル・ボタン・入力・表・設定タブを実装。会話・設定・実行管理に加え、資料・承認・案件・通知の空状態、資料選択、生成中、接続失敗、960×700の表示を実際に描画して確認。
- 標準Button/Entryの役割、Enter実行、Tabでのフォーカス移動、日本語文字列の入力、ボタンの端のクリック、無効状態で実行しないこと、角丸の部品と本体をまとめて破棄できることを検証。
- 960×700とTk scaling=2.0でナビゲーション、設定保存、会話の添付・停止・送信・継続タスク登録が表示領域に収まることを試験。従来の初期設定から引用付き回答までの試験も継続。
- 本文/白背景13.33:1、補足/白5.76:1、主ボタン6.44:1、サイドバー5.21:1、選択中5.26:1の文字コントラストを計算で確認。これは画面全体のアクセシビリティ認証ではない。
- Windows/Linux・Python3.11/3.13の4構成のCIで同じコア・GUI試験と既存Native Messaging試験を実行する。結果は[Actions](https://github.com/mugi0227/AI-Agent/actions)を参照。
- 画面例は架空資料と模擬モデル。社内AWS・実機IME・スクリーンリーダー・初見ユーザーの利用テストは未検証。

デザインの判断と参照したImpeccableの公開資料は[UIデザイン](UI_DESIGN.md)に記載。

## 初期設定UX v0.3.1の検証（2026-10-21）

- Linux / Python3.13.5 / Tk8.6 / Xvfbで、コア143件と実Tk操作30件、計173件が成功。Python3.12.14でもコア143件が成功。
- 未設定からの起動→保存の許可→フォルダ選択→AI接続の入力→合成入力の接続テスト→資料取り込み→質問→引用付き回答→原文表示を、設定ファイルの直接編集・アプリ再起動なしで試験。
- 設定保存時の共有参照と詳細設定の保持、保存失敗時の元データ保持、処理中の設定変更拒否、変更途中の設定を別の実行が読まないことを検証。
- 無効値の修正、未承認時の通信抑止、接続失敗からの再試行、古い成功表示の無効化、合成入力のみの送信、最大2回の呼び出しと日次予算への計上を検証。
- 資料0件の案内、許可外ファイルの拒否、取り込み後の添付、最小960×700とTk scaling=2.0でナビゲーション・保存ボタンの表示、資料選択の確定・取消の固定表示を確認。
- CIはWindows/Linux・Python3.11/3.13で同じコア・実Tk試験を実行し、Windows Native Messagingの既存試験を継続。コミットごとの結果は[Actions](https://github.com/mugi0227/AI-Agent/actions)を参照。
- 画面例は実Tkの未設定フォーム。モデル応答は模擬であり、実Bedrock・会社のAWS認証・ブラウザポリシー・実機IMEの疎通確認を実施済みとは扱わない。初見ユーザーを対象にした利用テストは未実施。

品質ゴールと検証の境界は[初期設定の完了条件](SETUP_ACCEPTANCE.md)を参照。

## 継続実行基盤 v0.3.0の検証（2026-10-21）

- Linux / Python3.13.5 / Tk8.6 / Xvfbでコア134件と実Tk操作18件、計152件が成功。Python3.12.14でもコア134件が成功。モデル応答と埋め込みはオフラインのfixtureであり、実Bedrock・実テナント・実埋め込みモデルの品質試験ではない。
- 実プロセスを起動し、非同期取得の依頼直後に`os._exit`で強制終了。DBと依頼ファイルを二重作成せず、保存したモデル出力・reasoning・ツール呼び出しから再開することを確認。
- 複数workerのclaim競合、プロセス間セッション制御、実行権の失効、古い副作用/結果の拒否、対話への譲渡、再試行・期限・日次予算更新待ちを試験。
- 役割別の記憶、資料・案件・ツールの範囲、子へ引き継ぐ権限の積集合、権限変更の子孫への適用、子が未終了の場合の親の待機と結果統合を試験。
- 時刻/追加取得/子タスク/資料変更/人の承認の待機、永続スケジュール、変更の集約、重複イベント、時計の逆行、イベント行の削除後も新しい世代を認識することを試験。
- 会話と作業の要約、ツール呼び出し・結果の対を崩さない圧縮、通信障害時の要約fallback、出典失効、ローカルファイル再取り込み後の再調査、schema1/2から3への互換移行を試験。
- 画面では継続タスクの登録→手動実行→結果→評価、待機→取消、記憶の追加/削除、定期実行の有効化を実Tkで操作。従来の会話・添付・停止・再実行も継続試験。
- Windows/Linux・Python3.11/3.13の4構成のCIでコアと実Tk操作を実行。Windowsでは従来のC#→Python Native Messaging試験も継続。各コミットの結果は[Actions](https://github.com/mugi0227/AI-Agent/actions)を参照。

設計上の対象と制限は[アーキテクチャ評価](ARCHITECTURE_REVIEW.md)、起動・設定・移行は[使い方](DURABLE_WORK.md)に記載。以下は以前のリリース時点の記録。

## 会話UX v0.2.0の検証（2026-10-16）

- Linux / Python3.13.5 / Tk8.6 / Xvfbで83件のコア試験と14件の実GUI操作試験、計97件が成功。コアはPython3.12.14でも成功。
- GUI試験: 改行/Ctrl+Enter、会話別下書き/添付、再起動後の履歴、資料検索/原文/選択ダイアログ、再実行、停止、生成中の会話切替、最小960×700、Markdown、保管/復元/検索/コピー、オフライン診断、失効時の遅延イベント拒否。
- コアの追加試験: schema1の履歴移行、中断した依頼の復旧、会話間の分離、下書きと資料メタデータの失効、添付参照と出典保存、途中出力の非保存、質問を重複登録しない再実行、失効中の回答を再保存しないこと。
- 画面例は架空の資料とオフラインのモック応答で作成。実Bedrockの成功を示す画像ではない。
- CIにWindows/Linux・Python3.11/3.13での実GUI操作試験を追加。現在の結果は[Actions](https://github.com/mugi0227/AI-Agent/actions)を参照。

## Chrome・Edge対応時の追試（2026-10-16）

- Linux / Python 3.12.14 / Node 24.19.0で69件のunittest、compileall、架空デモ、拡張JS構文検査、Power Automateドラフト生成が成功。
- 初回GitHub CIはLinuxのPython3.11/3.13で成功。Windowsの同2構成では、テスト内の`Path.write_text/read_text`で文字コードを省略したため、日本語データの4試験が失敗。該当箇所にUTF-8を明示する修正を追加。
- Windowsで68件のテストが通った後、CLIデモの日本語出力が標準出力の既定文字コードにより失敗する問題も確認。CLIの出力をUTF-8へ統一し、`PYTHONIOENCODING=cp1252`でも日本語JSONが正常に出力されるsubprocess回帰試験を追加（合計69件）。
- Windows CIへ`tests/test-native-host.ps1`を追加。Chrome・Edgeを別々に登録し、C#→分離モードのPython→Ping/日本語capture、未許可ID拒否、Edge解除後のChrome接続を試験する。実ブラウザUIや実テナントの確認は含まない。
- 以降の各コミットのWindows/Linux実行結果は[GitHub Actions](https://github.com/mugi0227/AI-Agent/actions)を参照。

以下は初版ZIP作成時の記録であり、現在のGitHubへの反映状況・CI実行状況とは区別する。

## 初版ZIPの検証記録

実施日: 2026-10-16。環境: Linux / Python 3.13.5.

**この結果はローカル/モック/架空データによるものです。Windows、AWS実接続、M365/Boxテナント、GitHub CIは未実行です。**

## python -m unittest discover -s tests -v

結果: PASS

```text
test_call_id_collision (test_core.AgentTests.test_call_id_collision) ... ok
test_cancellation (test_core.AgentTests.test_cancellation) ... ok
test_model_incomplete (test_core.AgentTests.test_model_incomplete) ... ok
test_no_approval_tool (test_core.AgentTests.test_no_approval_tool) ... ok
test_no_notify_in_chat (test_core.AgentTests.test_no_notify_in_chat) ... ok
test_read_before_propose (test_core.AgentTests.test_read_before_propose) ... ok
test_reasoning_and_call_id_roundtrip (test_core.AgentTests.test_reasoning_and_call_id_roundtrip) ... ok
test_round_limit (test_core.AgentTests.test_round_limit) ... ok
test_runtime_offline (test_core.AgentTests.test_runtime_offline) ... ok
test_strict_tool_arguments (test_core.AgentTests.test_strict_tool_arguments) ... ok
test_unknown_tool_not_executed (test_core.AgentTests.test_unknown_tool_not_executed) ... ok
test_unverified_citation_blocked (test_core.AgentTests.test_unverified_citation_blocked) ... ok
test_docx_extract (test_core.IngestionTests.test_docx_extract) ... ok
test_drop_stability_and_retry_fixed_file (test_core.IngestionTests.test_drop_stability_and_retry_fixed_file) ... ok
test_file_refresh_after_change (test_core.IngestionTests.test_file_refresh_after_change) ... ok
test_html_removes_active_content (test_core.IngestionTests.test_html_removes_active_content) ... ok
test_live_file_revalidation (test_core.IngestionTests.test_live_file_revalidation) ... ok
test_path_outside (test_core.IngestionTests.test_path_outside) ... ok
test_secret_name (test_core.IngestionTests.test_secret_name) ... ok
test_state_blocked (test_core.IngestionTests.test_state_blocked) ... ok
test_symlink_blocked (test_core.IngestionTests.test_symlink_blocked) ... ok
test_vtt_speakers_and_times (test_core.IngestionTests.test_vtt_speakers_and_times) ... ok
test_xml_entities_rejected (test_core.IngestionTests.test_xml_entities_rejected) ... ok
test_business_hours (test_core.ScheduleTests.test_business_hours) ... ok
test_foreground_yields_monitor (test_core.ScheduleTests.test_foreground_yields_monitor) ... ok
test_no_change_no_inference (test_core.ScheduleTests.test_no_change_no_inference) ... ok
test_notification_cooldown (test_core.ScheduleTests.test_notification_cooldown) ... ok
test_notification_dedupe (test_core.ScheduleTests.test_notification_dedupe) ... ok
test_overnight_weekday (test_core.ScheduleTests.test_overnight_weekday) ... ok
test_persistent_claim (test_core.ScheduleTests.test_persistent_claim) ... ok
test_quiet_hours (test_core.ScheduleTests.test_quiet_hours) ... ok
test_weekend (test_core.ScheduleTests.test_weekend) ... ok
test_atomic_budget (test_core.StorageTests.test_atomic_budget) ... ok
test_audit_no_body (test_core.StorageTests.test_audit_no_body) ... ok
test_calendar_requires_interval (test_core.StorageTests.test_calendar_requires_interval) ... ok
test_clock_requires_timezone (test_core.StorageTests.test_clock_requires_timezone) ... ok
test_collision (test_core.StorageTests.test_collision) ... ok
test_completed_task_not_revived (test_core.StorageTests.test_completed_task_not_revived) ... ok
test_default_off (test_core.StorageTests.test_default_off) ... ok
test_delete_before_arrival (test_core.StorageTests.test_delete_before_arrival) ... ok
test_deleted_cannot_resurrect_out_of_order (test_core.StorageTests.test_deleted_cannot_resurrect_out_of_order) ... ok
test_duplicate_receipt (test_core.StorageTests.test_duplicate_receipt) ... ok
test_expiry_removes_derived (test_core.StorageTests.test_expiry_removes_derived) ... ok
test_fake_quote_blocked (test_core.StorageTests.test_fake_quote_blocked) ... ok
test_fts_query_is_data (test_core.StorageTests.test_fts_query_is_data) ... ok
test_japanese_search (test_core.StorageTests.test_japanese_search) ... ok
test_new_authorized_observation_after_revocation (test_core.StorageTests.test_new_authorized_observation_after_revocation) ... ok
test_not_approved (test_core.StorageTests.test_not_approved) ... ok
test_old_revision (test_core.StorageTests.test_old_revision) ... ok
test_proposal_approval (test_core.StorageTests.test_proposal_approval) ... ok
test_reserved_metadata (test_core.StorageTests.test_reserved_metadata) ... ok
test_bridge_allowlist (test_core.TransportTests.test_bridge_allowlist) ... ok
test_bridge_duplicate_and_no_retained_body (test_core.TransportTests.test_bridge_duplicate_and_no_retained_body) ... ok
test_bridge_unknown_id (test_core.TransportTests.test_bridge_unknown_id) ... ok
test_capture_disallowed_host (test_core.TransportTests.test_capture_disallowed_host) ... ok
test_capture_disallowed_origin (test_core.TransportTests.test_capture_disallowed_origin) ... ok
test_capture_url_redaction (test_core.TransportTests.test_capture_url_redaction) ... ok
test_cross_region_explicit (test_core.TransportTests.test_cross_region_explicit) ... ok
test_endpoint_openai_blocked (test_core.TransportTests.test_endpoint_openai_blocked) ... ok
test_endpoint_suffix_attack (test_core.TransportTests.test_endpoint_suffix_attack) ... ok
test_mantle_special_path (test_core.TransportTests.test_mantle_special_path) ... ok
test_native_binary_unicode (test_core.TransportTests.test_native_binary_unicode) ... ok
test_native_fragmented_stream (test_core.TransportTests.test_native_fragmented_stream) ... ok
test_native_length_guard (test_core.TransportTests.test_native_length_guard) ... ok
test_runtime_profile_required (test_core.TransportTests.test_runtime_profile_required) ... ok
test_sse_delta_and_completion (test_core.TransportTests.test_sse_delta_and_completion) ... ok
test_sse_truncation (test_core.TransportTests.test_sse_truncation) ... ok
test_wire_store_false (test_core.TransportTests.test_wire_store_false) ... ok

----------------------------------------------------------------------
Ran 68 tests in 0.281s

OK

```

## python -m compileall -q work_assistant

結果: PASS

```text

```

## node --check extension/popup.js

結果: PASS

```text

```

## python -m work_assistant demo

結果: PASS

```text
{
  "mode": "offline_synthetic_demo",
  "results": [
    {
      "chunk_id": "6afd7859-9c30-5d54-971e-0e8515b87d09:0",
      "text": "田中: 塗工幅を測定しました。\n佐藤: 次回までに測定条件を確認します。",
      "id": "6afd7859-9c30-5d54-971e-0e8515b87d09",
      "title": "架空データ: 塗工条件の検討",
      "kind": "transcript",
      "source_url": "https://example.invalid/demo",
      "modified_at": "2026-10-16T04:53:01.163448+00:00",
      "observed_at": "2026-10-16T04:53:01.163448+00:00",
      "expires_at": "2026-10-17T04:53:01.163448+00:00",
      "rank": -5.124999999999999e-06
    }
  ]
}

```

## python power_automate/build_transcript_draft.py --sink onedrive --output /mnt/data/pa-review-only-draft.json

結果: PASS

```text

```

## xvfb-run -a python <synthetic GUI smoke>

結果: PASS

```text
PASS: Linux/Xvfb GUI launch, six tabs, Japanese search, close (not Windows validation)

```

## python -m pip wheel --no-index --no-deps --no-build-isolation .

結果: PASS

```text
Processing <source-root>
  Preparing metadata (pyproject.toml): started
  Preparing metadata (pyproject.toml): finished with status 'done'
Building wheels for collected packages: work-assistant
  Building wheel for work-assistant (pyproject.toml): started
  Building wheel for work-assistant (pyproject.toml): finished with status 'done'
  Created wheel for work-assistant: filename=work_assistant-0.1.0-py3-none-any.whl size=48980 sha256=4242fdf7531a8b0b0fca96ed358adcd8dfe43c7d3919a91c938a9b6e4579d0be
  Stored in directory: /tmp/pip-ephem-wheel-cache-5g0n52bf/wheels/70/41/13/862a3e9a530d27701c470ffd45ff3dac9cb42aff85e5b126ad
Successfully built work-assistant
WARNING: The directory '/home/oai/.cache/pip' or its parent directory is not owned or is not writable by the current user. The cache has been disabled. Check the permissions and owner of that directory. If executing pip with sudo, you should use sudo's -H flag.

```

## 再実行

ZIP展開先のAI-Agentで `python -m unittest discover -s tests -v` を実行してください。会社PCでの判定はCOMPANY_CODEX_PROMPT.mdに従ってください。
