# QNAP ニュースハブの計画

**その後の統合（ControlDeck × QnapNewsHub の統合ナレッジ基盤・2026-11-25）の計画と採否は、ControlDeck の `docs/PLAN-integrated-knowledge.md`。統合の実装はこの計画の v1.0 の後**。

設計の正は `docs/ARCHITECTURE.md`（2026-11-24、利用者の構想をまとめ直したもの）。経緯は `DESIGN.md`・`KNOWLEDGE.md`。無料の LLM の件数の根拠は `research-free-llm-batch.md`。

各マイルストーンで使える状態にし、PR・レビュー・マージ・Release を通す。目標値と実測値を分けて報告し、未実装を実装済みとして書かない。

## 済んだ段
| 段 | 中身 | 状態 |
|---|---|---|
| 0 | リポジトリの骨組み | ☑ |
| 0.5 | Python 3.12 のパッケージと `./hub.sh`（setup・test・run・create-admin） | ☑ |
| 1 | ニュースの核の移植・外部 LLM の下ごしらえ（20 件ずつ。2026-11-24 に 30 → 20、理由は research-free-llm-batch.md の追記・上限）・認証・暗号化した鍵（DESIGN 2.5・2.6） | ☑（2026-11-24・#1） |

## マイルストーン
| 版 | 中身 | 確かめ方 | 状態 |
|---|---|---|---|
| v0.1 基盤 | 画面（ControlDeck の 5 デザイン・取得先の管理・設定・ログイン）・PWA とアイコン（iOS・QPKG 用も）・取得先の確かめ・**Write Coordinator**（30 分のまとめ書き・checkpoint の制御）・お休みモード・**診断**（取得・LLM・仕事の列・保存と I/O・機械・自己点検、ARCHITECTURE 9.5）・Linux の常駐（systemd のユーザーの単位）・QPKG（x86_64） | E2E（320・390・1280、ライト・ダーク）・開発の PC で 24 時間・/proc/self/io で待機中の書き込み 0・利用者の QNAP に入れる | 作業中（**画面・PWA とアイコン 済**〔2026-11-24〕・**Write Coordinator・お休みモード・診断・Linux の常駐（systemd のユーザーの単位） 済**〔2026-11-24。24 時間の実測と QNAP 実機は未〕。**取得のエンジン（ETag・Adaptive Polling とバックオフ・URL／GUID／SimHash の重複・CSV/OPML・出どころの種類） 済**〔2026-11-24〕。**QPKG（x86_64） 作った**〔2026-11-24。QTS を模した CentOS 7 のコンテナで合格、docs/QPKG.md〕。残り: QNAP 実機に入れる・長時間の実測〔利用者「24 時間も待たなくて良い」〕） |
| v0.1.1 更新 | 署名つきの Release・更新の確認（1 日 1 回・ETag）・QPKG の自動の入れ替えと前の版への戻し・設定の更新の節・ホームの知らせ（docs/UPDATE.md） | 試験・E2E・QTS の模擬（run-sim.sh update）・利用者の QNAP で更新 | 作った（2026-11-24。実機・本物の Release は未） |
| v0.2 速いデータベースと検索・QNAP の無料 LLM の分析 | **無料の外部 LLM の係**（予算 1 日 800 回・用途ごとの割り当て・モデルの切り替え・質の守り、ARCHITECTURE 5.1）・概要と要素の取り出しを 1 回で・粗いイベントのまとめ・本文の取り出し（Trafilatura）・**新着の傾向**（7.5。LLM なしの数字の部分は分析の第 1 版で済み）・FTS5 ＋ trigram・本文の zstd の塊・キャッシュ（上限の設定）・集計の表（済）・100 万件の模擬のデータでの性能（分析 API は実測済み。DESIGN 9 章） | 10 万・100 万件で冷えた／温まった状態の P95 | 作業中（**前半 済**〔2026-11-24〕: 無料の外部 LLM の係〔予算 800 回・用途ごとの割り当て・夜の後回し・モデルの順番と切り替え・ControlDeck のゲートウェイ〕・概要と要素の取り出しを 1 回で・物と fact の保存と画面〔最小〕。本物の LLM での質の評価は未。**粗いイベント・イベントの要約・新着の傾向と LLM の解説・物のホットトピック 済**〔feat/events-trends〕。**検索・本文・圧縮 済**〔feat/search-storage〕: 日本語の全文検索（trigram＋英語）・本文の取り出し（Trafilatura・実走 20 件成功）・zstd・保存の圧縮・キャッシュ・画面。100 万件の温まった P95 は最大 483 ms〔この PC。DESIGN 12〕。残り: QNAP 実機での速さ・300 万件以上） |
| v0.3 ControlDeck との連携と AI の分析 | ペアリングと鍵の封じた受け渡し・仕事の列（lease・冪等）・`/changes` の同期・ControlDeck 側の係（本文・埋め込み・要約・取り出し・イベント）・ControlDeck のやり直しと高品質の仕事・ControlDeck 側の設定（出どころ Hub／ローカル・機能の入・切・受け取る範囲） | 両方の試験・本番の ControlDeck を止めた間の記事が後でまとめて分析されること | 作業中（**ニュースハブ側 作った**〔2026-11-24・feat/link-controldeck〕: ペアリング・scope=link のトークン・封じた鍵（ECIES）・`/changes`・仕事の列・生成物の受け取り・調査の記録・つなぐ画面と診断。ControlDeck 側の係・設定は別の PR。本物の ControlDeck とのつなぎは未） |
| v0.4 トレンドと分析の画面 | ホットトピック（点数）・推移・ヒートマップ・一緒に出る分析・日次の報告・ECharts | 実データの 1 週間で上位が一般語にならないこと | 一部 済（前倒し・2026-11-24: **集計の表・分析の画面・ホットトピック（LLM なし）・推移・ヒートマップ（取得先 × 日）・ECharts**。残り: 一緒に出る分析・日次の報告・実データの 1 週間の確かめ） |
| v0.5 知識グラフ（**統合の土台として汎用に**: Document〔news・paper・repo・model・research・note〕・Evidence〔根拠の位置〕・Relationship〔型・方向・根拠・確かさ〕。ニュース以外は表と API だけ） | 物・主張・根拠・関係・履歴と矛盾・知識カード・メモ（上書きしない）・地図（Cytoscape.js） | 試験・実データで「以前は非対応で最近対応したもの」に答えられること | 作った（2026-11-25・feat/knowledge-graph。試験・E2E・10 万件の性能は済み。**実データ・本物の LLM での関係・概要の質は未**。ARCHITECTURE 6 章・DESIGN 13 章） |
| v0.6 ハイブリッド RAG と Web 検索の統合（**ControlDeck の MPKR が使う検索の道具の API**: 種の検索〔全文・物〕・`POST /graph/neighbors`〔段ごとのまとめ取り・辺の型・上限〕・局所 PageRank、調査の記録 `/research`。MPKR 本体は ControlDeck） | ControlDeck の調査にニュースハブを提供元として足す・RRF と reranker・調査の記録の保存と再利用 | ControlDeck の評価（本番で） | 検索の道具の API 作った（2026-11-25・feat/retrieval-api: 種の検索・neighbors の拡張・局所 PPR・根拠のまとめ取り・`GET /research`・区分の強制・キャッシュと上限。性能は DESIGN 15）。残り: ControlDeck 側の MPKR（I4）・RRF と reranker・本物での評価 |
| v0.7 収集の connector の枠組み・数値・比較・高度な分析（ControlDeck の PLAN-integrated-knowledge 8 章） | **connector の枠組みと画面（予算・利用条件）・L0〜L4 と昇格・GitHub（発見・スターの急増）・HF（派生版）・arXiv・OpenAlex・Crossref・Semantic Scholar・HN 検索・Sitemap・変化の検出・Metric の時系列**。続き: ~~ベンチマークの DB（条件つき）・ウォッチリストと変化・変化点~~（済 2026-11-26・feat/watch-changes。DESIGN 26）・トピックの分類・意見の違い（設計のメモだけ。ARCHITECTURE 12.1）| 試験・E2E・実走（各 1〜2 回）・実データ | 前半 済（2026-11-25・feat/connectors。実データでの昇格の質・Semantic Scholar／Crossref の本物・実機の負荷は未。ARCHITECTURE 10 章・DESIGN 14 章）。残り: 数値・比較・高度な分析 |
| v0.8 収集の connector（日銀・e-Stat・国会会議録・e-Gov 法令・EDINET・DefiLlama・CoinGecko・GDELT・PyPI・npm・OSV。ControlDeck の PLAN-integrated-knowledge 8.2 の中・低） | **鍵の要るもの〔e-Stat・EDINET・CoinGecko〕は鍵があるときだけ動く（既定は切）**・数値は数値のまま（単位・時点・出どころ・条件）・RWA トークンとデジタル証券は別の名前空間・法令は版ごとの文書と段（提案・成立・施行・廃止）・GDELT は L0 で「未確認」の印・文書の種類 record・law・filing・release・advisory・利用条件の記録（読めなければ未確認・既定は切） | 試験・E2E・実走（鍵なしを各 1 回） | 済（2026-11-25・feat/connectors-v08。実走は GDELT 以外で成功。**鍵の要るもの 3 つと npm は本物を未確認**。ARCHITECTURE 10 章・DESIGN 24 章） |
| 軽い翻訳（2026-11-25・feat/translate） | **QuickMT 英→日（CTranslate2・int8）**: エンジンの枠・モデルを利用者の操作で取得（版・sha256 固定）・用語の保護（URL・コード・Things・版・単位）と対訳表・訳の検査（落ちた文は原文のまま）・文ごとのキャッシュ・別の表に保存して全文検索の索引へ・LLM が使えないときは題と本文だけ・重要な記事の本文を空き時間に・開いたとき訳す API・`translate_hq` の仕事・設定と診断の節・「機械翻訳」の印と原文の切り替え（ARCHITECTURE 8.5・DESIGN 19） | 試験・E2E・QPKG の模擬・本物の QuickMT の実走・物差しの前後 | 作った（検索の点数は物差しが飽和していて変化なし。QNAP 実機での速さ・メモリは未） |
| v0.5 の続き: Things の脳（2026-11-25・feat/things-brain） | 物を **Things** へ改名・カードの帯・関係の型分け・LLM の定義と未解決の問い・主な数値・つなぐ候補・主題の入口（MOC）・全体の地図（検索の強調と絞り・ローカル）・LLM の脳の API（context）・Obsidian の書き出しを厚く（ARCHITECTURE 6 章の続き・DESIGN 16） | 試験・E2E・性能（模擬 10 万件）・実データの撮影 | ☑（本物の LLM での質は未） |
| v0.9 測定の記録（Cond 2.0・2026-11-26） | 数値を中心にした測定の記録（対象・指標・値・単位・測り方・疎な条件・原文の位置）。分野ごとの条件の定義（LLM 性能・価格から）。規則と表の取り込みを先に、曖昧な結び付けだけ LLM（候補の番号を選ばせる）。条件は正規化の完全一致で比べ、一致・不明・違うを分ける。独立した物差し（測定 300 以上・問い 200〜300）。段は C1〜C6b。**詳細は docs/MEASUREMENT.md** | ☐ C1 から |
| v0.8 生成物と外部の互換 | 画像・報告の保管（hash）・Markdown／Obsidian・OPML の書き出し | 試験 | ☐ |
| v1.0 安定版 | 300 万・500 万件の試験・障害からの戻り・守りの検証・実機の I/O・データを残す更新の試験 | ARCHITECTURE 15 章の目標 | ☐ |
