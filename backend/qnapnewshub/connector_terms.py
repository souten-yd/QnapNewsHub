"""connector の利用条件の記録（v0.7。2026-11-25 に公式の資料を読んで確かめた）。

- status: confirmed（公式の資料で確かめた）／partial（一部だけ確かめた・条件つき）／per_site（取得先のサイトごとに違う。robots.txt を守る）／unconfirmed（公式の資料を読めなかった・分からない）。
- **unconfirmed の connector は既定で切**。入れるには利用者が「利用条件が未確認だと分かって入れる」と明示する（connectors の terms_ack）。
- redistribution: allowed／conditional／forbidden／unknown。ここでの「再配布」は、ニュースハブから外（ControlDeck 以外）へ出すこと。ニュースハブは書誌（メタデータ）だけ保存し、論文・コードの本体は保存しない。
- ai_use: その connector が入れる文書の ai_use（allowed なら LLM に渡してよい）。条件が分からないものは local_only。
- 値は公式の資料に書いてあったことだけ。読めなかったものは「未確認」と書く（推測しない）。レートは記載の数字（2026-11-25 時点。変わりうる）。
"""
from __future__ import annotations

CHECKED = "2026-11-25"

TERMS: dict[str, dict] = {
    "feed": {
        "status": "per_site", "license": "取得先のサイトごと（RSS の公開は配信の意思表示。記事の著作権は各サイト）", "storage": "題・抜粋・URL だけ保存（本文は取り出せる記事だけ。robots.txt を守る）",
        "redistribution": "conditional", "rate": "サイトごと（既定は取得先ごとの間隔・同じホストへ 1 秒以上あける）", "tos_url": "https://www.rfc-editor.org/rfc/rfc9309.html",
        "notes": "RSS・Atom・一覧の巡回・Steam・フォーラム・Codeberg など。有料の記事・ロボット確認はすり抜けない。", "ai_use": "allowed"},
    "github": {
        "status": "confirmed", "license": "データ全体の単一のライセンスは無い。各リポジトリの内容は各リポジトリのライセンス", "storage": "公開メタデータ（Release の題・本文・URL）の保存は可。個人情報の再利用・スパム・再販は不可",
        "redistribution": "conditional", "rate": "認証なし 60 回/時・トークンあり 5,000 回/時（REST）", "tos_url": "https://docs.github.com/en/site-policy/github-terms/github-terms-of-service",
        "notes": "Release の差分（既存の取得を載せ替え）。上限（403・429）のときは releases.atom に切り替える。鍵は任意。", "ai_use": "allowed"},
    "github_discover": {
        "status": "confirmed", "license": "同上（GitHub の利用規約・Acceptable Use Policies。研究目的の公開の非個人情報の収集は、成果が open access の場合に限る旨がある）",
        "storage": "リポジトリの名前・説明・スター数・言語・トピックなど公開メタデータだけ保存。コードは取らない", "redistribution": "conditional",
        "rate": "検索 API: 認証なし 10 回/分・トークンあり 30 回/分。1 回の検索は最大 1,000 件・1 ページ 100 件。REST は上の通り", "tos_url": "https://docs.github.com/en/rest/search/search",
        "notes": "副次制限（同時リクエストなど）は予告なく変わる。403・429 の x-ratelimit-reset・retry-after を守る。トークンを共有してレートを回避しない。", "ai_use": "allowed"},
    "huggingface": {
        "status": "partial", "license": "モデル・データセットごとに別のライセンス（利用規約はコンテンツのライセンスを尊重する趣旨）", "storage": "メタデータ（名前・タグ・ダウンロード数）だけ。重みは取らない",
        "redistribution": "unknown", "rate": "API: 匿名 500 回/5 分・無料アカウント 1,000 回/5 分（2025-09 時点の表）", "tos_url": "https://huggingface.co/terms-of-service",
        "notes": "メタデータ単独の再利用を明示した条項は未確認。話題の GGUF（既存の取得を載せ替え）。鍵は任意。", "ai_use": "allowed"},
    "hf_models": {
        "status": "partial", "license": "同上（モデルごとに別。ゲート付きは同意・承認が要る）", "storage": "メタデータ（名前・作者・タグ・派生の関係・量子化の種類）だけ。重みは取らない",
        "redistribution": "unknown", "rate": "同上（匿名 500 回/5 分）。超過は 429", "tos_url": "https://huggingface.co/terms-of-service",
        "notes": "メタデータ単独の再利用を明示した条項は未確認。スパム・過剰な一括操作は禁止（content policy）。", "ai_use": "allowed"},
    "arxiv": {
        "status": "confirmed", "license": "記述的なメタデータは CC0 1.0。論文本体は著作権の対象（許可なしの再配布は不可）", "storage": "メタデータ（題・要旨・ID）の保存・変換・共有は可。本体の PDF は保存・配信しない",
        "redistribution": "allowed", "rate": "API・OAI-PMH・RSS は 3 秒に 1 回以下・同時 1 接続（合算）。鍵は不要", "tos_url": "https://info.arxiv.org/help/api/tou.html",
        "notes": "RSS の記事（既存の取得を載せ替え）に加え、論文の文書（L1）を arXiv ID で作る。arXiv 公認を装わない。", "ai_use": "allowed"},
    "openalex": {
        "status": "confirmed", "license": "すべての記録は CC0（パブリックドメイン）", "storage": "書誌・引用の数の保存・再配布は可", "redistribution": "allowed",
        "rate": "鍵なしでも基本のクエリは可。無料の鍵は 1 日 $1 の予算（鍵なしの 10 倍）。秒間: 鍵なし 10 回・無料の鍵 30 回。1 ページ最大 100 件", "tos_url": "https://help.openalex.org/api/authentication/",
        "notes": "429 は指数バックオフ。カーソルによる深いページ送りは無料の鍵が要る（この版は浅いページだけ）。mailto の礼儀プールは現行の資料に記述が見つからず未確認（メールは送らない）。", "ai_use": "allowed"},
    "crossref": {
        "status": "confirmed", "license": "メタデータはほぼ著作権の対象でなく、目的を問わず使える（一部の要旨は出版社・著者の著作権）", "storage": "書誌の保存は可。要旨は保存しない", "redistribution": "allowed",
        "rate": "Public: DOI の検索 5 回/秒（同時 1）・クエリ 1 回/秒（同時 1）。Polite: 10 回/秒・3 回/秒", "tos_url": "https://www.crossref.org/documentation/retrieve-metadata/rest-api/",
        "notes": "DOI の解決だけ（1 件ずつ・Public プール）。Polite プールにはメールアドレスを書く必要があるが、公開のリポジトリに入れないため使わない。キャッシュする。", "ai_use": "allowed"},
    "semanticscholar": {
        "status": "partial", "license": "データは付随のライセンスに従う（CC BY-NC・ODC-BY など）。出典として Semantic Scholar を明記する", "storage": "引用の辺・題・ID・引用数だけ。本文は取らない",
        "redistribution": "conditional", "rate": "鍵なし: 全利用者で共有 1,000 回/秒（混むと絞られる）。鍵: 導入時 1 回/秒", "tos_url": "https://www.semanticscholar.org/product/api/license",
        "notes": "API 自体の再配布・転売は禁止。データの商用の可否は未確認（CC BY-NC を含む）。AI2 はいつでも理由なく停止できる。この版は個人の研究の用途だけで使う（外へ再配布しない）。",
        "ai_use": "local_only"},
    "hn": {
        "status": "unconfirmed", "license": "未確認（公式の文書で、データの利用規約を読めなかった）", "storage": "未確認（既存の取得は題・URL・点数だけ保存）", "redistribution": "unknown",
        "rate": "未確認（Algolia の API のページは本文を読めなかった。公式の Firebase API は「現在は制限なし」）", "tos_url": "https://hn.algolia.com/api",
        "notes": "既存の取得（トップページ・チャンネルの語の検索）を載せ替えたもの。新しく増やした取得ではないので既定は入のまま。HN の FAQ は短時間に多数のページを取ると IP が止められうると書いている。",
        "ai_use": "local_only"},
    "hn_search": {
        "status": "unconfirmed", "license": "未確認", "storage": "未確認（題・URL・点数の L0 だけ保存）", "redistribution": "unknown", "rate": "未確認（控えめに 1 回の問いで 1 リクエスト・1 日 100 件まで）",
        "tos_url": "https://hn.algolia.com/api", "notes": "問いに応じて 1 回ずつ引く道具。常に全部は取らない。利用条件が未確認なので既定は切。", "ai_use": "local_only"},
    "sitemap": {
        "status": "per_site", "license": "取得先のサイトごと（sitemap は公開の意思表示だが、権利は各サイト）", "storage": "URL・日付・URL から作った題だけ（L0）。本文は取らない",
        "redistribution": "unknown", "rate": "サイトごと（既定は 1 サイト 1 日 1 回・同じホストへ 1 秒以上あける）", "tos_url": "https://www.sitemaps.org/protocol.html",
        "notes": "robots.txt（RFC 9309）の Disallow に従う。sitemap は 1 ファイル 5 万 URL・50MB まで。robots.txt が読めないときは取らない。", "ai_use": "local_only"},
    "change": {
        "status": "per_site", "license": "取得先のサイトごと", "storage": "ページの hash・ETag・変化した時刻だけ（本文は保存しない）", "redistribution": "unknown",
        "rate": "サイトごと（条件付きの GET・1 ページ 15 分に 1 回まで）", "tos_url": "https://www.rfc-editor.org/rfc/rfc9309.html",
        "notes": "利用者が指定したページだけ。robots.txt に従い、各サイトの利用規約は利用者が確かめる。", "ai_use": "local_only"},
    # ---- v0.8（2026-11-25 に公式の資料を読んだ範囲。読めなかった所は「未確認」と書く） ----
    "boj": {
        "status": "confirmed", "license": "日本銀行の「API 機能利用時の留意点」に従う（データそのものの二次利用の条項は、この留意点では読めていない）",
        "storage": "指定した系列の数値（系列コード・日付・値・単位）だけ保存", "redistribution": "conditional",
        "rate": "数値の上限は明記なし。短時間の高頻度のアクセスは禁止（遮断・停止される）。1 回の問いは 250 系列まで。gzip 対応", "tos_url": "https://www.stat-search.boj.or.jp/info/api_notice.pdf",
        "notes": "サービスを公開するときは「このサービスは、日本銀行時系列統計データ検索サイトの API 機能を使用しています。サービスの内容は日本銀行によって保証されたものではありません。」を表示し、調査統計局へ知らせる（留意点 1・2）。予告なく停止・制限されうる。ニュースハブは個人用で公開しない。", "ai_use": "allowed"},
    "kokkai": {
        "status": "partial", "license": "データベースの著作権は国立国会図書館。国会議員など発言者の発言は各発言者の著作物（転載には原則として許可が要る）", "storage": "発言の冒頭（要旨）・話者・院・会議・日付・URL だけ保存。全文は保存しない",
        "redistribution": "conditional", "rate": "登録不要・無料。数秒あけて直列に取る（並列は不可。多用するとブロックされうる）。1 回 1〜100 件・検索条件は 2,000 バイトまで", "tos_url": "https://kokkai.ndl.go.jp/api.html",
        "notes": "API の仕様のページと NDL の転載のページを読んだ。国立国会図書館ウェブサイトの利用規約本文のうち会議録に特有の条項は読めていない。外へ再配布せず個人の研究の用途にとどめる。", "ai_use": "local_only"},
    "egov_law": {
        "status": "unconfirmed", "license": "未確認（e-Gov 法令検索の利用規約は項目の見出しだけ読めた。本文の条件を読めなかった）", "storage": "法令の版の履歴（法令 ID・版 ID・公布日・施行日・改正法令の名称）だけ保存。条文は保存しない",
        "redistribution": "unknown", "rate": "未確認（アクセス頻度の制限が規約にあると見出しにある。控えめに 1 秒以上あける）", "tos_url": "https://laws.e-gov.go.jp/terms/",
        "notes": "法令 API v2（/api/2/law_revisions）。利用規約の本文を読めなかったので既定は切。入れるには未確認と分かって入れる確認が要る。", "ai_use": "local_only"},
    "estat": {
        "status": "unconfirmed", "license": "未確認（API 機能の利用規約の本文を読めなかった。仕様書では appId〔利用登録で取得〕が全 API で必須と確認した）", "storage": "指定した統計表の数値（表 ID・時点・分類・値・単位）だけ保存",
        "redistribution": "unknown", "rate": "未確認（1 回の件数は limit で指定。省略時は 10 万件）", "tos_url": "https://www.e-stat.go.jp/api/",
        "notes": "鍵（appId）が要る。鍵が無いと動かない・既定は切。利用規約を読めていないため、入れるには未確認と分かって入れる確認も要る。", "ai_use": "local_only"},
    "edinet": {
        "status": "unconfirmed", "license": "未確認（API 仕様書・利用規約を読めなかった）", "storage": "提出書類の知らせ（書類 ID・提出者名・書類の種類・提出日時）だけ保存。書類の本体は取らない", "redistribution": "unknown",
        "rate": "未確認（控えめに 1 秒以上あける）", "tos_url": "https://disclosure2.edinet-fsa.go.jp/",
        "notes": "鍵（Subscription-Key）が要る。鍵が無いと動かない・既定は切。仕様書を読めなかったため、入れるには未確認と分かって入れる確認も要る。", "ai_use": "local_only"},
    "defillama": {
        "status": "partial", "license": "未確認（無料のホスト api.llama.fi・stablecoins.llama.fi は鍵を読まず、レート制限がある、とドキュメントにある。出典表示・再配布の条項は読めなかった）", "storage": "TVL・ステーブルコインの供給量などの公開の数値だけ",
        "redistribution": "unknown", "rate": "無料のホストにはレート制限がある（数値は読めなかった）。1 日 1 回・同時 1 本で足りる量にとどめる", "tos_url": "https://api-docs.defillama.com/",
        "notes": "無料の範囲だけ（Pro API は使わない）。出どころは DefiLlama と書く。RWA は protocols の category が RWA のもの（RWA トークン）。デジタル証券は別の種類で、この取得では作らない。", "ai_use": "local_only"},
    "coingecko": {
        "status": "partial", "license": "未確認（Demo API は無料で、エンドポイントとレートが限られる、とドキュメントにある。出典表示の条件・数値は読めなかった）", "storage": "価格（通貨・値・取得時刻）だけ",
        "redistribution": "unknown", "rate": "未確認（Demo は少ない。1 日 1 回・少数の銘柄にとどめる）", "tos_url": "https://docs.coingecko.com/reference/introduction",
        "notes": "無料の Demo の鍵があるときだけ動く・既定は切。鍵は x-cg-demo-api-key で送る。出どころは CoinGecko と書く。", "ai_use": "local_only"},
    "gdelt": {
        "status": "partial", "license": "公開データセットは学術・商用・政府を含め無料で無制限に使える。再配布・再公開もできるが、GDELT Project への引用と公式サイトへのリンクが要る（about ページ）", "storage": "記事の URL・題・ドメイン・言語・国・見た日時だけ（L0）。本文は取らない",
        "redistribution": "allowed", "rate": "未確認（DOC API のレートは読めなかった。控えめに 6 秒に 1 回・1 問い 1 リクエスト）", "tos_url": "https://www.gdeltproject.org/about.html",
        "notes": "語の検索だけ。結果は機械の抽出で、事実の確認はされていない（未確認の印をつけて L0 で保存）。出どころは GDELT Project と書く。", "ai_use": "allowed"},
    "pypi": {
        "status": "unconfirmed", "license": "未確認（JSON API のドキュメントにレート・利用条件の記載が無かった）", "storage": "監視するパッケージの版・公開日だけ", "redistribution": "unknown",
        "rate": "未確認（1 パッケージ 1 リクエスト・1 日 1 回）", "tos_url": "https://docs.pypi.org/api/json/",
        "notes": "/pypi/<名前>/json。大きい応答がありうるので通信量の予算で止める。", "ai_use": "local_only"},
    "npm": {
        "status": "unconfirmed", "license": "未確認（registry API の文書にレート・利用条件の記載が無かった）", "storage": "監視するパッケージの版（dist-tags）だけ", "redistribution": "unknown",
        "rate": "未確認（1 パッケージ 1 リクエスト・1 日 1 回）", "tos_url": "https://github.com/npm/registry/blob/main/docs/REGISTRY-API.md",
        "notes": "省略形のメタデータ（application/vnd.npm.install-v1+json）を使い、応答を小さくする。公開日時は取れない（版の印だけ）。", "ai_use": "local_only"},
    "osv": {
        "status": "partial", "license": "未確認（データのライセンスは読めなかった。取り込み元のデータベースごとに違うと考えられる）", "storage": "脆弱性の ID・要約・公開／更新日・別名・深刻度・修正済みの版だけ",
        "redistribution": "unknown", "rate": "API に上限は無いとドキュメントにある（FAQ）。1 パッケージ 1 リクエスト・1 日 1 回", "tos_url": "https://google.github.io/osv.dev/faq/",
        "notes": "POST /v1/query（パッケージ名＋エコシステム。大文字小文字を区別）。ページング（next_page_token）は 1 ページだけ読む。", "ai_use": "local_only"},
}

for _id, _t in TERMS.items():
    _t["checked_at"] = CHECKED
    _t["id"] = _id
