# 納品・リポジトリ反映状況

2026-10-21 / v0.4.0更新。

Claude Converse / ConverseStream、AWSからのモデル一覧取得、利用条件表示、画面でのモデル選択を追加。必要な権限・対応範囲と確認方法は[Bedrockのモデル選択](BEDROCK_MODELS.md)に記載。

明るい配色と共通の角丸部品を、会話・設定・資料・実行管理・各ダイアログへ反映。設計指針と操作状態は[UIデザイン](UI_DESIGN.md)を参照。

初期設定フォーム、再起動不要の設定反映、合成入力による接続テスト、許可フォルダからの資料追加を実装。品質ゴールは[初期設定の完了条件](SETUP_ACCEPTANCE.md)、検証範囲は[試験記録](TEST_REPORT.md)を参照。

## GitHubへ反映済み

- 詳細設計先行コミット: `827315bbd79eed338c78aa1c71b4c0518afa636f`。
- ZIP内44ファイルのインポート: `3caa5153487df69f852268f244e7963d821a11a3`。ZIPとリモートのファイル内容の一致を確認済み。
- 現在のソースにはPython秘書本体、Tk UI、CLI、Chrome・Edge共通拡張、ブラウザ別Native Messaging登録、Power Automateデータ契約・構築仕様を含む。
- v0.2.0で会話ワークスペースを改善。会話別の履歴/下書き/資料、逐次表示、停止と再実行、出典の確認を追加。操作と検証は[会話UX](CHAT_UX.md)に記載。
- v0.3.0で永続タスク・待機/再開・役割別の記憶と権限・会話/作業の要約・イベント/定期実行・品質指標を追加。構造と検証は[アーキテクチャ評価](ARCHITECTURE_REVIEW.md)、操作は[継続タスク](DURABLE_WORK.md)を参照。
- 設計ファイルのGit blob SHAは`f34ef88c1a36b282640c4b679f01982c7c4a6c90`。設計先行の原本と同一。

初期ZIPの「設計書のみGitHubへ反映」「実装の書き込みがブロックされた」という記述は過去の納品時点のものです。実装はその後、利用者の指示でGitHubへ反映されました。

## 利用開始

最新のリポジトリを取得して、READMEのテストと架空デモから開始できます。拡張は[Chrome・Edge導入手順](../extension/README.md)、会社PCでの検証は[引き継ぎ手順](COMPANY_CODEX_PROMPT.md)に記載しています。古いZIPより新しい`main`を優先してください。

## まだ完了していないこと

会社PCでのUI・実ブラウザ接続・署名/実行ポリシー、実Bedrock、接続済みPower Automateフロー、他人主催のTeams文字起こし権限は未検証です。完成度と残る作業は[コードレビュー](READINESS_REVIEW.md)、CI結果は[Actions](https://github.com/mugi0227/AI-Agent/actions)で確認できます。

本番完成・運用承認を意味しません。接続情報・DB・業務データ・現地診断ログは公開リポジトリへ追加しません。
