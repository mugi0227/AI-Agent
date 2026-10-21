# 実装・検証状況

2026-10-21 / v0.3.0。構造と検証は[アーキテクチャ評価](ARCHITECTURE_REVIEW.md)、操作は[継続タスク](DURABLE_WORK.md)に記載。 初回インポートは`3caa5153487df69f852268f244e7963d821a11a3`。実装前の詳細設計コミットは`827315bbd79eed338c78aa1c71b4c0518afa636f`。利用者向けの評価は[完成度レビュー](READINESS_REVIEW.md)を参照してください。

## 実装済み・この環境で検証した範囲

- PythonネイティブのAgent/SQLite/フォルダ収集/日本語索引/原文出典/明示承認/タスク/案件。
- Bedrock専用Responses transport、SigV4またはAPIキー、store:false、reasoningを含むtool roundtrip、SSE parser、能力・予算制約。**transportはモック試験であり実AWS成功ではない**。
- 定期候補の選別、静穏時間、集中モード、通知重複・cooldown、永続化された実行キューと再試行。
- 継続タスクの状態機械・チェックポイント・receipt・リース回復・待機/再開、親子の委譲と結果統合、版付き成果物、承認待ちと日次予算更新待ち。
- 役割別の記憶・資料/案件/ツールの範囲、権限変更の委譲先への適用、会話/作業の要約、FTS5と任意のローカル埋め込みによる検索。
- 永続タイマー・定期実行・イベント、変更の集約と重複排除、調査候補の効用ルール、実行履歴・使用量・人の評価からの指標。
- JSON mailboxの相関・上限・期限・重複、tombstoneと更新順序の判定。
- Chrome・Edge共通のManifest V3拡張とNative Messagingプロトコル。ブラウザ別のWindows relay/登録・解除スクリプト。
- 会話一覧・検索・保管/復元、会話別の履歴/下書き/資料、複数行入力、応答の逐次表示・停止・再実行を持つTk UIとCLI。出典と確認手順、オフライン/実接続用doctor。
- Power Automateの8種類の構築仕様、3つのJSON schema、文字起こしフロー定義ドラフト生成器。

現行の試験環境と件数は[TEST_REPORT.md](TEST_REPORT.md)に記載。モデル応答はオフラインのfixtureであり、実AWSの成功と区別します。実プロセスの強制終了・DB再オープン・複数worker・実Tkの操作も試験します。

## 会社PCでの検証が必要な範囲

| 対象 | 現在の判定 | 判定方法 |
|---|---|---|
| Windows UI/Tk、ACL、長時間稼働 | NOT_TESTED | 実機起動・操作・権限・スリープ復帰 |
| Native hostの署名/Chrome・Edgeの実ブラウザ接続 | NOT_TESTED | 各ブラウザでregister→Ping→架空capture→検索 |
| 実Bedrock認証・処理地域・reasoning互換 | NOT_TESTED | 承認後doctor --live |
| GPTによる候補や通知の品質 | NOT_TESTED | 架空案件の評価と勤務日試用 |
| Power Automate実フロー | NOT_TESTED | 接続を選択し新規構築・テスト実行 |
| 他人主催会議の文字起こし取得/通知 | NOT_TESTED | 主催者別、閲覧/保存/API/Triggerを分離 |
| Box SSO/DLP・同期遅延 | NOT_TESTED | 固定架空JSON配送と実測 |
| SharePoint検索の範囲 | NOT_TESTED | 固定許可サイト検索と全文/抜粋比較 |

GitHub ActionsはWindows/Linux・Python3.11/3.13でコア試験と実GUI操作試験を実行します。WindowsではC#コンパイル、Chrome/Edge登録、実行ファイル→PythonのPing/日本語captureと拒否・解除も試験します。[最新の実行結果](https://github.com/mugi0227/AI-Agent/actions)を確認してください。会社の実ブラウザ・実機試験とは別です。

## 未実装・意図的な非対応

- OpenClaw互換の全チャネル、公開Web Gateway、プラグイン、シェル、分散サーバー運用。
- 自律的なブラウザ巡回/自動クリック。現状は明示キャプチャ。
- メール送信、文書削除、外部書き込み、AIによる承認。
- 画面操作を観察する手順学習、自動的に恒久ルールを書き換える機構、複数OSユーザーの強制隔離。
- 全M365履歴取得、完全なSharePoint同期、OCR/画像解析、暗号化文書の解除。
- 実埋め込みモデルでの品質評価、大規模ANN、全文書分類DLP、DB独自暗号化、署名済配布パッケージ。
- Windowsトースト、システムトレイ、自動更新、自動ログオン起動登録。通知はアプリ内。
- PC停止中の推論、クラウドサービス常駐、24時間稼働。

## 設計上の保守的な選択

原本の改訂/失効時は派生メモ/候補/会話/要約/途中状態/成果物も失効する。人が明示的に登録した継続タスクの目的は区別して保持し、改訂時は最新情報からやり直す。アクセス不能・失効時は自動継続しない。更新頻度の高い資料では再調査・再承認が多くなる。

会議やTeamsの生成時刻・イベント形状、Box同期速度は予測で成功判定していない。実環境での修正指示は`COMPANY_CODEX_PROMPT.md`に記載している。
