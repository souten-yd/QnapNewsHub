# QNAP ニュースハブ（QnapNewsHub）

ずっと電源の入っている QNAP（または Linux の PC）で、ニュースを集める・ためる・読むための小さなサーバーです。

- RSS／Atom・GitHub の Release・arXiv・Steam・記事の一覧の巡回など、数百の取得先を、取得先ごとの間隔で集め続けます（取りこぼしを防ぐため 24 時間）。
- 重複を除いてためておき、全文検索・チャンネル（キーワード）・アーカイブで読めます。画面は放送局ふうほか 5 つのデザイン。
- 無料の外部の LLM（OpenRouter）で、記事の日本語の概要とまとめ（日刊・週刊）を作れます（無くても動きます）。
- [ControlDeck](https://github.com/souten-yd/ControlDeck) とつなぐと、ControlDeck が必要な記事だけを受け取り、本文の訳・画像生成・知識への蓄積などの重い処理をします。

まず Linux で動かし、そのあと QNAP の QPKG として配ります。

- 設計（正）: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- これまでの設計: [docs/DESIGN.md](docs/DESIGN.md)・[docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)
- 計画: [docs/PLAN.md](docs/PLAN.md)
- 無料の LLM の件数の調査: [docs/research-free-llm-batch.md](docs/research-free-llm-batch.md)
- 開発の決まり: [AGENTS.md](AGENTS.md)

状態: バックエンド（取得・保存・認証・下ごしらえ・まとめ・API）まで。画面は次の段です。

```
./hub.sh setup           # .venv を作って依存を入れる
./hub.sh test            # 試験
./hub.sh create-admin    # 最初の管理者を作る
./hub.sh run             # 127.0.0.1:8787 で起動（データは NEWSHUB_DATA。既定 ~/.local/share/qnapnewshub）
```
