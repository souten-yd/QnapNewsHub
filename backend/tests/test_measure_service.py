"""目的: C5b 正本とwriter・条件付き検索の境界。外部通信なし、合成DBのみ。"""
import hashlib
from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from qnapnewshub import ztext
from qnapnewshub.db import Base, ArticleEntity, Document, Entity, NewsBody, NewsDocument, NewsItem, Measurement, MeasurementCond
from qnapnewshub.measure_service import preview, apply_in_transaction, search_saved_news, load_source
from qnapnewshub.measure_search import search


def test_authoritative_news_service():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    source = "Foo decode 60 tok/s on RTX 4070."
    with Session(engine) as db:
        doc = Document(kind="news", ident="url:test", sensitivity="public", ai_use="allowed")
        ent = Entity(key="foo", name="Foo", type="model", sensitivity="public", ai_use="allowed")
        art = NewsItem(kind="news", source="test", source_key="test", title="Foo",
                       url="https://example.test/foo", published_at=datetime.now(timezone.utc))
        db.add_all([doc, ent, art])
        db.flush()
        body = NewsBody(item_id=art.id, state=1, raw_len=len(source.encode()), body=ztext.pack(source))
        db.add_all([NewsDocument(news_item_id=art.id, document_id=doc.id),
                    ArticleEntity(article_id=art.id, entity_id=ent.id), body])
        db.flush()
        p = preview(db, doc.id)
        assert p["status"] == "preview" and p["count"] == 1
        assert p["doc_hash"] == hashlib.sha256(source.encode()).hexdigest()
        assert db.scalar(select(func.count()).select_from(Measurement)) == 0
        assert apply_in_transaction(db, doc.id, "0"*64)["status"] == "changed_or_unavailable"
        result = apply_in_transaction(db, doc.id, p["doc_hash"])
        assert len(result["ids"]) == 1
        assert apply_in_transaction(db, doc.id, p["doc_hash"]) == result
        args = dict(document_ids=[doc.id], entity_id=ent.id, metric_id="llm.decode_tps",
                    filters={"hw":"RTX 4070"})
        assert search_saved_news(db, **args)["items"][0]["status"] == "matching"
        body.body = ztext.pack(source.replace("60", "80"))
        db.flush()
        assert search_saved_news(db, **args)["items"] == []
        assert apply_in_transaction(db, doc.id, p["doc_hash"])["status"] == "changed_or_unavailable"
        body.raw_len = 1
        db.flush()
        assert load_source(db, doc.id) is None
        body.raw_len = len(source.encode())
        art.user_id = 1
        db.flush()
        assert load_source(db, doc.id) is None
        art.user_id = None
        doc.ai_use = "local_only"
        db.flush()
        assert load_source(db, doc.id) is None


def test_rank_before_limit_and_scope():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    digest = "a"*64
    with Session(engine) as db:
        doc = Document(kind="news", ident="url:test", sensitivity="public", ai_use="allowed")
        ent = Entity(key="foo", name="Foo", type="model", sensitivity="public", ai_use="allowed")
        db.add_all([doc, ent])
        db.flush()
        rows = [Measurement(doc_id=doc.id, entity_id=ent.id, doc_hash=digest,
                            subject_text="Foo", metric_id="llm.decode_tps", value=60,
                            value_text="60", unit="tok/s", unit_text="tok/s", fingerprint=f"{i:064x}",
                            sensitivity="public", ai_use="allowed", extractor="test")
                for i in range(1203)]
        db.add_all(rows)
        db.flush()
        db.add_all([MeasurementCond(measurement_id=r.id, key="hw",
                                    value_text="RTX 4070" if i == 0 else "RTX 4090",
                                    value_norm="rtx4070" if i == 0 else "rtx4090",
                                    scope="explicit", span_start=0, span_end=8,
                                    extractor="test", confidence="exact")
                    for i,r in enumerate(rows[:-1])])
        db.flush()
        args = dict(entity_id=ent.id, metric_id="llm.decode_tps", filters={"hw":"RTX 4070"},
                    scopes={"public"}, current_hashes={doc.id:digest}, limit=1)
        assert search(db, **args)[0]["id"] == rows[0].id
        assert search(db, **{**args,"filters":{"hw":"RTX 4070","quant":"Q4"}})[0]["id"] == rows[0].id
        assert search(db, **{**args,"current_hashes":{doc.id:"b"*64}}) == []
        doc.ai_use = "local_only"
        db.flush()
        assert search(db, **args) == []
        assert search(db, **args, local_only=True) == []  # stale measurement and entity permissions
        with pytest.raises(ValueError):
            search(db, **{**args,"limit":0})
