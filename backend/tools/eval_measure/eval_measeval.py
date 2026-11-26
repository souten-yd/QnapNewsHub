"""MeasEval external annotated scientific text: evaluate scalar prefilter only.

Run: cd backend && python -m tools.eval_measure.eval_measeval /path/to/MeasEval
No download, no document content persisted, no LLM. Source/TSV Git blob SHA
pinned for the initial four evaluation documents; do not silently substitute.
This is quantity coverage and token containment, NOT full extraction F1.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
from qnapnewshub.measure_quantity import scalar_tokens, normalized

SAMPLES = {
    "S0012821X12004384-1610": ("7fd8ce6b96084c3f6c67d1334cf88aee5e0f8ff4","deca8faeb58ea27ce3df6474b3a1e898b085fe5a"),
    "S0012821X12004384-990": ("e113e5abac506806ddbd42af25f9ec95f8010ec8","1efa64dadbefd3ea45e31569fb69ae245bb270a6"),
    "S0012821X13007309-1649": ("cb5653bbd3dae754dbe0fc4d4bb320796cca961d","7a053bdef29afe59e733554cf7a45444a9e15765"),
    "S0016236113008041-3127": ("1a5982fad083d2010466caaffc402ca553e96960","11e1343c2494b528b2be97b34ca46c44ccfafd75"),
}


def read_verified(path, expected):
    if path.stat().st_size > 1024*1024:
        raise ValueError("sample too large")
    data=path.read_bytes()
    blob=hashlib.sha1(b"blob "+str(len(data)).encode()+b"\0"+data).hexdigest()
    if blob != expected:
        raise ValueError("source blob mismatch: "+path.name)
    return data.decode("utf-8")


def evaluate(root):
    results=[]
    for doc_id,(text_sha,tsv_sha) in SAMPLES.items():
        source=read_verified(root/"data"/"eval"/"text"/(doc_id+".txt"),text_sha)
        annotations=read_verified(root/"data"/"eval"/"tsv"/(doc_id+".tsv"),tsv_sha)
        gold=[]
        for row in csv.DictReader(io.StringIO(annotations),delimiter="\t"):
            if row["annotType"]!="Quantity":
                continue
            a,b=int(row["startOffset"]),int(row["endOffset"])
            if not 0 <= a < b <= len(source) or source[a:b]!=row["text"]:
                raise ValueError("annotation offset mismatch")
            gold.append((len(normalized(source[:a])),len(normalized(source[:b]))))
        pred=[m.span() for m in scalar_tokens(source)]
        contains=lambda s,e,a,b:a<=s<e<=b
        results.append({"doc_id":doc_id,"tokens":len(pred),
                        "contained":sum(any(contains(s,e,a,b) for a,b in gold) for s,e in pred),
                        "quantities":len(gold),"covered":sum(any(contains(s,e,a,b) for s,e in pred) for a,b in gold)})
    totals={key:sum(r[key] for r in results) for key in ("tokens","contained","quantities","covered")}
    return {"mode":"scalar_prefilter_only","cases":results,"totals":totals,
            "token_containment_precision":totals["contained"]/totals["tokens"] if totals["tokens"] else 0,
            "quantity_coverage":totals["covered"]/totals["quantities"] if totals["quantities"] else 0}


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("root",type=Path)
    args=p.parse_args()
    print(json.dumps(evaluate(args.root),indent=2))
