# Verified matrix writer bridge (Draft)

Stacked on #60. Existing internal explicit preview/confirm/apply service now also derives literal spans for supported Markdown backend pp/tg matrix cells. The writer re-derives complete matrix drafts from source and rejects missing/modified conditions, incorrect model identity, or nonmatching spans. Literal t/s is canonicalized as tok/s only for verified benchmark matrix drafts; source unit text retained. The service uses existing public/allowed representative news body, exact associated model name (case-insensitive), confirmed source and input hashes, transaction, result ledger and supersession. Bump extractor to rule:c7a; include c5b/c6c in managed lifecycle; preserve existing manual/claim rows. Result hash now covers statistic, confidence, min/max as well as previous fields and rejects unavailable hashes. No new endpoint, scheduler, permission, external access, UI, DB schema or automatic execution.

Only stat=unknown matrix candidates, with explicit metric and unit, are eligible. Fenced examples excluded by the bridge. Native mean±stddev, depth, compound update cells, JSON/CSV/vLLM, PDF/Office, non-news documents and inferred/shared conditions are not bridged. The current schema lacks lossless standard-deviation representation. Footnote alias remains a retrieval hint, never persistence identity; no entity merges. Raw model label, value/unit and original condition spans retained; file size is not VRAM. Contextual comparability not established. A table inside other unsupported Markdown containers or a prose example is not proven to be an actual measurement by this parser.

Offload: existing generic fold strips hyphens, so -1 and 1 would collide. Normalize only explicit layer-count syntax (-1,0,positive integers up to 8 digits); unsupported filters rejected. Search compares original canonical integer evidence for offload, including older folded rows; no migration or rewriting prior measurements. Other conditions unchanged. Noncanonical legacy raw representations do not match. Existing low-level search uses current body digest and policy checks; full association/revision synchronization and policy auditing remain follow-up tasks.

## Executed isolated diagnostic

Python 3.13.5, exact module blob 6cb6e76e3a005bd33328155000cffe5c3bb11ae9, parser a1b5fd29df63041b4a5e6ec2120295188ca51732 and quantity 0065e56b28c591d1a8538554d042bb03a76996ab verified using git hash-object. Imported only verbatim excerpts of Span/Candidate/MeasurementDraft and Markdown _cells from existing dependencies, not full application modules. Used copied real table excerpts from llama.cpp README blob 3760072121150eab2c33e79a1a7be8b0fec71b57 and Framework benchmark blob 8ae3b7dfbff78e89b14b319db43562b7c4f55b15 (prior audit fixtures).

| Source | Prior candidate count | Bridge literal spans |
|---|---:|---:|
| Radeon matrix | 12 | 12 |
| CPU threads | 12 | 0 |
| Batch | 4 | 0 |
| Update steps | 0 | 0 |

All 12 Radeon drafts had exact source substrings for subject, value, unit and conditions, including Unicode/CRLF prefix. Both backtick and tilde fenced matrix examples excluded. With three unmarked entity names 3B/7B/14B, 8/12 eligible by exact-name check; 7B with superscript note remains excluded (4). This is not a saved-row count. Decode values 3B 86.6/104.9; 14B 18.9/19.2 tok/s; verified header backend/gen_tokens and row ngl. No DB or service executed in this diagnostic.

Added repository regression tests for real excerpt preview/confirm/search/revision/old hash/policy change, removed conditions and wrong entity rejection, signed offload legacy comparison, fence and spans. These tests and the full application suite were NOT run: container clone DNS failed and zstandard is missing. No claim about real service runtime, schema migration, full pytest or writer transaction correctness until executed in installed backend. Reproduce: cd backend && pytest -q tests/test_measure_matrix_writer.py tests/test_measure_service.py tests/test_real_benchmark_formats.py tests/test_measure_candidate_contract.py. Then verify actual writer and API with public data, test rollback/limits/reversion, old rows, policy and stale entity association. Do not merge until verified. Next: lossless statistical evidence schema, contextual compound-cell candidates, shared document contract and API/sync integration.
