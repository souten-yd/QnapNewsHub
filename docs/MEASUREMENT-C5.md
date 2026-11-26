# C5 条件検索（初期実装）

内部用 search は認可済み scopes・entity_id・metric_id・現在本文の hash を受ける。
一致／不明／不一致を欄別に返し、測定単位で並べる。現行文書の区分・AI利用禁止・hash不一致を除外する。
API・LLMコンテキスト・旧claimsとの互換統合、正本本文hash取得、1000件超のランキング改善は未実装。
既存の /metrics/compare は変更しない。回帰試験未実行。
