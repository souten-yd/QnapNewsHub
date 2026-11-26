# C6d 実公開データによる測定情報抽出の評価（Draft）

2026-11-26、合成ケースではなく公開資料で形式カバー率を試験。数値と明示条件だけを最小fixtureとして保持し、長い本文はリポジトリに転載しない。

## 出典・取得時の同定
- [llama.cpp公式README](https://github.com/ggml-org/llama.cpp/blob/master/tools/llama-bench/README.md): Git blob `3760072121150eab2c33e79a1a7be8b0fec71b57`、JSON/CSV出力例・prefilled context/Markdown例。JSON/CSVは評価対象fieldを投影、表の空白は正規化。別の実行の数値なので同一測定としない。
- [Framework 16 benchmarks](https://github.com/rmdes/framework16-local-llm/blob/main/docs/BENCHMARKS.md): blob `8ae3b7dfbff78e89b14b319db43562b7c4f55b15`、Model size sweep の実機ROCm/Vulkan表。脚注付きモデル表示を維持、別段落のGPU/量子化/commitを推測して付けない。
- [vLLM CLI](https://github.com/vllm-project/vllm/blob/main/docs/benchmarking/cli.md): blob `1e18de4cd0e17581fbf0832db56f806dbbd3da32`、Serving Benchmark Result の12指標。
- [vLLM Qwen2.5-VL Recipe](https://docs.vllm.ai/projects/recipes/en/stable/Qwen/Qwen2.5-VL.html#qwen25vl-72b-benchmark-on-visionarena-chat-dataset): 2026-11-26取得、72B VisionArena-Chatの12指標。ログの数値・ラベルを投影、元ログのモデル/機材は抽出器から見て不明。開発前に確認した追加データでblind holdoutではない。
- [MeasEval](https://github.com/harperco/MeasEval): eval/text と eval/tsv の4文書、SHAはeval_measeval.pyに固定。外部の人手注釈を使用。原文や注釈ファイルを本PRにコピーしない。

## 実行・結果
`cd backend && python -m tools.eval_measure.eval_real_benchmarks`
`cd backend && python -m tools.eval_measure.eval_measeval /path/to/MeasEval`

|形式|gold|旧構造化抽出|新候補抽出|新余分候補|p50/p95 ms (200回)|
|---|---:|---:|---:|---:|---:|
|JSON|2|2|2|0|0.0136/0.0157|
|CSV|2|2|2|0|0.0168/0.0181|
|native Markdown|6|0|6|0|0.0694/0.1304|
|backend matrix|12|0|12|0|0.0581/0.1098|
|vLLM log|12|0|12|0|0.0364/0.0570|
|vLLM another model|12|0|12|0|0.0370/0.0719|
|合計|46|4|46|0|—|

旧8.7%→新100%はこの選択済み投影fixtureの数値/指標/統計種別/単位/明示backend・pp/tg・表示subjectの完全一致。数値だけの一致ではない。標準偏差・depth・hashを別途検査。機器/quant等が不明な場合は不明のまま。vLLMのログにはsubjectを付けない。46個は別々の文書数ではない。選択した資料に対応するよう実装したため一般化性能・独立テストセットの精度ではない。

実際にPythonで実行したコピーのGit blob SHA: quantity `0065e56b28c591d1a8538554d042bb03a76996ab`、旧tables `49d57416f6d35a72cc246ce2264f8c37f5751914`、新formats `5f14609f98af14636f3c6fd04f3f246fb48648d5`。旧tablesのimport用span dataclass/単位辞書は最小代替（tok/sのみ）。再現評価スクリプトは同じ入力/手動goldを用いる。実行時間はコンテナでの短い投影データ処理でありWeb取得・DB・LLM・NAS実機を含まない。追加の改変入力（範囲/負値/不完全ログ/重複指標/根拠hash等）も検査。

### 独立注釈: MeasEvalの数値字句前処理
|文書ID末尾|数字候補|gold内の数字候補|gold数量|カバー数量|
|---|---:|---:|---:|---:|
|1610|8|3|2|2|
|990|15|6|6|5|
|1649|18|7|7|6|
|3127|5|4|5|4|
|合計|46|20|20|17|

4つの原文blob一致コピーと外部TSVから転記した正解位置、実scalar_tokensで評価。数値候補のgold内包含率43.5%、数量カバー率85%。gold数量が複数数字を含むため指標の分母が異なる。全数量の完全抽出・関係抽出のP/R/F1ではない。数字のみでは年号/図番号/軸番号を拾い、英単語の数、範囲、上下限を落とす。範囲を単一数値にしない現方針は維持。一般計量向けにはquantity span + unit + modifier + entity/property/qualifierの専用候補器と注釈評価が必要。学習・調整に使うデータと外部テストの分離が必要。

## 実装・制約
新規 measure_benchmark_formats.py はread-only候補器。native pp/tg, depth, mean/stddev、列固有backend、vLLM req/s/output/total/peak、TTFT/TPOT/ITLのmean/median/p99を分ける。元入力hash/行/列を保持し、文字spanやDBの既定経路へ偽変換しない。文書・取得元の正本/認可との接続、脚注/共有条件、全形式にわたる統計種別、範囲・誤差の永続保存は後続。検索のBEIR/SciFactとMIRACL-ja、PDF/Officeの実ファイル評価、ブロック差分抽出、実reranker統合はまだ未実施。直接clone DNS失敗のため全体pytestと実パッケージimportは未実施。マージしない。
