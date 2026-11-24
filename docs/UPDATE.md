# 更新（署名つきの Release・確認・入れ替え・戻し）

2026-11-24（版 0.1.1 で入った）。実装は `backend/qnapnewshub/updater.py`・`update_router.py`・`release_key.py`、署名は `packaging/release/sign.py`、画面は設定の「アプリの更新」。

## 仕組み
1. **Release**（GitHub）に、`QnapNewsHub_<版>_x86_64.qpkg` と、署名の 2 ファイル `….qpkg.manifest.json`・`….qpkg.manifest.sig` を添付する（`./hub.sh release` が作る）。
2. **確認**: GitHub の API（`/repos/souten-yd/QnapNewsHub/releases`）で、今の版より新しい Release を探す。1 日 1 回（起動の 2 分あとから）と、画面の「今すぐ確認」。ETag を付けて GitHub の制限を節約する（304 なら前の結果のまま）。結果は**メモリだけ**（ディスクへ書かない）。お休みモードの間は自動の確認をしない。
   - 版の比較は SemVer（0.1.0 < 0.1.1 < 0.2.0-rc1 < 0.2.0）。試験版（prerelease）は設定で含めたときだけ対象（既定は含めない。`<データの置き場>/update-settings.json`）。
3. **更新**（QPKG で動いているときだけ。管理者の操作・確認の画面つき）:
   1. manifest と署名を取り、**署名を確かめてから**大きい .qpkg を取る（https だけ・`github.com`・`objects.githubusercontent.com`・`release-assets.githubusercontent.com` だけ・リダイレクトは各段を検査・300MB まで・manifest の大きさを超えたら止める）。
   2. **検査**: Ed25519 の署名（manifest のバイト列そのもの）・名前（`QnapNewsHub`）・版（Release の版と同じ）・大きさ・sha256。1 つでも合わなければ取得物を消して入れない。
   3. メモリの記事を書き切る（`writer.flush`）。
   4. 取得物をデータの置き場の `updates/` に置き（置いたものを確かめ直す）、**新しいセッションの子**（`start_new_session` = setsid。サーバーが止まっても道連れにならない）で `sh <qpkg>` を流す。QDK のインストーラーがサーバーを止めて、プログラムを入れ替え、起こし直す。QDK の成功の終了コードは **10**（0 も成功と見る）。
   5. 子が結果を `updates/last.json`（`{kind,from,to,rc,at}`）に、インストーラーの出力を `updates/install.log` に書く。画面は、サーバーが新しい版で起きると自動で読み込み直し、「更新しました／失敗しました（ログ）」を出す。成功は「終了コードが 10 か 0」かつ「今の版が目標の版」。
4. **戻す**: `updates/` に前の版（今より古い版）の取得物が残っていれば、「前の版に戻す」で、署名などを**もう一度検査して**入れる。取得物は「今入れる版＋それ以外で最も新しい 1 つ前の版」だけ残す。初めて更新するとき（手で入れた版しか無いとき）は戻せない。
5. **Linux（QPKG でない）**: 新しい版があることと手順（git で取り直して `./hub.sh setup && ./hub.sh build`、サービスの再起動）を出すだけ。自動では入れない。
6. お休みモードの間は更新しない（明けてから）。書き込みはすべて Write Coordinator（`writer.run`）を通す。

## 署名
- 公開鍵（Ed25519・raw の base64）は `backend/qnapnewshub/release_key.py`。秘密鍵はリポジトリに入れない（Release を作る人の手元）。
- 署名の対象は manifest.json のバイト列。manifest は `{"name":"QnapNewsHub","version":"…","sha256":"…","size":…}`（キーを並べ替えた 1 行）。署名は raw 64 バイトの base64。
- 鍵を替えるとき: 新しい公開鍵を入れた版を**古い鍵で署名して**出し、次の版から新しい鍵で署名する（古い版は新しい鍵を知らないと検査できないため）。

## Release の作り方
```
# 1. 版を上げてコミット（backend/qnapnewshub/__init__.py・backend/pyproject.toml・frontend/package.json〔と package-lock.json〕・packaging/qpkg/qpkg.cfg）
# 2. ノート（日本語。更新の方法・前の版への戻し方・確かめていないこと）を ~/ に書く
./hub.sh release 0.1.2 --notes ~/notes-0.1.2.md              # 試験版は --prerelease、台本の確認だけは --dry-run
```
`./hub.sh release <版>` は、版の書き換えの確認（3 か所が指定の版と同じ・作業ツリーがきれい）→ `./hub.sh test` → build → qpkg → 署名 → `gh release create` を順に行う（途中で不合格なら止まる）。秘密鍵は環境変数 `NEWSHUB_SIGNING_KEY`（既定 `~/.keys/qnapnewshub-release.pem`）から**読むだけ**で、中身は出力しない。署名のあと、リポジトリの公開鍵で検証し、対でなければ失敗にする。`--target` は付けないので、Release の元になるコミットは push・マージ済みの既定ブランチ。

## 試験
- `./hub.sh test`: `backend/tests/test_updater.py`（版の比較・試験版・署名の正誤・manifest の改ざん・sha256／大きさ／名前／版の不一致・https とホストの制限・リダイレクトの各段・大きさの上限・ETag・お休み中・Linux・管理者だけ・last.json・切り離した子の台本）。
- `./hub.sh e2e`: `frontend/e2e/update.spec.ts`（設定の更新の節・確認の画面・結果・ホームの知らせ・☰ の印・診断）。
- QTS の模擬: `./hub.sh qpkg && packaging/qpkg/test/run-sim.sh update`。0.1.0 相当の古い QPKG（`NEWSHUB_QPKG_VERSION=0.1.0` で版だけ書き換えて作る。`dist/sim-old/`）を入れ、試験用の一時の鍵で署名した模擬の Release から API 経由で更新する。署名の違う Release は入れないこと・新しい版で起き直すこと・データが残ることを確かめる。
  - 試験用の差し替え: `NEWSHUB_UPDATE_TEST_FEED`（Release の一覧 `releases.json` と取得物のディレクトリ）・`NEWSHUB_UPDATE_TEST_PUBKEY`（試験用の公開鍵）。**`NEWSHUB_INSTALL=qpkg-sim` のときだけ効き**、本番の `qpkg` では無視する。起動台本は `QNH_INSTALL`・`QNH_UPDATE_TEST_FEED`・`QNH_UPDATE_TEST_PUBKEY` で渡す。検査の中身（署名・sha256 など）は本番と同じ。

## 確かめていないこと
- 本物の GitHub の Release からの取得（まだ Release が無い。確認は API の読み取りだけ試した）・QNAP 実機での入れ替え（QTS の App Center と同じ経路か）・実機での更新中の時間。
- `setsid` 相当は `start_new_session`（Python）で行う。QTS の busybox の挙動は模擬でしか見ていない。
