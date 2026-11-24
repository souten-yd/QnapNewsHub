# QPKG（QNAP のアプリ）

QNAP（x86_64。例: TS-253Be の Celeron J3455）向けの QPKG の作り方・入れ方・確かめ方。2026-11-24。

## 中身
| 置き場（QPKG の中） | 中身 |
|---|---|
| `python/` | python-build-standalone の CPython 3.12（版と sha256 を `packaging/qpkg/build.sh` で固定。glibc 2.17 以上で動く） |
| `site/` | 依存（`packaging/qpkg/requirements.lock` の版。manylinux2014 = glibc 2.17 の wheel だけ） |
| `app/qnapnewshub/` | サーバー（backend） |
| `web/` | build 済みの画面（QNAP の上では build しない） |
| `QnapNewsHub.sh` | 起動・停止（App Center から） |
- `.pyc` は build のときに作る（`--invalidation-mode unchecked-hash`）。動いている間は QPKG の置き場に書かない（`PYTHONDONTWRITEBYTECODE`）。
- QPKG にまとめるのは公式の QDK 2.5.3 の `qbuild`。手元の機械にシステム全体で入れずに済むよう、podman のコンテナ（debian bookworm-slim ＋ QDK の deb〔sha256 固定〕＋ ImageMagick）の中で使う。アイコンは `packaging/qpkg/icons/` の PNG を GIF（64・80・灰色）に変える。

## 作る
```
./hub.sh setup && ./hub.sh build      # 依存と画面
./hub.sh qpkg                          # → dist/QnapNewsHub_<版>_x86_64.qpkg と .sha256
```
取得物（Python・QDK の deb）は `packaging/.cache/`（git に入れない）。大きさは約 90MB。

## 入れる（利用者の QNAP）
1. QTS の App Center → 右上の「手動でインストール」（＋の箱のアイコン）→ `QnapNewsHub_<版>_x86_64.qpkg` を選ぶ。QNAP の署名が無いアプリなので、QTS の設定で「デジタル署名の無いアプリのインストールを許可」が要る（App Center の設定）。
2. 入ると自動で起きる。App Center の「開く」か `http://<NAS の IP>:8787/`。
3. **最初の管理者を作る番号**: 初めて開くと「一回限りの番号」を求められる。番号は QTS の **通知センター → システムのログ** に「[QnapNewsHub] 最初の管理者を作る一回限りの番号: XXXX-XXXX-XXXX」と出る（QTS の管理者だけが見られる）。データの置き場の `setup-token.txt`（0600）にもある。管理者を作ると番号は消える。
   - 理由: QPKG は LAN（0.0.0.0:8787）に待ち受けるので、番号が無いと LAN の誰でも先に管理者を作れてしまう。
4. 設定 → LLM で OpenRouter の鍵を入れる（または、v0.3 以降は ControlDeck から封じて渡す）。

## データの置き場
- 既定は `<QPKG の置き場の隣>/QnapNewsHub-data`（例: `/share/CACHEDEV1_DATA/.qpkg/QnapNewsHub-data`、権限 700）。`/etc/config/qpkg.conf` の `[QnapNewsHub] Data_Dir` で変えられる（止めてから変え、中身を移す）。
- **QPKG を更新しても、消しても、データは残る**（QDK は QPKG の置き場だけを消す）。データも消したいときは、QPKG を消したあとに SSH でこの置き場を消す。
- 更新のときは、古い版のプログラム（python・site・app・web）を消してから新しい版を入れる（`package_routines`）。

## 更新（アプリの中から）
0.1.1 から、設定の「アプリの更新」で、GitHub の Release の署名つきの .qpkg を確かめて入れられる（docs/UPDATE.md）。起動台本は `NEWSHUB_INSTALL=qpkg`（QPKG で動いている印）と `NEWSHUB_QPKG_ROOT`（QPKG の置き場）を渡す。取得物はデータの置き場の `updates/`（QPKG の更新でも消えない）。模擬: `packaging/qpkg/test/run-sim.sh update`。

## 止める・更新
- 停止は SIGTERM。Write Coordinator がメモリに溜めた記事を書き切るまで最大 100 秒待ち、それでも止まらなければ強制終了（`qpkg.cfg` の QPKG_TIMEOUT は停止 120 秒）。
- 待ち受けのポートは `qpkg.cfg` の 8787（`qpkg.conf` の `Web_Port` を変えると起動台本が従う）。待ち受けのアドレスは起動台本が 0.0.0.0 を渡す（設定の画面の「待ち受け」より環境変数が勝つ）。

## 確かめ方（実機が無いとき）
```
./hub.sh qpkg && packaging/qpkg/test/run-sim.sh
```
CentOS 7（glibc 2.17 = 同梱の Python と wheel が求める最も古い glibc）のコンテナの中で QTS を模して（`getcfg`・`setcfg`・`log_tool` の模擬、`/etc/config`、`/share/CACHEDEV1_DATA/Public`、`/mnt/HDA_ROOT`）、次を確かめる:
入れる（QDK の成功の終了コードは **10**）→ 起きる（health・画面・manifest）→ 番号がシステムのログに出る・ファイルは 0600 → 番号なしの setup は 403・番号ありは 200・番号のファイルが消える → 動いている間に QPKG の置き場に .pyc を書かない → 同梱の Python で HTTPS が通る（証明書は certifi）→ 止まる → 入れ直してもデータ（管理者）が残る。

模擬と QTS の違い（模擬の側で合わせたもの）:
- QTS の busybox sh は `. qpkg.cfg` を今の場所から読むが、CentOS の bash は PATH から探す → 模擬で PATH に `.` を足す。
- インストーラは塊を 1KB 単位で切り出すので data.tar.gz の末尾に余りが付く。QTS では `/usr/local/bin/python` で切り詰めるか busybox の gzip が無視する。CentOS 7 の GNU gzip は警告（終了コード 2）にする → 模擬で 2 を成功として扱う。

### 2026-11-24 の結果（模擬・開発の PC）
すべて合格: 入れる・起きる（glibc 2.17）・画面 200・番号がログに出る・0600・番号なし 403／あり 200・番号のファイルが消える・.pyc の書き込み 0 件・HTTPS 200・SIGTERM で止まる・入れ直しで管理者が残る。

### 更新の模擬（`run-sim.sh update`）
0.1.0 相当の古い QPKG を入れる → 署名の違う Release は入れない（取得物も残らない）→ 正しい Release から API 経由で 0.1.1 へ更新 → サーバーが新しい版で起き直す・qpkg.conf の版が変わる・データ（管理者・ファイル）が残る・結果が `last.json` に出る。

## 確かめていないこと
- **QNAP の実機**（QTS の版・App Center の手動インストールの画面・署名の無いアプリの扱い・通知センターでの番号の見え方・電源を入れ直したあとの自動起動）。
- 実機の CPU（Celeron）での起動の時間・常駐のメモリ・待機の CPU。
- QuTS hero・ARM の機種（ARM 向けの QPKG は作っていない）。
