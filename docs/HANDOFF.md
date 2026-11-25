# 引き継ぎ（HANDOFF）

開発を途中から引き継ぐ人（人でも AI でも）が、最初に読む文書。**新しい段を終えるたびに、この文書の「今の状態」と「次にやること」を更新する**。

最終更新: 2026-11-25（LAN での発見の応答〔feat/lan-discovery〕・軽い翻訳〔feat/translate〕の後。その前はつながりの精度〔feat/link-precision〕・検索の評価の物差し〔feat/retrieval-eval〕）

## 1. 読む順番
1. `AGENTS.md`: 開発の決まり（ControlDeck と同じ。迷ったら ControlDeck の AGENTS.md を正とする）
2. この文書
3. `docs/ARCHITECTURE.md`: **設計の正**（v2・2026-11-24）。食い違ったらこちら
4. `docs/PLAN.md`: マイルストーン（v0.1〜v1.0）と状態
5. `docs/DESIGN.md`: 1 段からの実装の詳しい形（モジュール・認証・API の一覧〔2.5〕・画面〔2.7〕・各段で確かめたこと／確かめていないこと〔2.6・7 章〕）
6. 必要に応じて `docs/KNOWLEDGE.md`（知識化の最初の案。ARCHITECTURE に統合済み）・`docs/research-free-llm-batch.md`（無料 LLM の 1 回の件数の実測）

## 2. これは何か
- ずっと電源の入っている QNAP（または Linux の PC）で、ニュースを集める・ためる・検索する・集計する・知識に変えるサーバー。**データの正本はニュースハブ**。
- ControlDeck（GPU のある PC 上の個人 AI 基盤。https://github.com/souten-yd/ControlDeck ）は「重い分析の係」で、API でつなぐ（v0.3）。ControlDeck は電源を切ることがあるので、ニュースハブは単体で集め続ける。
- 文章の分析（概要・要素の取り出し・まとめ）は、**ニュースハブから OpenRouter の無料 LLM を積極的に呼ぶ**（ARCHITECTURE 5.1。QNAP 自身では LLM を動かさない）。
- 対象の機械: QNAP TS-253Be 相当（Intel Celeron J3455・x86_64・メモリ 16GB）。正確な型番は未確認（利用者の記憶は「TS-254BE」だが、その名の機種は見つからなかった）。QPKG は x86_64 向け。

## 3. 今の状態（2026-11-24）
| PR | 中身 |
|---|---|
| #1 | 0.5・1 段: ControlDeck のニュースの核を移植（取得・目録 472 件・取得先ごとの間隔と履歴・通信確認・一覧の巡回・フィードの生成・記事 DB・FTS5・画像・Google ニュース・チャンネル・好み）、OpenRouter の下ごしらえ（20 件ずつ。2026-11-24 に 30 → 20、理由は research-free-llm-batch.md の追記・上限・送り直し）、認証（管理者の session・読み取りの API トークン）、暗号化した鍵、`./hub.sh` |
| #2・#3・#4 | 設計: 知識化 → 設計 v2（ARCHITECTURE.md）→ 無料 LLM を QNAP 側の主力に・新着の傾向・診断 |
| #5 | 画面（React ＋ Vite。ControlDeck の 5 デザイン・まとめのバブル・取得先の管理・設定・ログイン）・PWA とアイコン（iOS・QPKG 用）・E2E |
| #6 | v0.1: Write Coordinator（まとめ書き 30 分）・お休みモード・診断・systemd のユーザーの単位の生成 |
| #9 | v0.1: QPKG（x86_64）`./hub.sh qpkg`・QTS の模擬での確かめ（`packaging/qpkg/test/run-sim.sh`）・LAN に待ち受けるときの最初の管理者の一回限りの番号（`NEWSHUB_SETUP_TOKEN=1`）。docs/QPKG.md |
| 更新（0.1.1） | 署名つきの Release（Ed25519）・更新の確認（1 日 1 回・ETag・メモリだけ）・QPKG の入れ替え（切り離した子で `sh <qpkg>`）・前の版への戻し・設定の更新の節・ホームの知らせ・診断。docs/UPDATE.md。`./hub.sh release <版>` が Release の台本（作るだけ。実際の Release は司令塔が作る） |
| 分析（第 1 版） | 集計の表（`analytics.py`・まとめ書きと同じトランザクションで増分・作り直し）・`/api/v1/analytics`（概要・推移・ホットトピック・取得先・比較）・画面「分析」（ECharts 5.6.0 を遅延読み込み）・ホームの上位 5。PLAN v0.4 の一部を前倒し。ARCHITECTURE 7・7.5・11 章の「実装と違った点」、DESIGN 9 章（速さの実測） |
| v0.1.1 | **Release 済み**（GitHub の Release、署名つき） |
| v0.2 前半（feat/llm-worker） | 無料の外部 LLM の係（予算 1 日 800 回・1 分 15 回・用途ごとの割り当て・夜の後回し・モデルの順番と切り替え）・**ControlDeck のゲートウェイ**（OpenAI 互換。先に使い、つながらなければ OpenRouter へ）・概要と要素の取り出しを 1 回の呼び出しで・物（entities）と fact（claims）の保存・`/api/v1/entities`・画面「物」・カードの札・`./hub.sh llm-try`。ARCHITECTURE 5.1 の「実装と違った点」・DESIGN 10 章 |
| v0.2 後半の一部（feat/events-trends） | イベントへの粗いまとめ（`events.py`・表 events／event_articles・まとめ書きで増分）・イベントの画面・ニュースの「ほか N 件」・イベントの要約（用途 event・既定 100）・新着の傾向（分析の節）と「今日の傾向」の解説（用途 trend）・物のホットトピック。ARCHITECTURE 4・7.5 の「実装と違った点」・DESIGN 11 章 |
| v0.3 ニュースハブ側（feat/link-controldeck） | **ControlDeck とつなぐ**: ペアリング（6 桁・10 分・総当たり制限・`link.py`）・scope=link のトークン（管理 API 不可。試験が全ルートを調べる）・封じた鍵の受け取り（ECIES・許可は openrouter だけ）・`GET /changes`（trigger の `change_log`・`changes.py`）・仕事の列（`jobs.py`。lease・冪等・期限切れは claim で列へ。作り手は claim のとき）・画像の受け取り（`artifacts.py`。形の検査・hash の重複・AI 生成の印）・調査の記録・つなぐ画面（`LinkCard.tsx`）・診断の「つないだ ControlDeck」。ControlDeck 側は別の PR。DESIGN 3 章・ARCHITECTURE 5 章の「実装と違った点」 |
| v0.2 後半（feat/search-storage） | **日本語の全文検索**（trigram＋英語 porter の 2 つの索引・BM25・短い語は LIKE・古い DB はバッチで作り直し）・**本文の取り出し**（Trafilatura・robots・Zstandard の BLOB・1 日 200 件・1 分 6 件・取れない記事は二度と試さない・下ごしらえは本文の頭 1,500 文字）・**保存の圧縮**（1 年より古い記事の抜粋を圧縮・索引から外す）・性能の実測（`backend/tools/bench_search.py`）・画面（「本文あり」の印・設定・診断）。ARCHITECTURE 8・9 章の「実装と違った点」・DESIGN 12 章 |
| v0.5 知識グラフ（feat/knowledge-graph） | **統合の土台として汎用に**: 表 documents・news_documents（記事の対応）・evidences・relationships・entity_notes・entity_cards（`kg.py`・`kg_view.py`・`kg_job.py`・`kg_router.py`）・機密の区分の引き継ぎ（既定は public だけ・confidential は管理者だけ）・LLM の型つきの関係（下ごしらえの同じ 1 回の呼び出しに relations。引用が原文に無ければ「推測」）・co_occurs の集計（夜・回数 3・比 2）・履歴と矛盾（読むときに計算）・知識カード（概要は夜に変化のあった物だけ・メモは別の表に即時保存）・知識の地図（Cytoscape.js 3.33.1・`POST /graph/neighbors`）・Obsidian の書き出し（zip）。試験 351 件・性能 P95 216 ms 以下（10 万件の模擬）。ARCHITECTURE 6 章・DESIGN 13 章 |
| v0.6 検索の道具の API（feat/retrieval-api） | **ControlDeck の MPKR が使う道具**（`retrieval.py`・`retrieval_router.py`・`rcache.py`）: `POST /retrieve/seeds`（全文 OR・物・イベント・ニュース以外の文書）・`POST /graph/neighbors` の拡張（向き・時系列・矛盾だけ・ハブの抑制・次数）・`POST /graph/ppr`（局所 push 法）・`POST /retrieve/evidence`・`GET /research?topic=`・**区分の強制**（link のトークンは、つないだときの設定 `link.allow_confidential` の範囲だけ。既定 public）・版つきメモリキャッシュ・時間と大きさの上限・datetime の警告 0。ARCHITECTURE 8 章（v0.6）・DESIGN 15 |
| v0.7 前半（feat/connectors） | **収集の connector の枠組み**（`connectors.py`・種類 feed／api／historical／change／discovery／market・取得の間隔・1 日の件数と通信量の予算・鍵〔任意〕・利用条件の記録〔`connector_terms.py`。未確認は既定で切〕・取得先の管理の「connector」の節）・**保存の段 L0〜L4 と昇格**（`docstore.py`。documents.tier・document_promotions・昇格の規則）・論文の同定（document_aliases）・document_links・Metric の時系列（metric_points）・L0 の検索（doc_fts）・connector: GitHub の発見とスターの急増・HF の新しいモデルと派生版・OpenAlex・Crossref・Semantic Scholar・HN 検索・Sitemap・変化の検出（既存の RSS・Release・arXiv・HN・HF は載せ替え）。試験 376 件。ARCHITECTURE 10 章の続き・DESIGN 14 章 |
| Things の脳（feat/things-brain） | 画面の「物」を **Things** へ（API の道は互換）・**カードの帯**（定義・状態・確かさ〔根拠の数だけで決める〕・根拠の数と新しさ）・**関係を型ごとに**（上位・下位・仲間・使う・使われる・置き換え・矛盾）・夜のカードの LLM に**定義・状態・未解決の問い・反対の見方**（材料に無い数字・英字の名前を捨てる）・主な数値・**つなぐ候補**（リンクされていない言及。押すと確定）・**まとまり（Louvain）と MOC**（`/moc`）・**全体の地図**（最初に主要 1,500 点・検索は強調／絞る・ローカル深さ 1〜3・孤立した点の切り替え）・**LLM の脳の API**（`/things/{id}/context`・`POST /things/context`・`/things/lookup`）・Obsidian の書き出し（frontmatter・Connections・Open Questions・`_MOC/`・README）。`things.py`・`things_router.py`・`features/knowledge/mapLayout.ts`・`pages/Moc.tsx`。試験 11 件・性能 P95 73ms 以下（10 万件の模擬）。ARCHITECTURE 6 章の続き・DESIGN 16 |
| 検索の評価の物差し（feat/retrieval-eval） | **統合の段 I0 の前倒し**（`backend/tools/eval_retrieval/`・`./hub.sh eval-retrieval --data <評価用 DB のコピー> --questions <json> --out <md> [--build]`）: 評価のハブの DB から**規則と雛形だけで**（LLM なし）問いを作る（事実・比較・変化・複数の文書・関係・日本語の問い→英語の記事の 6 種類。各問いに文書・物・主張・関係・イベントの正解）。5 つの道（①全文 ②種 ③種＋周り ④種＋局所 PPR ⑤物の文脈）を既存 API で流し、Recall@5/10/20・MRR・nDCG@10・**文脈の密度**（1,000 トークンあたりの正解の根拠）・時間 P50/P95 を種類ごとに出す。**問いと結果は実データ由来なのでリポジトリに入れない**（非公開の評価の置き場）。DESIGN 18 章 |
| つながりの精度（feat/link-precision） | **つなぐ候補の雑音を減らした**（一般語・長い名前の一部・大文字小文字・媒体名・種類／短い語。実データ 1,089 → 193 件・無作為 30 件の誤り 14 → 4）・**名寄せ**（規則で確かな組は夜に自動、迷う組は候補で管理者が確定。記事・主張・関係・別名・集計を付け替え、`entity_merges` の記録で完全に戻せる）・**まとまりの名前**（番号でなく「代表・特徴の語」）・**全体の地図の名前の出し方**（上位 24 点だけ・拡大するほど出す）。`entity_merge.py`・`things.py`・`pages/Entities.tsx`。試験 9 件。ARCHITECTURE 6 章の続き・DESIGN 18 |
| 検索の改善（feat/retrieval-density） | 物差しの基準値の弱い所 3 点: ①文脈の密度（text の context は URL でなく「出どころ 日付 d<id>」・evidence に `label`・`compact`）②**主張を種に**（`claim_fts_*` の全文索引・`/retrieve/seeds` が `kind: claim`）③**周りの点数**（`/graph/neighbors` に `seed_scores`・`q` → 辺・物の `score`。必ず元の種より低い）。③ R@10 0.634→0.762・主張 R@20 0→0.85・⑤ 文脈 1068→752 トークン。下がった所（② 複数の文書など）と理由は評価の置き場。ARCHITECTURE 8 章の続き・DESIGN 17.1 |
| 軽い翻訳（feat/translate） | **QuickMT 英→日（CTranslate2 int8）**（`translate.py`・`translate_flow.py`・`translate_router.py`・表 `news_translation`・`news_tr_cache`）: エンジンの枠・モデルを利用者の操作で取得（設定の「翻訳」・版と sha256 固定）・用語の保護（印 `ZQnZ`）と対訳表・訳の検査（落ちた文は原文のまま〔未訳〕）・文ごとのキャッシュ・全文検索の索引に訳を足す（「概要（日本語）」の列）・LLM が使えないときは題と本文だけ・重要な記事の本文は空き時間に 1 日の上限まで・開いたとき訳す API・`translate_hq` の仕事（実行は ControlDeck 側）・画面の「機械翻訳」の印と原文・診断の節。ARCHITECTURE 8.5・DESIGN 19。**物差しでは点数は変わらない**（飽和。評価の置き場の md）。依存は ctranslate2 4.6.2・sentencepiece 0.2.0（manylinux2014 の最後の版。上げない） |
| #8 | v0.1: 取得のエンジン（ETag/Last-Modified・Adaptive Polling とバックオフ・URL の正規化と SimHash の近い重複・CSV/OPML・出どころの種類）。実走で 5 件中 4 件が 2 回目に 304 |
| 銀河の地図（feat/galaxy-map） | 重力の地図の見た目を Obsidian・Nebula 風に作り直した（丸 2〜9・放射状の葉・色はまとまり・光・設定の枠・型の保存）。ARCHITECTURE 6 章の続き・DESIGN 20 |
| 重力の地図（feat/gravity-map） | **丸の大きさ＝重要度（検索で当たった回数・カードを開いた回数・直近の記事・関係の中心性）・付随する点は大きな点の周りに**。夜に ForceAtlas2（依存なしの Python・Barnes-Hut）で配置して保存（`mapgrav.py`・`map_layout`・版）。数えはメモリ→まとめ書き（`entity_usage_daily`）。画面は sigma.js＋graphology（Cytoscape は外した）。保存が無いときは従来の塊（フォールバック）。計算 1,500 点 約 4 秒・5,000 点 約 13 秒（開発の PC）。**QNAP 実機の計算時間は未測定**。記録は ARCHITECTURE 6 章の続き・DESIGN 19 |
| 発見の応答（feat/lan-discovery） | LAN に出しているときだけ UDP 8788 で ControlDeck の「探す」に答える（名前・版・URL の候補・公開鍵の指紋。秘密なし・1 秒あたりの上限つき）。設定の「待ち受け」で切／ポート変更・「つなぐ」の画面に LAN の URL を表示。DESIGN 3.5。落とし穴: QTS のファイアウォールが UDP を止めると見つからない（URL を手で入れれば従来どおり） |

- 試験: backend 376 件（v0.7 前半の時点）・E2E 223 件（v0.5 で 211 件・v0.7 前半で `connectors.spec.ts` 12 件を足した）。QPKG は `packaging/qpkg/test/run-sim.sh` で合格。
- 実測（開発の PC）: 待機中の書き込み 0 バイト／10 分、お休み中も 0、常駐 114〜190MB、待機 CPU 0.1〜0.4%。
- **作業中**: なし（#9 の時点）。
- まだ無いもの: **本物の ControlDeck とのつなぎ（ControlDeck 側の PR のあと）**・QNAP 実機での確認・長時間の連続運転・QNAP 実機・**本物の LLM での取り出しの質の評価**・本物の LLM でのイベントの要約・解説の質・イベントのしきい値の実データでの調整・一緒に出る分析（v0.5 で集計は済み）・知識グラフ（v0.5 で作った。実データ・本物の LLM での質は未）（分析の画面の「知識の地図」「物のノート」は空き枠だけ）。
- 既知の小さなずれ: 古い設定ファイルに保存済みの 1 日 300 回は、そのまま残る（既定の 800 回は新しい設定だけ）。まとめ・一言要約はモデルの順番に乗っていない（固定のモデル）。試験で StarletteDeprecationWarning が 1 件出る（動作に影響なし）。

## 4. 次にやること（順番）
**利用者の決定（2026-11-24）: QNAP の QPKG への変換は、計画したもの（v1.0 まで）をすべて Linux で完成させてから。それまで Linux（この PC の systemd のユーザーのサービス）で評価する。**

1. 開発の PC で動かす（利用者「24 時間も待たなくて良い」〔2026-11-24〕。長時間の運転は並行して、結果を待たずに次へ進む）（`./hub.sh install-service` か `./hub.sh run`、目録から 50〜100 件を有効に）。取得の実績・失敗・書き込みの量・メモリを診断の画面で見る。
2. QPKG を利用者の QNAP に入れてもらう（`./hub.sh qpkg` の .qpkg を渡す。手順は docs/QPKG.md。型番と CPU も QTS の画面で確認してもらう）。動いたら v0.1 を Release（.qpkg を Release の添付に）。
3. （済）分析の第 1 版（集計の表・API・画面）。続き: 実データを 1 週間ためてホットトピックの上位が一般語にならないか見る（ストップワードの調整）・集計の表の作り直しが長い（100 万件で約 34 秒）ので、必要なら分割する。
4. v0.2 の後半: （前半の無料 LLM の係・要素の取り出し・物の画面は済。**検索（trigram＋英語）・本文の取り出し（Trafilatura）・zstd・保存の圧縮・性能の実測は済〔10 万件・100 万件の温まった P95 は 1 秒以内。DESIGN 12〕**〔feat/search-storage〕）残り: **本物のゲートウェイ・OpenRouter での取り出しの評価**（`./hub.sh llm-try`。20 件のままでよいか・名寄せの精度）・粗いイベント・新着の傾向（7.5）。
5. （済）v0.1.1 は Release 済み。残り: 実機の QNAP で「古い版 → 更新」を確かめる（利用者の QNAP は 0.1.0 を手で入れた場合、更新の機能が無いので、0.1.1 だけは手で入れ直す）。
6. v0.3 の続き: **ControlDeck 側**（ControlDeck のリポジトリ・その決まりの関門と PR）。つなぐ画面・`/changes` の取り込み・仕事を借りる係（埋め込み・要約・取り出し・画像）・鍵を封じて渡す（`link.seal()` が見本）・画像を PUT する。つないだら本物同士で「止めていた間の記事が後でまとめて分析される」を確かめる。
7. （済）v0.5 知識グラフ（feat/knowledge-graph・2026-11-25）。残り: **本物の LLM で 20 件を 1 回流し、relations を足した出力が上限に収まるか（finish_reason）・引用が原文に出る率・推測の割合を見る**／実データで co_occurs のしきい値と、物の別名寄せの要否を見る／カードの概要の質。続きは v0.6（検索の統合: 局所 PageRank・`/research`。neighbors は済み）。
8. （済）v0.7 前半（feat/connectors・2026-11-25）。残り: **実データで昇格の規則（既定のチャンネルの語は広い）と、1 日の予算の既定を調整**／本物の Semantic Scholar・Crossref・Sitemap・変化の検出を 1 回ずつ確かめる／HF の `filter=base_model:…` の効き／L2 に上がった非ニュースの本文の取得／診断・画面に文書（論文・リポジトリ・モデル）の見方を足す。v0.8 は e-Stat・日銀・国会会議録・e-Gov・EDINET・DefiLlama・CoinGecko・GDELT・Wikidata・PyPI／npm・OSV／NVD（ControlDeck の PLAN-integrated-knowledge 8.5）。
9. v0.6 の続き: **ControlDeck 側の MPKR（I4）が `/retrieve/seeds`・`/graph/neighbors`・`/graph/ppr`・`/retrieve/evidence`・`GET /research` を呼ぶ**（トークンは `Authorization: Bearer`・POST も通る・区分は `confidential: true` を付けたときだけ。機密を読ませるには、つなぐ前に設定 `link.allow_confidential` を true にする）。本物のデータでの評価（I0 の問い）・hub_k と ε の既定の調整。
10. v0.4・v0.6 以降は PLAN.md の順。**検索を変えるとき（MPKR・翻訳・名寄せ・意味の検索）は、前後で `./hub.sh eval-retrieval` を同じ問いで流して点数を比べる**（問いの JSON は `--build` で作り直さず、基準値と同じものを使う。DESIGN 18 章）。
11. （済）Things の脳（feat/things-brain・2026-11-25）。残り: **本物の LLM でカードの定義・状態・問い・反対の見方を夜に回し、検査の落ちる率と質を見る**／（済: つなぐ候補の雑音・名寄せ・まとまりの名前〔feat/link-precision〕。残り: 日英の名寄せの確かさ・本物の LLM で候補の質を見る）／スマホでの全体の地図の操作／**ControlDeck の MPKR・MCP が `/things/{id}/context`・`/things/lookup` を呼ぶ**（トークンは `Authorization: Bearer`。区分は既存の範囲）。


13. （済）重力の地図（feat/gravity-map・2026-11-25）。残り: **QNAP 実機で `./hub.sh map-layout` の時間を測る**・重要度の重みを使われ方がたまってから見直す（設定 `map.weights`）・実機の指での sigma の操作。
14. （済）銀河の地図（feat/galaxy-map・2026-11-25）。丸 2〜9（平方根・上位 5% で頭打ち）・弱い重力/強い反発/線形引力＋円盤化・葉は親の周りに放射状・線は 1 色の灰青（背景の canvas に半透明。sigma は透明度を無視する）・色はまとまりごと（設定で種類ごと）・光・左上の設定の枠（型の保存）。記録は ARCHITECTURE 6 章の続き・DESIGN 20。残り: **実機の iPhone・GPU のある端末で滑らかさ**・データが増えたときの放射の見え方・まとまり（Louvain）が夜に計算されていること（評価の DB では空だった）・力のスライダーと夜の配置の値の対応。
12. 軽い翻訳の続き: **QNAP 実機（AVX 無し）で QuickMT の速さ・メモリを測る**（見込みは実測ではない）・ControlDeck 側で `translate_hq`（高品質の訳）の係を作る・LLM の日本語の概要が無い記事から問いを作って翻訳の効果を点数で測る・題をまとめて訳して速くする。

## 5. 開発の仕方
```
./hub.sh setup                 # .venv と依存
./hub.sh test                  # backend の試験（pytest）
./hub.sh build                 # 画面の build（frontend/dist。サーバーの起動時には build しない）
./hub.sh e2e                   # Playwright。一時の置き場とポート 18787 でバックエンドを起こして流す
./hub.sh icons                 # アイコンの PNG を SVG から作り直す
./hub.sh create-admin          # 最初の管理者
./hub.sh run                   # 127.0.0.1:8787（データは NEWSHUB_DATA、既定 ~/.local/share/qnapnewshub）
./hub.sh print-service|install-service|uninstall-service   # systemd のユーザーの単位
```
- **関門**（マージの前に必ず自分で流す）: `./hub.sh test`・`./hub.sh build`・`./hub.sh e2e` がすべて合格。最後の行の「passed」を見てから進む（`| tail && merge` のように不合格でも進む書き方をしない）。画面を変えたら撮影を見る（320・390・1280、ライト・ダーク）。
- 流れ: 作業ブランチ → コミット（日本語・末尾に Co-Authored-By 等）→ PR（日本語。内容・検証・未確認）→ squash でマージ → ブランチを消す。マイルストーンの区切りで Release（日本語のノート）。
- 子エージェントに任せるときは、目的・触ってよい所・守ること・完了の条件・「報告は日本語で」を書き、報告を信じずに差分と関門を自分で確かめる。
- 新しい試験には docstring に「目的」と「根拠」を書く。不具合を直したら、それを検知する試験か診断も足す。
- docs は実装と一緒に直す: ARCHITECTURE の該当章に「実装と違った点」、DESIGN に「確かめたこと・確かめていないこと」、PLAN の状態、**この HANDOFF**。

## 6. 落とし穴（実際に踏んだもの）
- **Playwright のブラウザ**: ControlDeck の `data/cache/ms-playwright` を ControlDeck の E2E と共有している（`PLAYWRIGHT_BROWSERS_PATH` で指定）。**`playwright install` をしない**（別の版を入れると他の版のブラウザが消える）。`@playwright/test` は ControlDeck の frontend と同じ 1.61.x。
- **残ったサーバー**: この開発の環境では SIGTERM が塞がれて子に引き継がれることがあり、`kill` で止まらない試験のサーバーが何時間も残った。起こしたら必ず止めたことを `pgrep -af qnapnewshub.cli` で確かめ、止まらなければ SIGKILL。一時の置き場（`.data/` など）も消す。
- **書き込み**: 永続化は必ず `writer.py`（Write Coordinator）を通す。直接 INSERT/UPDATE を足すと、まとめ書き・お休み・診断が崩れる。試験で保存を待つときは `writer.flush("test")`。
- **未保存**: バッファにある記事は `persisted:false`。ControlDeck への差分は保存済みだけ（`store.changes_persisted`）。
- **E2E の模擬**: `frontend/e2e/mock.ts` の値（お休みの終わり 06:00 など）は固定の見本。撮影の数字は実データではない。
- **claims.article_id は NULL 可になった（v0.5）**: 古いファイルは開いたとき 1 回だけ表が作り直される（`db._rebuild_claims`）。`Claim` を記事と inner join するコードは、記事の無い文書の主張を落とす（物のページの facts はそれで良い＝公開のニュースだけ）。新しく claims を読むコードは `sensitivity` で絞ること。
- **派生データの区分（v0.5）**: 主張・関係・物・カードを新しく作る経路は、必ず `kg.inherit` で文書から区分を引き継ぐ。読む API は `scopes_for`（confidential は session だけ）を通す。LLM に送る材料は public かつ allowed だけ（`kg_job.targets`）。ここを緩めない。
- **LLM の出力の上限**: `prep.TOKENS_PER_ITEM`（600）× 件数。relations を足すと 20 件は上限ぎりぎり。件数を増やす・出力の項目を足すときは、DESIGN 13 章の見積もりをやり直す（30 件は 2026-11-24 に切れた）。
- **Things の脳（2026-11-25）**: 画面の名前は Things、データ・API は entity のまま（`/entities`・`/kg`・`/things` の 3 つの道が同じ中身）。カードの帯の確かさは根拠の数だけ（`things.confidence_of`）で、LLM の値を混ぜない。カードの LLM の検査（`kg_job._grounded_line`）を緩めない（材料に無い英字の名前・数字を捨てる）。夜の集計（`kg_job.aggregate`）がまとまり（`things.compute_communities`）と候補（`compute_mentions`）も作る＝検索の索引が無いと候補は 0。まとまりは public の関係だけで計算（機密の物は入れない）。まとまりの id は夜ごとに変わる（覚えない）。全体の地図の応答は 1MB 未満に保つ（`OVERVIEW_EDGES_MAX`。超えるとキャッシュされない）。`/things/context` は `read_user` なので POST でもトークンが通る（副作用を足さない）。
- **つながりの精度（2026-11-25）**: 名寄せは `entity_merge.py`。**Entity を直接消さない・付け替えない**（`entity_merge.merge`／`undo` だけ。関係・主張・結び付き・集計が一緒に動く）。新しい表（entity に結ぶ表）を足すときは `merge`／`undo` に付け替えを足す（足さないと寄せたとき外部キーの CASCADE で消える）。自動の寄せは規則の鍵（`rule_key`）が同じで種類が合うものだけ＝ゆるめない（括弧を除く・日英は候補止まり）。`Entity` の作り方は `entities._get_or_create` だけ（寄せた表記を勝った物へ向ける `resolve_key` が入っている）。つなぐ候補の絞りは `things.compute_mentions` の docstring の順（一般語・長い名前・大文字小文字…）。試験の実データ比較は評価のハブの DB を**読むだけでコピー**して一時の置き場で行う（コピーは消す）。
- **co_occurs の行の id は夜ごとに変わる**（置き換え）。id を覚えておく使い方をしない（物の id と型で引く）。
- **作業の場所**: 子エージェントが `QnapNewsHub` の checkout で作業している間に docs を直すときは、`git worktree add ../QnapNewsHub-xxx origin/main` で別の場所を作り、終わったら `git worktree remove` する。

- **子エージェントの利用の上限**: 上限で途中で止まると、コミットされていない変更が作業の場所に残る。引き継ぐときは `git status` と `./hub.sh test` で状態を見てから続ける。

- **QPKG**: QDK の QPKG はインストールの成功で終了コード **10** を返す（0 ではない）。QDK はシステム全体に入れる作りなので、手元では podman のコンテナの中で使う（build.sh）。依存の wheel は manylinux2014 だけ（QTS の glibc が古い）。依存を足したら `packaging/qpkg/requirements.lock` も直し、`run-sim.sh` を流す。

- **更新の落とし穴**: 更新の試験用の差し替え（`NEWSHUB_UPDATE_TEST_FEED`）は `NEWSHUB_INSTALL=qpkg-sim` のときだけ効く（本番の qpkg では無視する）。署名の秘密鍵は `~/.keys/qnapnewshub-release.pem`（`NEWSHUB_SIGNING_KEY`）。読むだけ・中身を出さない・コミットしない。版を上げるときは `__init__.py`・`pyproject.toml`・`frontend/package.json`・`package-lock.json` の 4 か所（`./hub.sh release` が確かめる。`qpkg.cfg` の版は build が書く）。

- **要素の取り出し・LLM の係**:
  - 試験で LLM を模擬するときは `llm._post`（OpenRouter）・`llm._post_gateway`（ゲートウェイ）を差し替え、待ちは `llm._sleep`、モデルの時計は `llm_models._clock`。`llm._sleep` だけ差し替えて 1 分の窓が詰まると、時計が進まず空回りする（`llm.reset_minute_window()`）。
  - 割り当てを書かない設定は、既定の割合を上限に合わせて縮める。割り当ての合計が上限を超える保存は 422。
  - 取り出しは必ず `writer.get().stage_extraction`（まとめ書きに乗る）。DB へ直接 Entity を入れない。
  - ゲートウェイの URL は設定の値だけ。LAN の私的な IP を許すので、**記事の中の URL・取得先の URL から決して呼ばない**。診断の束には URL を出さない。
  - 古い DB の列・表は `db._LATE_COLUMNS`・`create_all` で足す（`news_items.extract_v`・entities 系の 4 表）。

- **ControlDeck との連携（v0.3 のニュースハブ側）**:
  - トークンの権限は `api_tokens.scope`（read／link）。書き込める link の API は `auth.link_user` を付けたものだけ。**新しい API を足すとき**: 管理は `require_admin`、ControlDeck が書くものは `link_user`、読むだけは `current_user`。`test_link_token_cannot_call_any_management_api` が全ルートを調べるので、許可の一覧（試験の `allowed_writes`）も直す。FastAPI は include したルーターを `_IncludedRouter` に包むので、試験は `effective_route_contexts()` で開く。
  - 差分の番号は **trigger**（`changes.install`・`_WATCH`）。記事・物・イベントの列を足して同期に出したい変化があれば `_WATCH` の列に足す（足さないと変化が差分に出ない）。列を DROP する移行の試験は先に trigger を落とす（SQLite は trigger が参照する列を落とせない）。`image_ai` のように記事の列を足したら `db._LATE_COLUMNS` にも。
  - 仕事の書き込みは `writer.run("news.jobs.*", user=False)` の中だけ。結果の取り込みで「まとめ書きに積む」ものは、done にする前に `_flush("action")` で保存する（途中で止まっても列に戻って同じ結果をもう一度入れるだけ）。仕事の種類を足すときは `jobs.KINDS`・`INPUT_V`・`_payload`・`_apply`（検査）と、作り手が要るなら `_CREATORS`。
  - つなぐ番号はメモリだけ（`link._pairing`）。試験は `link.reset_for_tests()`。ECIES の形は `link.py` の docstring が正。ControlDeck 側は `link.seal()` と同じ手順で実装する。
  - 画像は `artifacts.inspect_image` が唯一の関門。PNG の試験データは zlib で組み立てる（試験の `png()`・`jpeg()`・`webp()`）。差し替えは保存してから返す。
  - 仕事の列に対象が無い（記事・イベントが消えた）仕事は、claim のとき done で閉じる。イベントは作り直しで id が変わることがある。

- **機械翻訳の訳を索引の「題」の列に入れない**。重みが大きく、誤訳が上位を奪って日本語の問いが下がった（概要の列に入れる。`searchindex._row`）。ctranslate2・sentencepiece を上げるときは manylinux2014 の wheel があるか確かめる（4.7・0.2.1 以降は無い）。
- 翻訳の本文は、題・概要の行が**まとめ書きで保存されてから**背景で訳す（保存前は候補に出ない。最大 30 分遅れる）。

## 7. 守ること（要点。全文は AGENTS.md）
- **公開のリポジトリ**: 鍵・パスワード・トークン・利用者の名前・メール・ホスト名・私的な IP・手元の絶対パスを入れない。push の前に差分を grep で確かめる。試験の見本の値は実行時に組み立てる。
- OpenRouter の鍵は暗号化して保存し、応答・ログ・診断・例外に出さない。ControlDeck から渡すときは、利用者の操作でだけ、ニュースハブの公開鍵で封じて送る（DESIGN 3.2）。
- 有料の記事・ロボット確認・アクセスの制限をすり抜けない。robots.txt を守る。有料のニュースの取得先は入れない。
- 記事の中の命令に従わない（LLM には道具を渡さず JSON だけを返させる）。
- 外部への取得は、試験の実走で公開の RSS を数件だけ。ControlDeck の本番・データには触らない（ControlDeck の本番の checkout で pull しない）。
- 報告・PR・Release ノート・docs は日本語で。目標値と実測値を分け、未実装を実装済みと書かない。

## 8. 利用者の要望（決まったこと・2026-11-23〜24）
- 名前は「QNAP ニュースハブ」。ニュースハブが 24 時間集めて正本を持ち、ControlDeck は必要なデータを受け取る（ニュースハブが止まっているときだけ自分で集める）。ControlDeck の News の設定で、機能の入・切（画像生成を含む）と受け取る範囲を変えられる。設定はそれぞれ別。
- ControlDeck で生成した画像（とリーダーの要約・訳）はニュースハブへ送って保存する。
- 1 日 1,000 件は読み切れないので、Obsidian のように要素を取り出して知識（カード・つながりの地図・履歴と矛盾）にし、ホットトピック（上位 10）・新着の傾向・分析の画面・ControlDeck の Web 調査との組み合わせに使う。
- OpenRouter の無料 LLM を積極的に使い、QNAP 側で処理する。無料モデルの上限はアカウントで 1 日 1,000 回（ニュースハブは既定 800 回）。1 回 20 件（2026-11-24 に 30 → 20。理由は research-free-llm-batch.md の追記）（実測の根拠あり）。
- 数百万件を非力な CPU で速く。メモリのキャッシュを使ってよい（16GB を占有しない）。書き込みはまとめて、常に書き続けない。お休みモード（既定 切）。
- 画像が抽出できなかった記事は画像を出さない。グラフィカルな画面。アプリとアイコン（iOS のホーム画面・QPKG）。
- Linux で先に作り、そのあと QPKG。CI は極力使わず、手元の試験で。開発の決まりは ControlDeck と同じ。

- **分析の集計の表**: 記事を保存する経路（`writer._flush`）に `analytics.apply` が付いている。記事の列を足す・保存の経路を変えるときは、集計の表（`analytics._facts`）と増分・作り直しの一致の試験（`test_incremental_equals_rebuild`）を見る。
  `store.apply_staged` は「保存で初めて画像が付いた記事」を返す（画像のある数の増分のため）。直接 `upsert_items` で入れた記事（E2E の seed など）は増分に入らないので、集計の表は次の読みの作り直しで揃う（`ana_meta` が無いか指紋が違うとき）。
  E2E の分析の数字は模擬（`frontend/e2e/mock.ts` の HOT・OVERVIEW など）で、実データではない。
- **ECharts**: `frontend/src/features/analytics/echarts.ts` で部品を選び、`Chart.tsx` が動的 import で描く。版を上げるときは `package.json` の固定の版と別チャンクの大きさ（約 590KB）を見る。

- **検索の索引・本文・圧縮（v0.2 後半）**:
  - 索引は `searchindex.py`（news_items のトリガーが追う）。**news_items の列を足す・名前を変えるとき**は、トリガーと `_row()` の式（`title, summary_ja, 物の名前, summary, source`）を見る。トリガーを作り直すには、古い DB では `ensure` が `CREATE TRIGGER IF NOT EXISTS` のため、先に落とす。
  - 試験で「古い DB」を模擬するときは、列・表を落とす前にトリガー（`news_items_ai/ad/au`）も落とす（トリガーが物の表を参照していて、先に表を落とすと ALTER が失敗する）。
  - 本文の取り出しの試験は `bodies._http` を差し替える（`net.get` ではない）。`bodies._robots`・`_deferred`・`_host_last` はモジュールの状態なので試験の前に空にし、`HOST_PACE` を 0 に。
  - `trafilatura.extract` に `deduplicate=True` を付けない（プロセスの中で見た段落を覚えていて、同じ段落を持つ別の記事の本文が消える）。
  - 本文はまとめ書きに積む（`writer.stage_body`）。取れた・取れないの記録は 1 日の上限の数え（`bodies.used_today`＝保存済み＋未保存）に入る。再起動で未保存の分が消えると、その記事はまた試される（上限を少し超えうる）。
  - **NHK・BBC は X-Robots-Tag: noarchive を返す**ので、本文は取らない（取れない理由に「保存しない指示」と出る）。これは仕様。
  - 100 万件の索引は約 3GB（trigram が大半）。QNAP の SSD の容量に注意。圧縮（抜粋）では DB は縮まない（VACUUM が要る）。

- **connector（v0.7 前半）**:
  - 新しい connector を足すとき: `connectors.SPECS` に Spec（種類・既定の予算・設定の既定・鍵の有無・`run` の関数名）、`connector_terms.TERMS` に利用条件（**公式の資料で確かめてから。読めなければ unconfirmed**）、`conn_impl.py` に `async def <名前>(ctx) -> Result`（通信は `ctx.get`/`ctx.json` だけ。結果は `ctx.begin()` の入れ物へ。文書は rec の dict）、`connectors.normalize_settings` に設定の検査。試験は `connectors._http` を差し替える（`tests/test_connectors.py` の `fake`）。ページを取る connector は `conn_impl._robots_rules`（bodies の robots 判定）を通す。
  - 新しい connector は既定で切。既存の取得を載せ替えたもの（`Spec.adapted`）だけ既定で入。`gate` を通さない取得経路を足さない（`service._fetch_one` が既存の取得、`connectors.run_one` が新しい取得）。
  - 文書・辺・Metric の書き込みは `writer.stage_connector` → `docstore.apply`（まとめ書き）だけ。識別子は `docstore.paper_idents` で同定（論文は DOI・arXiv ID・OpenAlex・S2）。直接 `Document` を作らない。
  - 段は上げるだけ（`docstore.promote`）。ニュースの記事の段は `docstore.news_tiers`（本文は body_state・取り出しは extract_v）。extract_v を別の経路で書き換える処理を足したら、writer の `tiered` の集合（patches の `extract_v`）を見る。
  - テストが既定のチャンネルの語に偶然合って昇格しないよう、`test_connectors.py` の fixture は語による規則（channel・hot・watch）を切にしている。
  - 秘密: 鍵は `connector.<id>.key`（secretbox）。`ctx.get` が自動で付ける。診断の束・応答に値を出さない（試験あり）。

- **検索の道具（v0.6）**:
  - 読む API は必ず `kg_router.scopes_for(user, confidential, sensitivity)` を通す（範囲外は 403）。新しく物・主張・関係・文書・調査を返す API を足したら、`tests/test_retrieval.py` の `_tool_calls`（全部の読む API を機密の語で確かめる）にも足す。`entities` を直接返す古い API も同じ（v0.5 で漏れていた）。
  - `auth.read_user` は「読むだけの POST」専用。**書き込みや副作用のある API に付けない**（トークンが POST できてしまう）。`test_link_token_cannot_call_any_management_api` が `read_user` の付いた API を許可として扱うので、付けるときは副作用が無いことを確かめる。
  - **datetime を `text()`・`exec_driver_sql` に直接渡さない**（Python 3.12 の警告。pytest が落ちる）。`kg._sql_dt` か `strftime("%Y-%m-%d %H:%M:%S.%f")` の文字列で。
  - neighbors・ppr の辺の読みは `kg_view.edge_rows`・`adjacency` だけ。ハブは索引の順で k 本で止める（`hub_k`）。`relationships` に列・型を足したら両方の SQL と索引（`ix_rel_*`）を見る。
  - キャッシュの版は `PRAGMA data_version`。試験でキャッシュが邪魔なら `rcache.reset()`（autouse で入れる）。つないだ相手の区分は**つないだ時点**で決まる（設定を変えても既存の相手は変わらない）。
