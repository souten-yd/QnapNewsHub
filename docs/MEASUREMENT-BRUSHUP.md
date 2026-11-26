# Cond 2.0 改善設計と修正（2026-11-26）

前回 #50 の合成18件（13件成功・5件失敗）を対象にする。調査:
- Pint の数量解析 https://pint.readthedocs.io/en/0.22/user/defining-quantities.html : 値と単位を別管理。QNAPの依存を増やさずDecimalと明示辞書を利用。
- llama-bench https://github.com/ggml-org/llama.cpp/blob/master/tools/llama-bench/README.md : backends、gpu_info、n_prompt/n_gen、avg_ts を別欄で持つ。engine/backendと測定指標を分ける。
- vLLM https://docs.vllm.ai/en/stable/benchmarking/dashboard/ : 出力スループット、TTFT、TPOT、同時数を区別。per-user/per-request/aggregateを混同しない。
- LangExtract https://github.com/google/langextract : 原文spanの整合を重視。ただしspanの存在だけでは関係の正しさを保証しない。外部LLM/依存は追加しない。

## 修正
- 共通の厳密なDecimal字句・指数・桁区切り・範囲認識。旧scalar APIは範囲/近似/上下限を保留、範囲の端を単一値にしない。新しいspan抽出も範囲を保留。interval_valueは上下限を返す内部部品。範囲のDB保存と検索は未実装（値列nullable等、旧互換と移行を別途検証）。
- backendを疎な追加欄として分離、Q4をQ4_K_Mと同一視しない。num3の構造化出力も更新。既存JSONの欄は維持。ただし旧engineにバックエンドが保存されている場合の移行・API/UI互換は未検証。実機での抽出率を確認してから有効化する。
- per-userとper-requestは別指標。aggregateとの混在は不明。JSON/CSV/Markdownとspanの既存経路も共通の数値字句を利用。
- 既存writer/DBスキーマ/通信/予算/起動/UIの追加変更なし。全体pytest未実施。前回18件の固定評価を回帰テストとして追加（今回の実行結果は未確認）。

## 残り
原文位置に基づく条件の関連付け、否定・比較・共有設定の誤結合、範囲/誤差/近似の永続モデル（stat/methodとの区別）、既存writer/APIの統合、旧記録の安全な再抽出。実データ300測定/200〜300問を使い、正解から独立した評価で採否。全PRはDraftのまま。

## 単独評価結果
`measure_quantity.py` のGit blob SHA `809c4a2d09c6288bb3b36e0b73eab35e5d4b46f0` と一致するコピーをPythonで実行。指数・負の指数・区間・負の区間4件、区間/近似/誤差/上限等の単一値除外8件、不正数値拒否5件、計17/17。#50 の18件全体と実モジュール連携の再実行は未確認。
