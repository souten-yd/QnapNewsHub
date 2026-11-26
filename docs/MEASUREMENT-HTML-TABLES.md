# C4b HTML表根拠の連携（Draft）

ControlDeck W4a2 #646 の `tables` + `tables_meta`（controldeck.html-table.v1）を処理する `measure_html_tables.parse_bundle`。依存や通信の追加なし。テーブル数・セル数・候補数を制限、完全な行列・重複/重なり・hashを照合。先頭th帯、明示されたモデル列と速度単位、列見出しと同じ行の条件だけを利用。rowspanの明示的な共有条件/対象は同じ元セル座標、colspanの数値は保留。曖昧条件、未対応単位、切り詰め、範囲/誤差は採用しない。

出力は測定候補。表・行・列の座標、列の見出し経路、対象・条件の座標、HTML hashと表hashを保持する。既存本文文字spanへ偽変換しない。hashは署名/認可ではない。認証済みControlDeck結果と現在の正本/権限を検査するサービスは後続。DB保存・API・キャッシュ・LLM未接続、脚注・任意headers/複雑rowgroup/CSS・vLLM等は後続。試験追加、全体pytest未実施。マージしない。
