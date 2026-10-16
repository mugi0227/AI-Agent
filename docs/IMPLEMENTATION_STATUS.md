# 実装・検証状況

2026-10-16時点。**実装一式はGitHubの`main`へ反映済みです。** 初回インポートは`3caa5153487df69f852268f244e7963d821a11a3`。実装前の詳細設計コミットは`827315bbd79eed338c78aa1c71b4c0518afa636f`。利用者向けの評価は[完成度レビュー](READINESS_REVIEW.md)を参照してください。

## 実装済み・この環境で検証した範囲

- PythonネイティブのAgent/SQLite/フォルダ収集/日本語索引/原文出典/明示承認/タスク/案件。
- Bedrock専用Responses transport、SigV4またはAPIキー、store:false、reasoningを含むtool roundtrip、SSE parser、能力・予算制約。**transportはモック試験であり実AWS成功ではない**。
- 定期候補の選別、静穏時間、集中モード、通知重複・cooldown、イベントclaimと再試行。
- JSON mailboxの相関・上限・期限・重複、tombstoneと更新順序の判定。
- Chrome・Edge共通のManifest V3拡張とNative Messagingプロトコル。ブラウザ別のWindows relay/登録・解除スクリプト。
- 会話一覧・検索・保管/復元、会話別の履歴/下書き/資料、複数行入力、応答の逐次表示・停止・再実行を持つTk UIとCLI。出典と確認手順、オフライン/実接続用doctor。
- Power Automateの8種類の構築仕様、3つのJSON schema、文字起こしフロー定義ドラフト生成器。

現行UIの検証: Linux / Python3.13.5 / Tk8.6 / Xvfbで、83件のコア試験と14件の実GUI操作試験、計97件が成功。Python3.12.14でもコア試験が成功。compileall、拡張JS構文検査、架空デモも実行。初版の検証記録はTEST_REPORT.mdに保持しています。

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

- OpenClaw互換の全チャネル、プラグイン、シェル、マルチエージェント運用。
- 自律的なブラウザ巡回/自動クリック。現状は明示キャプチャ。
- メール送信、文書削除、外部書き込み、AIによる承認。
- LLMによる自動memory flush/要約compaction。現状は有界コンテキスト＋検索可能な過去会話＋承認済記憶。
- 全M365履歴取得、完全なSharePoint同期、OCR/画像解析、暗号化文書の解除。
- 意味ベクトル検索、全文書分類DLP、DB独自暗号化、署名済配布パッケージ。
- Windowsトースト、システムトレイ、自動更新、自動ログオン起動登録。通知はアプリ内。
- PC停止中の推論、クラウドサービス常駐、24時間稼働。

## 設計上の保守的な選択

原本の改訂/失効時は派生メモ/タスク/会話キャッシュも失効する。古い根拠を保持し続けるより安全側だが、更新頻度の高い資料では再承認が多くなる。会社の保持・権限要件を検証してから、ユーザー所有の独立したタスク記録と出典リンクを分離する発展を検討する。

会議やTeamsの生成時刻・イベント形状、Box同期速度は予測で成功判定していない。実環境での修正指示は`COMPANY_CODEX_PROMPT.md`に記載している。
