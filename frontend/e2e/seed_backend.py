"""E2E 用: 一時の置き場（NEWSHUB_DATA）の記事の DB に、見本の記事を入れる。外へ通信しない。利用者は作らない（最初の管理者は画面で作る）。"""
from datetime import datetime, timedelta, timezone

from qnapnewshub import db as news_db
from qnapnewshub import sources, store

now = datetime.now(timezone.utc)
ROWS = [
    ("news", "ggml-news", "llama.cpp の新しい量子化が入りました", "https://news.example.test/a/1", "GPU が無くても動く軽い量子化。速度と精度の比較が公開されました。", "https://img.example.test/1.png", 1),
    ("news", "ggml-news", "画像のない記事: ローカル LLM の運用メモ", "https://news.example.test/a/2", "画像の部分を出さず、細い線だけが付きます。", None, 2),
    ("release", "llama-cpp", "llama.cpp b9000 を公開", "https://github.example.test/r/9000", "新しい量子化と修正。", None, 3),
    ("forum", "forum-a", "フォーラム: 24GB でどこまで動くか", "https://forum.example.test/t/1", "実測の報告が集まっています。", None, 5),
    ("model", "hf", "新しい GGUF モデルが話題に", "https://hf.example.test/m/1", "小さくて速いモデル。", "https://img.example.test/2.png", 8),
    ("news", "paper-b", "NAS で動かす小さなサーバー", "https://news.example.test/a/3", "非力な機器でニュースを集める話。", None, 12),
]
with news_db.session() as db:
    items = [sources.make_item(kind=k, source=src, source_key=src, title=t, url=u, summary=s, image=img, published=now - timedelta(hours=h))
             for k, src, t, u, s, img, h in ROWS]
    store.upsert_items(db, items)
    from sqlalchemy import update
    from qnapnewshub.db import NewsItem

    db.execute(update(NewsItem).where(NewsItem.title.like("画像のない記事%")).values(body_state=1))      # 「本文あり」の印の見本
    db.commit()
print("seeded", len(ROWS))
