# 測定の記録（Cond 2.0・Measurement）の計画と詳細設計（2026-11-26）

利用者「抽出機能の超強化とブラッシュアップ」。提示された改善案（Cond 2.0・Measurement Intelligence）を、main の実装と照らして確かめ、計画と詳細設計にまとめた。
目的は **数値・測定の対象・指標・条件・測り方・原文の根拠を、正しく結び付けたまま持つ** こと。条件を「たくさん付ける」ことではなく、**数値と条件の対応を正しく保つ** ことを成功の基準にする。
条件が記事に書かれていなければ「不明」が正解。付与率は目標にしない。

関連: ARCHITECTURE 12.1（条件つきの数値）・DESIGN 26（ウォッチと比較）・DESIGN 26.3（数値パス）・PLAN の v0.9 の行。

---

## 実装 PR の順序（2026-11-26）

各段階の差分を独立してレビューできる積み上げ式の Draft PR。後続 PR の base は直前のブランチ。
前段を検証・マージした後、後続を最新 main に合わせ、base を main に変更してからレビューする。
前段を squash/rebase でマージすると祖先関係が変わるため、差分に前段が再出現しないよう後続の rebase を確認する。

| 段 | PR | 実装の状態・詳細 |
|---|---|---|
| C1 | [#42](https://github.com/souten-yd/QnapNewsHub/pull/42) | 旧数値処理の修正案・検証待ち |
| C6a | [#43](https://github.com/souten-yd/QnapNewsHub/pull/43) | 原文注釈評価器。実データ正解未作成 ([詳細](MEASUREMENT-EVAL.md)) |
| C2 | [#44](https://github.com/souten-yd/QnapNewsHub/pull/44) | 3表と辞書。旧データ移行・衝突処理未完 ([詳細](MEASUREMENT-C2.md)) |
| C3 | [#45](https://github.com/souten-yd/QnapNewsHub/pull/45) | 単一測定の原文位置・追加保存。共有条件・writer接続未完 ([詳細](MEASUREMENT-C3.md)) |
| C4 | [#46](https://github.com/souten-yd/QnapNewsHub/pull/46) | Markdown・llama-bench 初期パーサー。HTML・vLLM・取得統合未完 ([詳細](MEASUREMENT-C4.md)) |
| C5 | [#47](https://github.com/souten-yd/QnapNewsHub/pull/47) | 内部検索。API・LLM context接続未完 ([詳細](MEASUREMENT-C5.md)) |
| C3b | [#48](https://github.com/souten-yd/QnapNewsHub/pull/48) | 候補番号のプロンプト・検証。LLM呼び出し・自動保存未接続 ([詳細](MEASUREMENT-C3B.md)) |
| C6b | [#49](https://github.com/souten-yd/QnapNewsHub/pull/49) | 読み取り専用再抽出プレビュー。適用・版管理未完 ([詳細](MEASUREMENT-C6B.md)) |

全段階は実装の土台であり、設計書の全受け入れ条件を満たした状態ではない。テスト・実機評価・R@10・マージは未実施。
C6a の独立正解作成、既存機能との結合、機密区分の実機試験、移行の再実行・復旧試験を完了するまで本番で新しい保存経路を有効化しない。

## C1 の実装状況（2026-11-26・レビュー待ち）

- 数値パスを `num2` に更新。数値だけの合流を廃止し、記事・対象・種類・主張文・値（符号を保持）・条件の一致でのみ重複を除く。既存の曖昧な主張は書き換えないため、同一測定の表現違いが残ることがある。原文位置による fingerprint は C2/C3 で対応する。
- 条件は数値と対象を含む同じ引用に限定。欄ごとの全候補を調べ、異なる候補・保存値と規則の衝突は不明とする。条件の明示的な別名以外は完全一致。価格から機材の条件を除く。離れた文・表ヘッダー・脚注・共通設定の取り込みは C3/C4。
- `metrics/compare` は既存のフィールドを維持し、速度の指標を分離する `metric_id` を追加。明示の prefill/decode/total throughput を区別し、複数・不明は unspecified。単位は引用中で値に隣接するものだけ採用するが、単位の次元・範囲・統計の全面的な正規化は C2。
- DB の移行・既存行の一括再抽出・追加 LLM 呼び出し・依存追加は行っていない。既存の誤った記録は残る。衝突の永続記録、span、独立した評価データ、C2〜C6b は未実装。
- 回帰試験を既存の `test_numeric_pass.py`・`test_watch_bench.py` に追加。実行環境に GitHub リポジトリを clone できないため pytest・全体試験・実機評価・R@10 は未確認。マージ前に集中試験→全体試験→build/E2E→実機→独立した物差しを確認する。

## 0. 改善前の実装の確かめ（main・2026-11-26）

| # | 問題 | 場所 | 確かめた内容 | 起きる誤り |
|---|---|---|---|---|
| P1 | 数値だけの一致で統合 | `numeric.py::apply_numeric` | 同じ記事の既存の主張と `numbers_in(value)` が同じなら、その主張に条件を足す | 同じ記事の「RTX 4090 で 60 tok/s」と「R9700 で 60 tok/s」が 1 つの主張になり、条件が混ざる |
| P2 | 条件の比較が部分一致 | `bench.py::_field_match` | `w == h or w in h or h in w` | 「RTX 4070」と「RTX 4070 Ti」、「Q4」と「Q4_K_M」が一致扱い |
| P3 | 規則は最初の 1 つだけ | `bench.py::parse_cond` | 欄ごとに `pat.search` の最初の一致 | 複数の GPU・量子化を並べた記事で、全部の数値に最初の GPU が付く |
| P4 | 条件の検査が候補の文全体 | `numeric.py::check_item` → `bench.clean_cond(…, source)` | 条件の値が「渡した文のどこか」にあれば通る | 別の行・別の測定の条件が、この数値の条件として確定する |
| P5 | 指標の区別が無い | `bench.COND_KEYS`（6 欄） | prefill・decode・並列の合計・単一ユーザーの区別が無い | 420 tok/s（prefill）と 60 tok/s（decode）が同じ組に並ぶ |
| P6 | 欄が少ない | 同上 | model・quant・hw・engine・ctx・version だけ | backend（ROCm/CUDA/Vulkan）・並列数・バッチ・オフロード・MTP などが落ちる |
| P7 | 候補の上限が固定 | `numeric.py` `MAX_WINDOWS=5`・`MAX_CHARS=1800` | 表の多いベンチマーク記事は途中で切れる | 取りこぼし |
| P8 | 表の行と列を持たない | `numeric.py::split_units` | 表の行を 1 文として扱う。列の見出しと結ばない | 「列の見出し＝条件」の表で条件が付かない |
| P9 | 条件の衝突を黙って上書き | `numeric.merge_cond` | 規則が先・LLM は検査が通れば優先 | 食い違いが記録に残らない |

既存の実測（PR #38・#39）: 条件つき比較の問い（12 問）の R@10 は 0.316 → 0.437。値のある主張のうち規則で条件が拾えたのは 4.3%（評価 DB 3,315 記事）。**問いが 12 問と少なく、正解も道具と同じ主張から作ったので、これ以上の判断には使えない**（C6 で作り直す）。

---

## 1. 参考にするリポジトリと採否

採否は「コードを取り込むか（依存）」と「設計を取り入れるか」を分けて決める。QNAP（TS-253be 級の CPU・RAM 少）で動くことが前提なので、**依存は原則足さない**。ライセンスは採用の前に原典で確かめ直す（下の表は公開資料からの確認）。

| リポジトリ | ライセンス | 取り入れる設計 | 依存として入れるか | 理由 |
|---|---|---|---|---|
| google/langextract | Apache-2.0 | **取り出した値ごとに原文の文字位置（span）を持ち、原文に揃える（完全一致 → 近い一致）**。長文の分割・複数回の取り出し | 入れない（設計だけ） | LLM の呼び出し口が別に要り、ハブの割り当て（quota）・ゲートウェイの仕組みと二重になる。揃え（alignment）は自前で小さく書ける |
| harperco/MeasEval | データは要確認 | **数量（Quantity）・測る物（MeasuredEntity）・測る性質（MeasuredProperty）・修飾（Qualifier）を別の注釈にして関係で結ぶ**データモデルと評価の仕方 | 入れない | 科学論文の英語のデータ。設計と評価の考え方を使う |
| ggml-org/llama.cpp（llama-bench） | MIT | **LLM 性能の条件の標準の欄**（model・size・params・backend・ngl・threads・n_batch・n_ubatch・type_k/v・fa・pp/tg の別・n_prompt/n_gen・avg/stddev） | 入れない | JSON・CSV・Markdown の出力の形を読む専用の取り込み（C4）を書く |
| vllm-project/vllm（benchmarks） | Apache-2.0 | **TTFT・TPOT・ITL・同時実行数・全体のスループットと 1 本の速さの区別** | 入れない | 指標の定義（metric_id）に使う |
| docling-project/docling | MIT | PDF・HTML・Office の構造（見出し・読む順・表のセル） | 今は入れない（ControlDeck 側の候補） | 重い（モデルを使う）。QNAP では動かさない。ControlDeck の W4 で評価 |
| hgrecco/pint | BSD-3 | **次元つきの単位の変換**（ms↔s・W↔kW・MHz↔GHz）と、次元の違う量を比べない | 今は入れない（評価して決める） | 純 Python だが辞書が大きい。必要な単位は限られるので、まず自前の小さな単位表（次元と倍率）。不足が見えたら入れる |
| microsoft/table-transformer | MIT | 画像・PDF の表のセル・行・列・見出しの認識 | 入れない（ControlDeck 側の候補） | GPU 向けのモデル。画像の表が必要になったときだけ |
| paperswithcode/axcell | Apache-2.0 | 論文の表から「モデル・データセット・指標・値」を結ぶ設計 | 入れない | 設計の参考（AxCell 自体の F1 は高くない） |
| mlcommons/inference | Apache-2.0 | **試験の場面（single stream・server・offline）・機材の構成・再現性の持ち方** | 入れない | 「測り方（scenario）」の欄の定義に使う |

---

## 2. データモデル（Measurement）

### 2.1 考え方
- **数値を中心にした 1 件の測定の記録** を独立した表に持つ。主張（claims）は「記事が述べた文」、測定（measurements）は「その中の 1 つの数値と、その条件と根拠」。
- 条件は **疎**（書かれている欄だけ）。書かれていない欄は行を作らない＝不明。推測で埋めない。
- **共通の測定の形** と **分野ごとの条件の定義**（LLM 性能・価格・電子部品の仕様・論文の測定値・株価や暗号資産など）を分ける。分野は定義の表を足すだけで増やせる。
- 既存の `claims.cond` と `/metrics/compare` は残す（互換）。測定の記録から `claims.cond` を作り直せる形にし、古い API の答えの形は変えない。

### 2.2 表（加算だけの移行。既存の表は消さない・書き換えない）

```
measurements
  id                 主キー
  doc_id             根拠の文書（news_items / documents）
  doc_hash           取り出したときの本文の hash（本文が変われば span は無効）
  claim_id           結び付く主張（あれば。互換のため）
  entity_id          測る物（Things。名寄せ済み）
  subject_text       原文の表記
  metric_id          指標の定義の id（2.4）
  metric_text        原文の表記（「生成速度」「tg128」など）
  value              数値（REAL）。範囲なら value_min・value_max も
  value_min, value_max
  value_text         原文の表記（「約 60」「60〜65」）
  unit               正規化した単位（2.5）
  unit_text          原文の表記
  stat               single・mean・median・max・min・p50・p95・p99・stddev・unknown
  method             measured（実測）・estimated（推定）・claimed（公称・メーカーの値）・unknown
  scenario           single_user・concurrent・offline・server・unknown（LLM 性能など。分野により null）
  measured_at        測った時期（書かれていれば）。無ければ null（記事の日付で代用しない）
  stated_at          記事の公開日
  sensitivity        区分（既存の scopes と同じ）
  span_start, span_end   本文の中の数値の位置（文字）
  block_id           表のセル・段落の識別（2.6）
  extractor          rule・table・json・llm と版（例 "llm:num2"・"table:t1"）
  confidence         exact（原文と完全に揃う）・aligned（近い一致）・none
  status             active・superseded（取り出し直しで置き換わった）・conflict
  fingerprint        重複の判定の鍵（2.7）
  created_at

measurement_conds          条件（疎）。1 測定に 0〜多
  measurement_id
  key                条件の欄（2.3。分野の定義にある key だけ）
  value_text         原文の表記
  value_norm         正規化した値（2.5 の名寄せ・数値の正規化）
  scope              explicit（同じ文・同じ表の行）・table_header（列や行の見出し）・shared（節・表題・脚注の共通の設定）・inferred（LLM が結んだ。原文の位置は必須）
  span_start, span_end   条件の原文の位置（必須。位置の無い条件は保存しない）
  extractor, confidence
  索引: (key, value_norm)・(measurement_id)

measurement_conflicts      黙って上書きしないための記録
  measurement_id, key, value_a, value_b, extractor_a, extractor_b, created_at

measure_domains            分野の定義（コードの中の辞書でもよい。表にするのは利用者が足すとき）
  domain（llm_perf・price・hw_spec・paper・finance …）, keys（欄と型）, metrics（指標の一覧）
```

### 2.3 分野ごとの条件（最初は llm_perf と price）

**llm_perf**（llama-bench・vLLM・MLPerf の欄に合わせる）

| key | 型 | 例 | 正規化 |
|---|---|---|---|
| model | 名前 | Qwen3.6-35B-A3B | Things の名寄せ（entity_id）＋表記 |
| model_params | 数＋単位 | 35B・A3B | 総パラメータ・有効パラメータを分ける |
| quant | 語 | Q4_K_M・IQ4_XS・FP8・AWQ・GPTQ-int4 | **語の辞書で完全一致**（Q4_K_M と Q4_K_S は別）。Unsloth の UD- は接頭辞として別の欄 `quant_variant` |
| hw | 名前 | R9700・RTX 4070 Ti Super | **機材の辞書で完全一致**（RTX 4070・4070 Ti・4070 Ti Super・4070 Super は別。別名は明示の辞書だけ） |
| hw_count | 整数 | 2 | |
| vram | 数＋単位 | 32GB | GB に揃える |
| cpu, cpu_threads | 名前・整数 | | |
| ram, mem_bw | 数＋単位 | | |
| engine | 語 | llama.cpp・vLLM・SGLang・Ollama・LM Studio・Strata・Radiance | 辞書 |
| backend | 語 | ROCm・CUDA・Vulkan・Metal・SYCL・CPU | 辞書 |
| engine_version | 語 | b12720・0.8.3 | |
| prompt_tokens, gen_tokens, ctx | 整数 | pp512・tg128・128K | K=1024 ではなく 1000 か原文に合わせる（原文の表記を value_text に残す） |
| concurrency, batch, ubatch | 整数 | 4 並列 | |
| offload | 語・整数 | ngl 99・CPU オフロード・SSD | |
| speculative | 語 | MTP・draft・EAGLE | |
| flash_attn, kv_type | 真偽・語 | fa=1・q8_0 | |
| power, power_limit | 数＋単位 | 300W | |

**price**: currency・region・edition（容量・色）・channel（店・セール）・tax（税込・税抜）・period（セール期間）。
価格の数値に「その製品の仕様」（チップ名など）は **条件として付けない**（仕様は物の属性。2026-11-26 の数値パスの実測で「iPad の価格に hw: A16」が付いた誤り）。

### 2.4 指標の定義（metric_id）

| metric_id | 意味 | 原文の手がかり（規則） |
|---|---|---|
| llm.decode_tps | 1 本の生成の速さ | tg128・generation・decode・生成速度・eval rate |
| llm.prefill_tps | 入力の読み込みの速さ | pp512・prompt processing・prefill・前処理 |
| llm.throughput_total_tps | 並列の合計の速さ | aggregate・total throughput・合計・N 並列で |
| llm.ttft_ms | 最初のトークンまで | TTFT・time to first token・初回応答 |
| llm.tpot_ms, llm.itl_ms | 1 トークンの間隔 | TPOT・ITL |
| llm.tps_unspecified | 上のどれか分からない tok/s | tok/s だけ |
| llm.score | ベンチマークの点 | MMLU・SWE-bench … （ベンチ名は条件 `benchmark` に） |
| price.amount | 値段 | $・円・USD |
| hw.memory_size など | 仕様 | |

`llm.tps_unspecified` は比較で「指標が不明」として分ける（decode とも prefill とも一致させない）。

### 2.5 正規化
- **単位**: 自前の単位表（次元と倍率）。時間（ns・µs・ms・s・min・h）・速さ（tok/s・tok/min）・大きさ（B・KB・MB・GB・TB、KiB 系は別）・電力（mW・W・kW）・周波数（MHz・GHz）・割合（%）・通貨（換算しない。通貨ごとに分ける）。次元の違う量は比べない。
- **名前の辞書**: 機材（GPU・CPU）・量子化・エンジン・backend の辞書を `measure_vocab.py` に持つ。比較は **正規化した値の完全一致**。別名は辞書に明示したものだけ（例 「Radeon AI PRO R9700」＝「R9700」）。辞書に無い値は、表記の正規化（大文字小文字・空白・ハイフン）だけして完全一致。部分一致は使わない。
- 辞書は利用者・LLM の提案で足せるが、足すのは明示の操作（自動で寄せない）。

### 2.6 文書の構造（ブロック）
本文を「見出しの階層・段落・リスト・表（行・列・見出し・結合セル）・キャプション・脚注」のブロックに分けて持つ（`doc_blocks`。文書の hash ごと）。
表は `table_id`・`row`・`col`・`header_row`・`header_col` を持つ。結合セル（rowspan・colspan）は展開して持つ。
本文の取り出しで表を落としている（trafilatura の `include_tables=False`）ので、表を残す取り出しに変える（ControlDeck の W4 と同じ部品を使い、二重に作らない）。

### 2.7 重複と統合の決まり（P1 の直し）
- `fingerprint = hash(doc_id, entity_id or subject_text, metric_id, value, unit, sorted(条件の key=value_norm), block_id or span_start)`。
- **同じ fingerprint だけを同じ測定とする**。数値が同じでも、対象・指標・単位・条件・位置のどれかが違えば別の測定。
- 取り出し直し（版が上がった）では、新しい記録を足し、古い記録を `superseded` にする（消さない）。
- 規則・表・LLM で同じ欄の値が食い違ったら、`measurement_conflicts` に残し、その欄は **確定させない**（比較では「不明」に入れる）。

---

## 3. 取り出しの流れ（詳細）

| 段 | やること | どこで | LLM |
|---|---|---|---|
| S1 構造を保った本文 | HTML → ブロック（見出し・段落・表・キャプション・脚注）。表を残す。JSON・CSV・Markdown の表はそのまま | QNAP | 不要 |
| S2 候補 | 数字＋単位・指標の語・物の名前・条件の語の位置を全部拾う（最初の 1 つではない）。適応の予算（3.3） | QNAP | 不要 |
| S3 構造の取り込み | llama-bench（JSON・CSV・Markdown）・vLLM の benchmark の出力・HTML や Markdown の表（列の見出しと行の見出しを条件に）。**ここで確定したものは LLM を通さない** | QNAP | 不要 |
| S4 結び付け（規則） | 3.2 の順で条件を測定に結ぶ。一意に決まらないものは「曖昧」 | QNAP | 不要 |
| S5 結び付け（LLM） | 曖昧なものだけ、測定ごとに「候補の条件（位置つき）から選ぶ」形で聞く（自由に書かせない）。選べなければ null | ControlDeck のゲートウェイか、既存の割り当ての中の LLM | 曖昧なときだけ |
| S6 原文の揃え | 数値・条件の各々の span を原文に揃える（完全一致 → 空白・全角半角を寄せた一致）。揃わないものは捨てる | QNAP | 不要 |
| S7 正規化と保存 | 単位・名前の辞書・fingerprint・衝突の記録 | QNAP | 不要 |

### 3.1 LLM に聞く形（S5）
- 1 回に数件の測定。各測定に「数値の位置と周りの文」と「その記事の中の条件の候補（key・値・位置の番号）」を渡し、**候補の番号を選ばせる**（値を書かせない）。選べないときは null。
- これで「原文に無い条件の生成」は形の上で起きない（番号に無い値は返せない）。
- 構造化出力（json_schema）を使う。断られたら普通の JSON（数値パスと同じ）。
- 利用枠: 既存の用途（prep・backfill）の割り当ての中。**新しい割り当てや夜の枠は足さない**。

### 3.2 規則による結び付けの順（S4）
1. 同じ表の行（行の見出し・同じ行のセル）と、その列の見出し → `explicit` / `table_header`
2. 同じ文の中で、数値と同じ節（読点・「、」・「with」「on」「using」で区切った範囲）にある条件 → `explicit`
3. 同じ文に同じ key の値が 1 つだけ → `explicit`
4. 節・表の題・キャプション・脚注の「共通の設定」（「すべて R9700・ROCm 7 で測定」など） → `shared`（記事全体でその key の値が 1 つだけのとき）
5. それ以外（同じ key の値が近くに複数・離れた場所にだけある）→ **曖昧**。S5 へ、または不明のまま

### 3.3 候補の予算（P7 の直し）
固定の 5 か所・1,800 文字をやめ、記事ごとに「数値の密度」で予算を決める（例: 規則・表で確定しなかった測定の数 × 周りの文、上限 4,000 文字・LLM 1 回分）。
表と JSON は S3 で LLM なしに処理するので、予算は自然文の曖昧な所だけに使う。取りこぼしと LLM の量を C6 の物差しで比べて上限を決める。

---

## 4. 検索と比較（C5）

- `/metrics/compare` の答えに、条件の欄ごとの一致の状態を足す: **exact（一致）・unknown（記事に書かれていない）・mismatch（違う）**。測定ごとの状態は「全部 exact → matching、mismatch が 1 つでも → other、それ以外 → unspecified」（今の 3 区分を保つ）。
- 指標（metric_id）と単位の次元が違うものは同じ組にしない。`llm.tps_unspecified` は decode・prefill のどちらとも別の組。
- 平均・合算は出さない（今のまま）。
- 問いから条件を読む: 「R9700 の Q4_K_M で単独の生成速度」→ hw=R9700・quant=Q4_K_M・metric=llm.decode_tps・concurrency=1。読み取りは規則（辞書）→ 足りなければ呼ぶ側の LLM が `cond=` で渡す（ハブ側で LLM を呼ばない）。
- LLM 用の context: 測定ごとに「値・単位・指標・条件（欄ごとに exact/不明/違う）・原文の引用・出典・日付」を短く。
- 索引: `measurement_conds(key, value_norm)` と `measurements(entity_id, metric_id)`。外部のベクトル DB・グラフ DB は使わない。

---

## 5. 評価（C6）

### 5.1 独立した物差し
- **抽出の正解データ**: 評価 DB の記事から、ベンチマーク・価格・仕様の記事を層別に選び、測定ごとに「対象・指標・値・単位・条件（位置つき）」を付ける。LLM で下書きし、**司令塔（人の代わりに Opus）が原文と照らして全件を確かめる**。抽出器の出力から作らない（道具に有利な測り方を避ける）。目標 300 測定以上。
- **検索の問い**: 200〜300 問。正解は上の正解データから作る。種類を必ず含める: GPU 名の類似（4070 と 4070 Ti）・量子化の違い（Q4_K_M と Q4_K_S）・同じ値の別測定・複数 GPU・prefill と decode・並列・表・脚注の共通の設定・単位の違い・価格・推定値と実測値・複数モデル・**条件が書かれていない（不明が正解）**。
- 置き場は評価の置き場（リポジトリに入れない）。作り方の道具（`tools/eval_measure`）はリポジトリに入れる。

### 5.2 指標と目標（暫定。達成済みではない）

| 項目 | 目標 |
|---|---|
| 数値の取り出し Precision / Recall | 0.95 / 0.90 |
| 条件の取り出し Precision | 0.95 |
| 数値と条件の結び付けの正確さ | 0.95 |
| 別の測定の誤った統合 | 0（重大なもの） |
| 原文に無い条件の生成 | 0 |
| 条件つきの検索 R@10 | まず 0.70、のち 0.85 |
| 通常の検索の R@10・nDCG | 下げない（前後で比べる） |
| 費用 | LLM の呼び出し数・時間・RAM を記録。既存の割り当てを超えない |

今の版と新しい版を、同じ DB のコピー・同じモデル・同じ設定で比べる。

---

## 6. 段階（PR の単位）

| 段 | 中身 | 合格の条件 |
|---|---|---|
| **C1 直し** | P1（数値だけの統合をやめる）・P2（部分一致をやめ、型ごとの正規化と明示の別名の辞書で完全一致）・P3（全部の一致を拾い、複数あれば確定しない）・P4（条件は数値と同じ文・同じ行のものだけ確定）・P5 の最小（prefill/decode/合計の語を指標として取る）・P9（衝突を上書きしない）。価格に仕様を条件として付けない | 各々の不具合を再現する試験が先に落ち、直して通る。既存の試験・物差しが下がらない |
| **C6a 物差し** | 5.1 の正解データと問い（C1 と並べて作る） | 正解の全件を原文と照らし済み |
| **C2 形** | 2.2 の表・2.3〜2.5 の定義・`measure_vocab.py`・既存の claims.cond からの移行（加算・やり直し可能・戻せる） | 移行の前後で `/metrics/compare` の答えが壊れない |
| **C3 結び付け** | S1〜S2・S4・S6（構造のブロック・全部の候補・規則の結び付け・原文の揃え）。適応の予算 | 結び付けの正確さ・原文に無い条件 0 を C6a で測る |
| **C4 表と構造** | S3（llama-bench・vLLM・HTML/Markdown の表・結合セル・列の見出し）。本文の取り出しで表を残す（ControlDeck W4 と部品を共有） | 表の記事で LLM を通さずに確定する割合 |
| **C5 検索** | 4 の比較・問いの条件の読み取り・context | 条件つきの R@10 0.70 以上・通常の検索を下げない |
| **C3b LLM の結び付け** | S5（候補の番号を選ばせる） | 曖昧なものの正解率・呼び出しの数 |
| **C6b 取り出し直し** | 版を上げた古い記事の取り出し直しを、夜の既存の枠の中で段階的に | 誤統合 0・既存のデータを消さない |

ControlDeck 側（ResearchDataHub・特許・論文の測定値）は、同じ形（measurements・measurement_conds・分野の定義）を使う。ハブを正本にし、ControlDeck は複製で分析する（統合の案 C）。

---

## 7. 守ること
- QNAP は軽い CPU の処理だけ。重い解析（PDF・画像の表・LLM）は ControlDeck のゲートウェイ。
- 機密は外へ送らない。区分（scopes）を守る（測定・条件の表にも sensitivity）。
- LLM の割り当て・夜の枠を増やさない。重いモデル・ライブラリを起動のときに自動で入れない。
- 画面の既定・権限・運用の決まりを変えない。
- 既存のデータを消さない・無条件に上書きしない。取り出し直しは版で管理し、段階的に。
- 既存の試験を先に広げる。新しい試験は目的と根拠を docstring に書く。
