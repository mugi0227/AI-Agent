# 標準Power Automateとの接続

ここにあるのは**フロー構築仕様、入出力スキーマ、定義ドラフト生成器**です。接続済みフローやインポート可能な完成ZIPではありません。会社の環境・接続参照・権限・動的トリガースキーマを確認して初めて実際のフローになります。

## 作る順序

1. 手動トリガー → 固定の架空JSON → Box/OneDrive「ファイルの作成」。これで接続と同期だけ検証する。
2. F03の手動文字起こし取得。自分主催を通した後、他人主催を試す。
3. F01メールとF02予定。対象メールボックス/フォルダ/期間を限定する。
4. F04自動文字起こしとF05対象Teams会話。通知範囲は取得権限とは別判定。
5. F06リクエスト受け付けとF07 SharePoint検索。応答待ちは非同期。
6. F08キャッシュ再確認。これがないと24時間のクラウドleaseで情報が利用不可になる。

`flow-recipes.json`は各フローの処理順・コネクタ・失敗時分岐を定義します。

## Codexが画面を構築するときの材料

```powershell
python power_automate/build_transcript_draft.py --sink onedrive --output "$env:LOCALAPPDATA\WorkAssistant\diagnostics\transcript-draft.json"
```

`--sink box`も選択可能。出力の`properties.definition.actions`を設計上の叩き台として使用する。直接の登録APIを勝手に呼ばない。会社で許可されたブラウザ操作なら、デザイナーで同じ構成を作り接続・動的コンテンツを選ぶ。ソリューション/Dataverseの正式な利用権がある場合だけ、会社が認めたALM経路で定義を取り込む。非ソリューションの任意JSONをそのまま登録できるとは扱わない。

ドラフトには未検証の返却shapeと接続参照がある。Scopeの失敗分岐・空本文チェック・保存先固定・保持期間を補ってから自動実行を有効化する。ドラフトの手動フォームには保存先を持たせているが、自動フローでは保存先を固定値/管理者設定にし、AIの入力に任せない。

## 受け渡しの契約

- `inbox`: PA→PC。`outbox`: PC→PA。必ず別フォルダ。自分の会社アカウントと許可したフロー実行主体だけがアクセスできる場所を選ぶ。
- 原文DBは同期フォルダの外。受け渡しは1イベント1JSON。ファイル名はイベントUUIDだけ。個人名やメール件名は使わない。
- `event_id`: 配送単位のUUID。**同じIDを別本文で再利用しない**。内容更新・権限再確認の再配送は新IDを作る。
- `source_id`: 元情報の安定ID。例 `teams:<meeting-id>:<transcript-id>`、`mail:<mailbox-alias>:<message-id>`。複数部署/接続が衝突しない名前空間にする。予定は定期会議のシリーズIDだけでなく各回を区別する。
- `source_modified_at`: 原本更新時刻。原本にない場合、取得時刻を使いmetadataでそのことを明記する。未来時刻/タイムゾーン欠落は不合格。
- `observed_at`: **実際にアクセスして本文を確認した時刻**。ローカルの古いコピーを再配送するだけで更新しない。
- `expires_at`: 許可された有効期限。既定はobserved_at+24h。本体は設定されたleaseを上限にする。
- `metadata.coverage`: `message_body_without_attachments`、`selected_messages`、`search_snippet`等。検索結果の抜粋を全文として扱わない。
- `calendar`: metadata.start_at/end_atに必ずoffset付き時刻。繰り返し/終日/取消/夏時間の取り扱いを検証する。
- `transcript`: VTTを返す場合だけcontent_type=`text/vtt`。string返却か`$content`のbase64 wrapperか、実出力で確認してから正規化する。JSON wrapperを文字起こし本文として保存しない。
- `tombstone`: 確認できた削除/アクセス取消を、元と同じsource_id・新event_id・現在のobserved_atで配送。古い到着順の更新で復活させない。

本体はサイズ上限1MB、本文最大180000文字。これを超える会議を黙って切り詰めない。必要なら会議ID＋segment番号で分割し、metadataにsegment_index/segment_count/coverageを記録する。元情報が更新/削除されたら全segmentを更新/失効させる。この分割フローは会社側で検証・追加する。

### Composeでの代表的な式

| 用途 | 式 |
|---|---|
| 新しい配送ID | `guid()` |
| 取得時刻 | `utcNow()`を一度Composeし使い回す |
| 有効期限 | `addHours(outputs('Observed_at'),24)` |
| ファイル名 | `concat(outputs('Envelope')?['event_id'],'.json')` |
| ファイル本文 | `string(outputs('Envelope'))` |

JSON本文を`concat`で手組みしない。Composeのobjectとして作成してからstring化し、引用符・改行・日本語をJSONエスケープする。

## エラー・再試行・削除

Try/Catch Scopeを作る。403/401は成功扱いにせず、確認できた権限取消だけtombstone、その他は安全な`ACCESS_DENIED`コード。404は会議ID違いと削除を区別。429/一時的5xxは最大3回・間隔を空けた再試行。全文取得が空なら`EMPTY_CONTENT`で失敗。機密を含む生のエラー文字列をresponseに入れない。

F06の失敗応答は`response.schema.json`に従い、status=`error`、documents=`[]`、error_code=`ACCESS_DENIED`等。成功はstatus=`ok`、同じrequest_id/action、coverageを明示。未知/期限切れIDを実行しない。20件を超える結果はpartialとし、追加依頼または範囲の縮小にする。PA側もaction/引数/件数/日付範囲を検証する。

再試行は同じ配送ファイルの再利用が基本。別event_idで届いてもsource_id/更新時刻で正規化される。完全なexactly-onceを仮定しない。大量変更時の取りこぼしに備え、トリガーに加えて未処理依頼の定期再走査を行う。

同期ファイルとPower Automate実行履歴は別の原文コピー。ローカルDBの失効だけでは消えない。管理者と保持期間・実行履歴のSecure inputs/outputs・専用フォルダの整理方針を決める。本体はクラウド原本を削除しない。

## 権限・ライセンスの注意

Box/OneDrive/Teams/Outlook/SharePointの標準コネクタを想定しているが、会社の割当・DLP・接続認証が許可することは別条件。BoxコネクタのSSO制約が障壁ならOneDriveへ切り替え、SSOを解除しない。SharePoint専用HttpRequestは固定サイトの検索に限定し、汎用HTTP Premiumを混ぜない。

他人主催の会議は閲覧・保存・コネクタ取得・自動通知を別々に試験する。できない場合は明示的な共同主催/ダウンロード許可/主催者からの承認済み共有を使う。隠れたGraph呼び出しやブラウザCookieの取得で代替しない。

## 一次資料

- https://learn.microsoft.com/en-us/connectors/teams/
- https://learn.microsoft.com/en-us/connectors/office365/
- https://learn.microsoft.com/en-us/connectors/box/
- https://learn.microsoft.com/en-us/connectors/onedriveforbusiness/
- https://learn.microsoft.com/en-us/connectors/sharepointonline/
- https://learn.microsoft.com/en-us/power-automate/manage-flows-with-code
