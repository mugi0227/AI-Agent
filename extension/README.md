# Chrome / Edge 拡張の導入（Windows）

`extension/`はChrome・Edge共通のManifest V3拡張です。ブラウザ別のコピーは不要です。選択文または表示本文を確認・編集してローカル秘書へ渡します。AIへの質問はデスクトップアプリの会話画面、またはCLIの`ask`から入力します。

## 1. 本体を準備

Python 3.11以上で、リポジトリのルートから実行します。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-build-isolation -e .
.\.venv\Scripts\python.exe -m work_assistant init
```

本体のインストールは必須です。Native hostは分離モード（`python -I`）で起動するため、作業ディレクトリにソースがあるだけでは読み込めません。拡張やローカル実行ファイルの導入が会社の管理ポリシーで許可される場合に進めてください。

## 2. 拡張を読み込む

| ブラウザ | 管理画面 | Native hostの登録オプション |
|---|---|---|
| Chrome | `chrome://extensions` | `-Browser Chrome` |
| Edge | `edge://extensions` | `-Browser Edge` |

許可された環境では、デベロッパーモードを有効にして「パッケージ化されていない拡張機能を読み込む」から`extension/`を選択します。管理対象PCでは会社の配布手順を使用します。表示された32文字の拡張IDを控えます。ChromeとEdgeのIDは各画面で確認してください。

## 3. 設定とNative hostの登録

`%LOCALAPPDATA%\WorkAssistant\config.json`の`extension_ids`へ実際の拡張IDを追加し、`allowed_web_hosts`へ対象のホスト名を小文字・完全一致で追加します。両ブラウザを使う場合は両方のIDを設定します。ローカル保存の許可後に`local_data_approved`を`true`にします。その他の既存設定は保持してください。

Chromeで使う場合:

```powershell
.\scripts\register-native-host.ps1 -Browser Chrome -ExtensionId '<Chromeの32文字ID>' -PythonExe '.\.venv\Scripts\python.exe'
```

Edgeでも使う場合は、EdgeのIDを指定して別途登録します。

```powershell
.\scripts\register-native-host.ps1 -Browser Edge -ExtensionId '<Edgeの32文字ID>' -PythonExe '.\.venv\Scripts\python.exe'
```

`-Browser`の省略時は従来どおりEdgeです。保存先を変更した場合は、本体の`--home`と同じパスを`-DataHome`に指定します。

登録は現在の利用者のHKCUのみです。Chromeは`Software\Google\Chrome\NativeMessagingHosts`、Edgeは`Software\Microsoft\Edge\NativeMessagingHosts`へ登録します。実行ファイルと許可元manifestはそれぞれ`native/chrome/`・`native/edge/`に分けて保存され、もう一方の登録やIDを上書きしません。管理者権限やExecutionPolicyの変更は不要です。会社の署名・実行制御で拒否された場合は会社の承認済み配布手順を使います。

## 4. 接続と保存を確認

1. 拡張を開き、許可ホストを本体と同じ内容で保存します。ブラウザごとに設定が必要です。
2. 「接続確認」で`status: ok`を確認します。
3. 許可したHTTPSページの文章を選択し、「プレビューを作る」で内容を確認します。
4. 「この内容をローカル秘書に渡す」で保存します。保存だけではBedrockへ送信しません。
5. `python -m work_assistant gui`で本体を起動し、「資料・検索」で取り込みを確認します。会話の「資料を添える」で渡した資料を選び、「この資料を要約して」と入力してCtrl+Enterで送信します。AI応答には別途Bedrockの承認・接続設定が必要です。[会話画面の使い方](../docs/CHAT_UX.md)も参照してください。

接続できない場合は、ブラウザ選択、実際の拡張ID、`extension_ids`、Pythonの絶対パス、venvへのインストール、会社のNative Messagingポリシーを確認します。詳細は[会社PC検証手順](../docs/COMPANY_CODEX_PROMPT.md)に記載しています。

## 解除

```powershell
.\scripts\unregister-native-host.ps1 -Browser Chrome
# Edgeも解除する場合
.\scripts\unregister-native-host.ps1 -Browser Edge
```

指定したブラウザの登録だけを解除します。ローカル資料、設定、実行ファイル、もう一方のブラウザの登録は保持します。

## 検証の範囲

Windows CIでは登録、ブラウザ別manifest、C#→Pythonのバイナリ通信、日本語capture、未許可IDの拒否、片方のみの解除を試験します。ブラウザUIを操作する試験や、会社のChrome/Edge管理ポリシー下での接続確認は別途必要です。Chrome Web Store / Edge Add-onsへの公開は行っていません。
