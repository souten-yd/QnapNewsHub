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

## C3c 原文との関連付けの保守的な検査（Draft）
- [LangExtract](https://github.com/google/langextract) の原文位置、[vLLM Benchmark CLI](https://docs.vllm.ai/en/latest/benchmarking/cli/) の測定指標分離を参考。原文位置の存在だけでは意味関係を保証しないので、別節・否定・比較・推定・選択肢は保留。単純規則のため正しい比較文も落とし得る。誤統合を先に抑える評価候補。
- C3 はセミコロンで節を分け、対象/数値/単位と重なる条件候補を除外。複数の異なる条件があれば条件全体を未確定にする。C3b は元の測定候補を再構成して、任意の候補IDから曖昧な条件を選べないようにする。保存時にも同じ候補を再構成し、別の文/表行や偽の指標/位置を拒否する。Markdownの同じ行は従来の表解析で検証。
- 旧 numeric はLLMの引用だけでなく元の文を検査。否定や別の測定を引用から切り落としても採用しない。完全な意味解析・正本取得権限の接続は未実装。既存claimの失効・再抽出はまだ行わない。
- 数値字句: 不正桁区切り、両端に単位がある範囲/誤差、和文の以上/以下/程度を単一値として扱わない。範囲/誤差のDB保存は別段階。

### 再評価（2026-11-26）
- #50 の固定18ケース: 13/18 → 18/18。bench.py blob `11e4624bb21e1ffaa811bebd9d1c242063667c5e` の対象純粋関数抜粋を Python で実行。旧18件の再現であって、実記事の正解率ではない。
- 新規26ケース: 26/26（原文抽出11、数量の拒否6、候補選択/ID/偽条件9）。
- 使用した単独モジュールコピーのGit blob SHA: measure_quantity `0065e56b28c591d1a8538554d042bb03a76996ab`、measure_relation `5ee85b6181b8e4fc8d2f36697bd52a6ed1ca1715`、measure_spans `d57a84f09505adad636d2245c8c336fc9657cd3d`、measure_selection `cc908856c92f972932cd34aaa07eeb2f7d47b379`。bench/measure_vocab は必要関数抜粋。完全なパッケージの import、numeric/DB 統合、既存テスト、実機・実LLM・原文の独立注釈は未確認。直接cloneはDNS解決失敗。既存writer/スケジューラ/公開API/権限/予算は変更なし。
- 次の関門: 誤結合P/R/F1・保留率（正しい測定を失う率）・文脈にあるが対象と関係ない条件、別の対象に属する条件、否定/推定/引用/脚注、正本hash/区分とDB保存・検索の統合。300測定以上の人手注釈と同一固定質問で採否決定。マージしない。

## C5b 正本・writer・条件検索の内部統合（Draft）
- measure_service.py: 既存NewsDocument/NewsItem/NewsBodyを使い、public/allowedでユーザー専用でない代表記事が1件のときだけ、保存本文を正本として取得。128KiB/20文書/10モデル/20測定の上限。既知のZstd frame size・raw_len・UTF8を照合。任意本文/hashをHTTPから受けない。本文や秘密をログに出さない。previewは件数/hashのみ、明示applyはhash再照合と休止時間確認、既存writer.run(news.measurement.confirmed)のtransactionで保存。スケジューラ/公開API/CLIに未接続、認可済みの内部呼び出し前提。既存claimは変更しない。
- measure_search.py: SQLで一致/不明/不一致、同区分内は一致済み条件数、id降順で上限前に順位付け。現行の本文hashをSQLで照合、区分/ai_useがEntity/Document/Measurementで一致する行のみ。既定public/allowed、local_onlyは認可済みの内部利用に限定（サービス自体はpublic限定）。入力上限や不正条件/hashを検証。全件検索ではなく最大20文書の明示候補内検索。
- measure_store.py: 同じfingerprintで失効済み/区分変更済みの記録を再利用しない。
- 再評価: measure_search.py blob `dacde88425a5c7abc7e44264d4d591ad70697363` と一致したコピーをSQLAlchemy/SQLiteで実行。最小ORM・条件辞書の代替を使用。1203合成候補で13検査（完全一致の古い候補、3状態順、一致済み条件数、hash、区分・local_only、入力拒否など）成功。初回は部分一致内で新しい無条件行が先になることを発見し修正。30回の単一条件・limit1はp50 3.875ms、p95 5.186ms（コンテナ内のメモリDB、実NASではない）。実DBクラス/全体pytest/Zstd・writer統合は未実行（clone DNS失敗、コンテナにzstandardなし）。test_measure_service.pyに実ORM/Zstdの回帰試験を追加。実機で要検証。
- 残: API/認可、既存FTSからの候補生成、旧claimとの互換、更新履歴/失効、範囲値、保存バッチ性能、実機writer/休止と競合、本文上限の見直し。自動適用はしない。マージしない。
