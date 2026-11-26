"""Actual service lifecycle with a public benchmark excerpt in an isolated test DB."""
from dataclasses import replace
from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from qnapnewshub import ztext
from qnapnewshub.db import Base, ArticleEntity, Document, Entity, NewsBody, NewsDocument, NewsItem, Measurement
from qnapnewshub.measure_matrix_bridge import verified_matrix_drafts
from qnapnewshub.measure_service import preview, apply_in_transaction, search_saved_news
from qnapnewshub.measure_store import save_draft
from qnapnewshub.measure_vocab import normalize_condition

# Selected published table; source blob 8ae3b7dfbff78e89b14b319db43562b7c4f55b15.
SOURCE = """| Model | File | Fits 8 GB? | ngl | ROCm pp512 | ROCm tg128 | Vulkan pp512 | Vulkan tg128 |
|---|---:|:--:|--:|---:|---:|---:|---:|
| Qwen2.5-3B  | 1.79 GiB | ✅ full | 99 | 2581 t/s | 86.6 t/s | 2089 t/s | **104.9 t/s** |
| Qwen2.5-7B¹ | 4.36 GiB | ✅ full | 99 | 1302 t/s | 52.1 t/s | 1000 t/s | 54.1 t/s |
| Qwen2.5-14B | 8.37 GiB | ⚠️ partial | 44 | 584 t/s | 18.9 t/s | 417 t/s | 19.2 t/s |
"""


def test_matrix_spans_and_fences():
    source = "日本語😀\r\n\r\n"+SOURCE.replace("\n","\r\n")
    drafts = verified_matrix_drafts(source)
    assert len(drafts)==12
    for d in drafts:
        assert all(s.valid(source) for s in [d.subject,d.value,d.unit]+[c.span for c in d.conditions])
    d = next(d for d in drafts if d.value.text=="104.9")
    assert d.unit.text=="t/s"
    assert {c.key:c.span.text for c in d.conditions} == {"backend":"Vulkan","gen_tokens":"128","offload":"99"}
    assert not verified_matrix_drafts("~~~md\n"+SOURCE+"~~~\n")
    assert normalize_condition("offload","-1") != normalize_condition("offload","1")
    assert normalize_condition("offload","0") == "0"


def test_real_excerpt_confirm_search_revise_and_reject():
    engine=create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        doc=Document(kind="news",ident="url:matrix-test",sensitivity="public",ai_use="allowed")
        art=NewsItem(kind="news",source="test",source_key="test",title="Benchmark",
                     url="https://example.test/matrix",published_at=datetime.now(timezone.utc))
        entities=[Entity(key=f"qwen-{n}",name=f"Qwen2.5-{n}",type="model",sensitivity="public",ai_use="allowed") for n in ("3B","7B","14B")]
        db.add_all([doc,art,*entities]);db.flush()
        body=NewsBody(item_id=art.id,state=1,raw_len=len(SOURCE.encode()),body=ztext.pack(SOURCE))
        db.add_all([NewsDocument(news_item_id=art.id,document_id=doc.id),body,
                    *[ArticleEntity(article_id=art.id,entity_id=e.id) for e in entities]])
        db.flush()
        p=preview(db,doc.id)
        assert p["count"]==8 # 7B footnote label cannot identify the entity.
        assert db.scalar(select(func.count()).select_from(Measurement))==0
        result=apply_in_transaction(db,doc.id,p["doc_hash"],expected_input_hash=p["input_hash"])
        assert len(result["ids"])==8
        assert preview(db,doc.id)["unchanged"]
        args=dict(document_ids=[doc.id],entity_id=entities[0].id,metric_id="llm.decode_tps",
                  filters={"backend":"Vulkan","gen_tokens":"128","offload":"99"})
        rows=search_saved_news(db,**args)["items"]
        assert rows[0]["status"]=="matching" and rows[0]["value"]==104.9
        assert not search_saved_news(db,**{**args,"entity_id":entities[1].id})["items"]
        d=next(d for d in verified_matrix_drafts(SOURCE) if d.value.text=="104.9")
        with pytest.raises(ValueError,match="unverified matrix relation"):
            save_draft(db,doc,SOURCE,replace(d,conditions=()),entity=entities[0])
        with pytest.raises(ValueError,match="subject identity"):
            save_draft(db,doc,SOURCE,d,entity=entities[1])
        new=SOURCE.replace("104.9","105.9")
        body.body=ztext.pack(new);body.raw_len=len(new.encode());db.flush()
        assert not search_saved_news(db,**args)["items"]
        assert apply_in_transaction(db,doc.id,p["doc_hash"],expected_input_hash=p["input_hash"])["status"]=="changed_or_unavailable"
        p2=preview(db,doc.id)
        result2=apply_in_transaction(db,doc.id,p2["doc_hash"],expected_input_hash=p2["input_hash"])
        assert len(result2["ids"])==8
        assert all(db.get(Measurement,i).status=="superseded" for i in result["ids"])
        assert search_saved_news(db,**args)["items"][0]["value"]==105.9
        doc.ai_use="local_only";db.flush()
        assert not search_saved_news(db,**args)["items"]


def test_legacy_offload_fold_is_not_a_match():
    from qnapnewshub.db import MeasurementCond
    from qnapnewshub.measure_search import search
    engine=create_engine("sqlite://")
    Base.metadata.create_all(engine)
    digest="a"*64
    with Session(engine) as db:
        doc=Document(kind="news",ident="url:legacy-offload",sensitivity="public",ai_use="allowed")
        ent=Entity(key="legacy",name="Legacy",type="model",sensitivity="public",ai_use="allowed")
        db.add_all([doc,ent]);db.flush()
        m=Measurement(doc_id=doc.id,doc_hash=digest,entity_id=ent.id,subject_text="Legacy",
                      metric_id="llm.decode_tps",value=42,value_text="42",unit="tok/s",unit_text="tok/s",
                      sensitivity="public",ai_use="allowed",extractor="test",fingerprint="b"*64)
        db.add(m);db.flush()
        db.add(MeasurementCond(measurement_id=m.id,key="offload",value_text="-1",value_norm="1",
                               scope="explicit",span_start=0,span_end=2,extractor="test",confidence="exact"))
        db.flush()
        args=dict(entity_id=ent.id,metric_id=m.metric_id,scopes={"public"},current_hashes={doc.id:digest})
        assert search(db,**args,filters={"offload":"1"})[0]["status"]=="other"
        assert search(db,**args,filters={"offload":"-1"})[0]["status"]=="matching"
        with pytest.raises(ValueError):
            search(db,**args,filters={"offload":"all"})
