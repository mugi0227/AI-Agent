# デスクトップUIのデザイン方針

2026-10-21 / v0.3.2。対象は会社PC上の業務アプリ。既存の設定・質問・資料・継続タスクの操作を維持し、明るい配色と角丸、文字と余白の一貫性を整える。

## 利用場面と制約

- 日常業務で長時間使う、操作を中心とする画面（Operate）。機能名と具体的な説明を使用し、挨拶や宣伝文句を挟まない。
- Windowsのデスクトップ版。Web化やスマホ対応は追加しない。
- TkとPythonの標準ライブラリで描画する。新しいUIライブラリ、外部フォント、ネット接続を表示の前提にしない。
- 角丸の外側を描画し、内側には標準のButton・Entry・Text・Treeview・Notebookを使用する。日本語入力、文字選択、キーボード操作と部品の役割を保持する。

## 共通の見た目

| 用途 | 色 |
| --- | --- |
| 作業領域の背景 | `#f5f8f6` |
| パネル・入力欄 | `#ffffff` |
| サイドバー | `#edf3ef` |
| 本文 | `#1c342b` |
| 補足説明 | `#536b61` |
| 主操作・フォーカス | `#176b52` |
| 選択・補助的な面 | `#e7f3ed` |
| 境界 | `#dce6df` |

角丸は入力・ボタン10px、補助表示12px、表14px、パネル16px、会話入力18px。余白は8・12・16・24pxを中心に使う。フォントはYu Gothic UI / Noto Sans CJK JP / Meiryoなど、利用環境にある日本語フォントを優先する。見出しと本文は同じ書体で、サイズと太さを変える。

本文・補足・主ボタン・サイドバー・選択中の文字色は、それぞれの背景に対して4.5:1以上のコントラストを確認する。状態は色に加えて文言でも示す。角丸パネルに重ねてさらに同じパネルを並べる装飾は避ける。

## 操作と状態

- 主操作を塗りのボタン、補助操作を淡い面または枠線のボタンにする。通常・ホバー・押下・キーボードフォーカス・無効を区別する。
- 設定タブは標準Notebookの選択とキー操作を保ち、背景だけを角丸にする。
- 「送信」と「継続タスクに登録」は別の段に置き、小さいウィンドウでも操作できるようにする。
- 初期設定、資料0件、生成中、停止、エラー、再試行、出典表示も共通部品を使う。「設定済み」と実際の接続確認の成功を混同しない。
- 960×700、1240×860、拡大文字（Tk scaling=2.0）を確認する。ナビゲーションと保存・送信・取消を固定し、本文と一覧で高さを調整する。

## 確認と参照

会話・設定・実行管理を実Tkで描画し、文字の切れ、角丸の重なり、枠線、余白、選択とフォーカスを確認する。機能試験では初回設定から質問と出典確認、Enter/Tab、日本語入力、ボタンの端のクリック、無効時の実行抑止、最小サイズ・拡大文字を検証する。画面例は架空資料と模擬モデルを使用する。

設計指針は[Impeccable SKILL.md](https://github.com/pbakaus/impeccable/blob/dad8dbb3aac21ad1fa236c110315ef4abece23e8/.agents/skills/impeccable/SKILL.md)、[Operate](https://github.com/pbakaus/impeccable/blob/dad8dbb3aac21ad1fa236c110315ef4abece23e8/.agents/skills/impeccable/reference/operate.md)、[Craft floor](https://github.com/pbakaus/impeccable/blob/dad8dbb3aac21ad1fa236c110315ef4abece23e8/.agents/skills/impeccable/reference/craft-floor.md)、[Polish](https://github.com/pbakaus/impeccable/blob/dad8dbb3aac21ad1fa236c110315ef4abece23e8/.agents/skills/impeccable/reference/polish.md)を参照した。専用ランチャー・フックは導入せず、既存の設計資料と実画面を直接確認した。第三者のスコアや初見ユーザーの完了率を測定済みとは扱わない。
