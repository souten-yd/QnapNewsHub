"""目的: C5 条件の正確な一致と文書区分・原文hashの制約。根拠: MEASUREMENT.md C5。"""
import hashlib
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from qnapnewshub.db import Base, Document, Entity
from qnapnewshub.measure_search import condition_match, search
from qnapnewshub.measure_spans import extract_explicit
from qnapnewshub.measure_store import save_draft


def test_filter_states():
    assert condition_match({"hw": "RTX 4070"}, {"hw": "rtx4070ti"}) == ("other", {"hw": "mismatch"})
    assert condition_match({"hw": "R9700"}, {"hw": "r9700"})[0] == "matching"
    assert condition_match({"hw": "R9700"}, {})[0] == "unspecified"


def test_search_checks_current_doc_hash_and_scope():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    src = "Foo decode 60 tok/s on RTX 4070."
    digest = hashlib.sha256(src.encode()).hexdigest()
    with Session(engine) as db:
        doc = Document(kind="news", ident="url:test", sensitivity="public", ai_use="allowed")
        ent = Entity(key="foo", name="Foo", type="model", sensitivity="public", ai_use="allowed")
        db.add_all([doc, ent])
        db.flush()
        save_draft(db, doc, src, extract_explicit(src, "Foo")[0], entity=ent)
        args = dict(entity_id=ent.id, metric_id="llm.decode_tps", filters={"hw": "RTX 4070"},
                    scopes={"public"}, current_hashes={doc.id: digest})
        assert search(db, **args)[0]["status"] == "matching"
        assert search(db, **{**args, "current_hashes": {doc.id: "wrong"}}) == []
        doc.sensitivity = "confidential"
        db.flush()
        assert search(db, **args) == []
