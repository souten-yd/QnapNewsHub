# MeasEval quantity span diagnostic (Draft)

[MeasEval](https://github.com/harperco/MeasEval/tree/1fa738b6bc9b72c84c88a80344ca3ab39a310a44), three train paragraphs and six eval paragraphs. Sampled from visible search results, not random or blinded; all examples were inspected during development, so eval is exploratory, not independent holdout. The full candidate and scorer JS functions executed against nine source blobs. The local Node CLI wrapper was not executed; source access via GitHub connector only. Reproduce: node backend/tools/eval_measure/eval_science_spans.mjs /path/to/MeasEval. Source IDs and blob SHAs are fixed in script. No source text or annotation text persisted. Exact-span precision/recall/F1 are not comparable to previous scalar token containment. Overlap coverage is weaker than exact span recall; neither demonstrates unit/value/modifier/relationship extraction accuracy.

|Split|Gold|Candidates|Exact|Overlap gold coverage|Precision|Recall|F1|
|---|---:|---:|---:|---:|---:|---:|---:|
|train 3|11|11|11|11/11|1|1|1|
|eval 6|17|19|15|17/17|.7895|.8824|.8333|

Observed errors: figure label 2A, material identifier 6H, inconsistent annotation of qualifier prefixes (after, up to). Keep raw evidence; do not classify + as uncertainty or convert range to point. Rules limited to explicit unit vocabulary, numeric ranges, ratios, selected counts and modifiers. No general word-number parser, list support, structured numeric semantics, property/entity relation or Japanese annotation evaluation. No production changes. Next: freeze parser before new source holdout, remove special-case count vocabulary or generalize using measured entity context, use Python implementation with raw span evidence, classify ambiguity, measure exact and overlap separately, expand to complete annotated corpus and technical Japanese. No DB/API/UI/network/automation changes; do not merge.
