# 引き継ぎ（HANDOFF）

開発を途中から引き継ぐ人（人でも AI でも）が、最初に読む文書。**新しい段を終えるたびに、この文書の「今の状態」と「次にやること」を更新する**。

最終更新: 2026-11-24（#9 の後）

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
| #1 | 0.5・1 段: ControlDeck のニュースの核を移植（取得・目録 472 件・取得先ごとの間隔と履歴・通信確認・一覧の巡回・フィードの生成・記事 DB・FTS5・画像・Google ニュース・チャンネル・好み）、OpenRouter の下ごしらえ（30 件ずつ・上限・送り直し）、認証（管理者の session・読み取りの API トークン）、暗号化した鍵、`./hub.sh` |
| #2・#3・#4 | 設計: 知識化 → 設計 v2（ARCHITECTURE.md）→ 無料 LLM を QNAP 側の主力に・新着の傾向・診断 |
| #5 | 画面（React ＋ Vite。ControlDeck の 5 デザイン・まとめのバブル・取得先の管理・設定・ログイン）・PWA とアイコン（iOS・QPKG 用）・E2E |
| #6 | v0.1: Write Coordinator（まとめ書き 30 分）・お休みモード・診断・systemd のユーザーの単位の生成 |
| #9 | v0.1: QPKG（x86_64）`./hub.sh qpkg`・QTS の模擬での確かめ（`packaging/qpkg/test/run-sim.sh`）・LAN に待ち受けるときの最初の管理者の一回限りの番号（`NEWSHUB_SETUP_TOKEN=1`）。docs/QPKG.md |
| 更新（0.1.1） | 署名つきの Release（Ed25519）・更新の確認（1 日 1 回・ETag・メモリだけ）・QPKG の入れ替え（切り離した子で `sh <qpkg>`）・前の版への戻し・設定の更新の節・ホームの知らせ・診断。docs/UPDATE.md。`./hub.sh release <版>` が Release の台本（作るだけ。実際の Release は司令塔が作る） |
| 分析（第 1 版） | 集計の表（`analytics.py`・まとめ書きと同じトランザクションで増分・作り直し）・`/api/v1/analytics`（概要・推移・ホットトピック・取得先・比較）・画面「分析」（ECharts 5.6.0 を遅延読み込み）・ホームの上位 5。PLAN v0.4 の一部を前倒し。ARCHITECTURE 7・7.5・11 章の「実装と違った点」、DESIGN 9 章（速さの実測） |
| v0.1.1 | **Release 済み**（GitHub の Release、署名つき） |
| v0.2 前半（feat/llm-worker） | 無料の外部 LLM の係（予算 1 日 800 回・1 分 15 回・用途ごとの割り当て・夜の後回し・モデルの順番と切り替え）・**ControlDeck のゲートウェイ**（OpenAI 互換。先に使い、つながらなければ OpenRouter へ）・概要と要素の取り出しを 1 回の呼び出しで・物（entities）と fact（claims）の保存・`/api/v1/entities`・画面「物」・カードの札・`./hub.sh llm-try`。ARCHITECTURE 5.1 の「実装と違った点」・DESIGN 10 章 |
| v0.2 後半の一部（feat/events-trends） | イベントへの粗いまとめ（`events.py`・表 events／event_articles・まとめ書きで増分）・イベントの画面・ニュースの「ほか N 件」・イベントの要約（用途 event・既定 100）・新着の傾向（分析の節）と「今日の傾向」の解説（用途 trend）・物のホットトピック。ARCHITECTURE 4・7.5 の「実装と違った点」・DESIGN 11 章 |
| #8 | v0.1: 取得のエンジン（ETag/Last-Modified・Adaptive Polling とバックオフ・URL の正規化と SimHash の近い重複・CSV/OPML・出どころの種類）。実走で 5 件中 4 件が 2 回目に 304 |

- 試験: backend 245 件・E2E 135 件（分析の第 1 版の時点）。QPKG は `packaging/qpkg/test/run-sim.sh` で合格。
- 実測（開発の PC）: 待機中の書き込み 0 バイト／10 分、お休み中も 0、常駐 114〜190MB、待機 CPU 0.1〜0.4%。
- **作業中**: なし（#9 の時点）。
- まだ無いもの: QNAP 実機での確認・長時間の連続運転・QNAP 実機・**本物の LLM での取り出しの質の評価**・本物の LLM でのイベントの要約・解説の質・イベントのしきい値の実データでの調整・一緒に出る分析・ControlDeck との連携（ペアリング・仕事の列・同期）・知識グラフ（分析の画面の「知識の地図」「物のノート」は空き枠だけ）。
- 既知の小さなずれ: 古い設定ファイルに保存済みの 1 日 300 回は、そのまま残る（既定の 800 回は新しい設定だけ）。まとめ・一言要約はモデルの順番に乗っていない（固定のモデル）。試験で StarletteDeprecationWarning が 1 件出る（動作に影響なし）。

## 4. 次にやること（順番）
1. 開発の PC で動かす（利用者「24 時間も待たなくて良い」〔2026-11-24〕。長時間の運転は並行して、結果を待たずに次へ進む）（`./hub.sh install-service` か `./hub.sh run`、目録から 50〜100 件を有効に）。取得の実績・失敗・書き込みの量・メモリを診断の画面で見る。
2. QPKG を利用者の QNAP に入れてもらう（`./hub.sh qpkg` の .qpkg を渡す。手順は docs/QPKG.md。型番と CPU も QTS の画面で確認してもらう）。動いたら v0.1 を Release（.qpkg を Release の添付に）。
3. （済）分析の第 1 版（集計の表・API・画面）。続き: 実データを 1 週間ためてホットトピックの上位が一般語にならないか見る（ストップワードの調整）・集計の表の作り直しが長い（100 万件で約 34 秒）ので、必要なら分割する。
4. v0.2 の後半: （前半の無料 LLM の係・要素の取り出し・物の画面は済）**本物のゲートウェイ・OpenRouter での取り出しの評価**（`./hub.sh llm-try`。30 件のままでよいか・名寄せの精度）・本文の取り出し（Trafilatura）・FTS5 ＋ trigram・zstd・集計の表・100 万件の模擬データでの性能。
5. （済）v0.1.1 は Release 済み。残り: 実機の QNAP で「古い版 → 更新」を確かめる（利用者の QNAP は 0.1.0 を手で入れた場合、更新の機能が無いので、0.1.1 だけは手で入れ直す）。
6. v0.3 以降は PLAN.md の順。ControlDeck 側の変更は ControlDeck のリポジトリで、その決まり（関門・PR）で行う。


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
- OpenRouter の無料 LLM を積極的に使い、QNAP 側で処理する。無料モデルの上限はアカウントで 1 日 1,000 回（ニュースハブは既定 800 回）。1 回 30 件（実測の根拠あり）。
- 数百万件を非力な CPU で速く。メモリのキャッシュを使ってよい（16GB を占有しない）。書き込みはまとめて、常に書き続けない。お休みモード（既定 切）。
- 画像が抽出できなかった記事は画像を出さない。グラフィカルな画面。アプリとアイコン（iOS のホーム画面・QPKG）。
- Linux で先に作り、そのあと QPKG。CI は極力使わず、手元の試験で。開発の決まりは ControlDeck と同じ。

- **分析の集計の表**: 記事を保存する経路（`writer._flush`）に `analytics.apply` が付いている。記事の列を足す・保存の経路を変えるときは、集計の表（`analytics._facts`）と増分・作り直しの一致の試験（`test_incremental_equals_rebuild`）を見る。
  `store.apply_staged` は「保存で初めて画像が付いた記事」を返す（画像のある数の増分のため）。直接 `upsert_items` で入れた記事（E2E の seed など）は増分に入らないので、集計の表は次の読みの作り直しで揃う（`ana_meta` が無いか指紋が違うとき）。
  E2E の分析の数字は模擬（`frontend/e2e/mock.ts` の HOT・OVERVIEW など）で、実データではない。
- **ECharts**: `frontend/src/features/analytics/echarts.ts` で部品を選び、`Chart.tsx` が動的 import で描く。版を上げるときは `package.json` の固定の版と別チャンクの大きさ（約 590KB）を見る。
