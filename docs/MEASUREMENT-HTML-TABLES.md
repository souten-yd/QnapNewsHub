# C4b HTML表根拠の連携（Draft）

ControlDeck W4a2 #646 の `tables` + `tables_meta`（controldeck.html-table.v1）を処理する `measure_html_tables.parse_bundle`。依存や通信の追加なし。テーブル数・セル数・候補数を制限、完全な行列・重複/重なり・hashを照合。先頭th帯、明示されたモデル列と速度単位、列見出しと同じ行の条件だけを利用。rowspanの明示的な共有条件/対象は同じ元セル座標、colspanの数値は保留。曖昧条件、未対応単位、切り詰め、範囲/誤差は採用しない。

出力は測定候補。表・行・列の座標、列の見出し経路、対象・条件の座標、HTML hashと表hashを保持する。既存本文文字spanへ偽変換しない。hashは署名/認可ではない。認証済みControlDeck結果と現在の正本/権限を検査するサービスは後続。DB保存・API・キャッシュ・LLM未接続、脚注・任意headers/複雑rowgroup/CSS・vLLM等は後続。試験追加、全体pytest未実施。マージしない。

## 合成HTML接続試験（2026-11-26）
実際のControlDeck table_bundleからNewsHub parse_bundleへ渡して実行。双方のblob一致コピー: producer `7bb34d6ec06e460540f0953a83a0f6b4262a101c`、consumer `6e290bc904364fdfc43e0043cc64333fcdab192c`、quantity `0065e56b28c591d1a8538554d042bb03a76996ab`。2段見出し+rowspanの実HTMLで4測定、GPU・対象・値・見出し位置、共有rowspan、hash改変/重なり、非表示セル/子要素、行グループ跨ぎ、欠けたセル、明示headers、不明単位、曖昧条件、範囲/誤差/不正値を含む25検査成功。100回の小型表（取得なし）p50 1.421ms/p95 2.331ms。実Web取得・DB保存・全体pytest・本番認可経路・QNAP実機は未検証。#53のbase最新差分は統合レビュー時に反映。マージしない。
