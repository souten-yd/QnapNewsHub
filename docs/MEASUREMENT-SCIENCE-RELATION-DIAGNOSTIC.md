# MeasEval relation linking diagnostic (Draft)

Eight additional paragraphs from the official [MeasEval eval split](https://github.com/harperco/MeasEval/tree/1fa738b6bc9b72c84c88a80344ca3ab39a310a44/data/eval), distinct from the nine used in #57. Both text and TSV blobs pinned in script. Selected from available public search results, not random or representative. Exact offsets and annotation IDs checked. Source text not copied into repo.

Oracle span setting: all human-annotated Quantity, MeasuredEntity, MeasuredProperty and Qualifier spans are supplied as inputs. Duplicate nodes and gold links deduplicated by type and character offsets; annotation groups and target IDs used only to construct gold. Predictor does not consume gold group or relation IDs. It selects one type-compatible target per source span, using interval gap, midpoint distance, then deterministic position/type ties. Second baseline prioritizes same sentence using a simple punctuation heuristic. Neither baseline extracts entities or quantities from raw text; this is not end-to-end F1. One-target assumption can lose multi-target relations.

|Method|Gold edges|Pred edges|Exact correct|Precision|Recall|F1|
|---|---:|---:|---:|---:|---:|---:|
|nearest|27|25|20|.8000|.7407|.7692|
|sentence first|27|25|20|.8000|.7407|.7692|

|Type|Gold|Pred|Correct|
|---|---:|---:|---:|
|HasQuantity|16|18|15|
|HasProperty|9|5|5|
|Qualifies|2|2|0|

The sentence heuristic did not change performance in this small sample. Even perfect entity/quantity spans did not solve relation linking. Do not infer relation correctness from span overlap. Qualifier links need special attention; two examples are insufficient for a general conclusion.

Next: permit multiple typed candidate links with bounded graph cardinality, use syntax/units/property dictionaries as features and compare local model predictions; validate evidence offsets, ambiguity/abstention, relation direction and repeated measurements. Freeze candidate and run on disjoint source IDs with sufficient qualifier examples. Measure span, normalized value/unit, relation and whole-record correctness separately. Keep unknown relationships unknown; do not publish uncertain measurements into the production DB.

Executed JS evaluate function against actual source blobs; local Node CLI wrapper not executed because container download unavailable. Reproduce: node backend/tools/eval_measure/eval_science_relations.mjs /path/to/MeasEval. No runtime, database, UI, API, network, execution or default changes; do not merge.
