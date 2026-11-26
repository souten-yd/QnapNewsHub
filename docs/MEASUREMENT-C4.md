# C4 表と構造化データ（初期実装）

単純な Markdown 表（model・GPU・明示の decode/prefill/total tok/s 列）と llama-bench JSON/CSV の pp/tg 単独行を LLM なしで解析する。
表の値と条件は行、単位は列ヘッダーの span。JSON/CSV は行/フィールド位置を返すが DB 保存しない。
未実装: HTML・結合セル・一般表・vLLM・共有設定・脚注・本文収集の表保持・既存Web Collector W4との統合。
ネットワーク・依存追加なし。既存テストに影響するランタイム接続なし。回帰試験は未実行。
