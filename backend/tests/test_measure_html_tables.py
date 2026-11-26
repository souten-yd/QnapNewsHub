"""C4b: same producer contract as ControlDeck W4a2; synthetic fixture only."""
import copy
import hashlib
import json
import pytest
from qnapnewshub.measure_html_tables import parse_bundle


def cell(row,col,text,header=False,rs=1,cs=1):
    return {"row":row,"col":col,"text":text,"header":header,"rowspan":rs,"colspan":cs,"scope":"","truncated":False}


def fixture():
    cells = [cell(0,0,"Model",True,2),cell(0,1,"GPU",True,2),cell(0,2,"Speed (tok/s)",True,1,2),
             cell(1,2,"Decode",True),cell(1,3,"Prefill",True),
             cell(2,0,"Foo",False,2),cell(2,1,"RTX 4070"),cell(2,2,"60"),cell(2,3,"420"),
             cell(3,1,"RTX 4070 Ti"),cell(3,2,"61"),cell(3,3,"500")]
    return [{"table_index":0,"rows":4,"cols":4,"caption":"","cells":cells}]


def meta(tables):
    return {"schema":"controldeck.html-table.v1","html_sha256":"a"*64,
            "tables_sha256":hashlib.sha256(json.dumps(tables,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()}


def test_merged_table_conditions_and_coordinates():
    t=fixture()
    rows=parse_bundle(t,meta(t))
    assert len(rows)==4
    assert [r["metric_id"] for r in rows]==["llm.decode_tps","llm.prefill_tps"]*2
    assert rows[2]["conditions"]["hw"]=={"text":"RTX 4070 Ti","cell":[0,3,1],"scope":"row"}
    assert rows[2]["evidence"]["subject_cell"]==[0,2,0]
    assert rows[2]["evidence"]["value_cell"]==[0,3,2]
    assert rows[2]["evidence"]["headers"]==[[0,0,2],[0,1,2]]
    assert "span" not in rows[2]["evidence"]


def test_reject_tampered_or_bad_geometry():
    t=fixture()
    m=meta(t)
    t[0]["cells"][-1]["text"]="999"
    with pytest.raises(ValueError):
        parse_bundle(t,m)
    t[0]["cells"][-1]["col"]=2
    with pytest.raises(ValueError):
        parse_bundle(t,meta(t))


@pytest.mark.parametrize("value",["40–60","60±5","-60","1,2","60*","about 60"])
def test_not_scalar(value):
    t=fixture()
    t[0]["cells"][7]["text"]=value
    assert len(parse_bundle(t,meta(t)))==3


def test_ambiguous_condition_and_truncated_cell():
    t=fixture()
    t[0]["cells"][6]["text"]="RTX 4070 or RTX 4090"
    assert len(parse_bundle(t,meta(t)))==2
    t=fixture()
    t[0]["cells"][7]["truncated"]=True
    assert len(parse_bundle(t,meta(t)))==3
