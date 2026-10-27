# Synthetic engineering comparison

Authored synthetic labels; no clinical validation or independent review.

These are three frozen fictional trials against the same 20 synthetic patients,
using the actual local MiniLM encoder and cross-encoder. `lexical` is BM25,
`vector` is cosine search, `fused` is RRF, and `shortlist` is RRF plus reranking.
The top-10 metric is computed before the top-20 selection flag. Latency columns
are query-stage measurements; model startup and offline indexing are recorded
separately in the JSON results under `data/class5/engineering`.

| Trial | Method | Recall@10 | nDCG@10 | Stage seconds |
|---|---|---:|---:|---:|
| SYNTHETIC-T2 | lexical | 1.000 | 0.653 | 0.001 |
| SYNTHETIC-T2 | vector | 1.000 | 0.899 | 0.019 |
| SYNTHETIC-T2 | fused | 1.000 | 0.893 | 0.019 |
| SYNTHETIC-T2 | shortlist | 1.000 | 0.705 | 1.156 |
| SYNTHETIC-T1 | lexical | 1.000 | 0.905 | 0.001 |
| SYNTHETIC-T1 | vector | 1.000 | 0.761 | 0.014 |
| SYNTHETIC-T1 | fused | 1.000 | 0.983 | 0.014 |
| SYNTHETIC-T1 | shortlist | 1.000 | 1.000 | 0.945 |
| SYNTHETIC-OBESITY | lexical | 1.000 | 1.000 | 0.001 |
| SYNTHETIC-OBESITY | vector | 1.000 | 0.733 | 0.010 |
| SYNTHETIC-OBESITY | fused | 1.000 | 0.906 | 0.010 |
| SYNTHETIC-OBESITY | shortlist | 1.000 | 0.733 | 1.052 |

Recall@10 is 1.0 for all methods on this small, easy fixture, so it does not
distinguish the approaches. Reranking improved nDCG for the type-1 example but
reduced it for type-2 and obesity. This does not support assuming that the generic
cross-encoder always improves clinical retrieval. Keep all four baselines for
comparison, and judge real trial/patient pairs independently before tuning.

## Public Synthea baseline

The `NCT03926806` run processed all 555 shared November 2021 patient IDs, fused
the two top-50 lists to 50 candidates, and selected 20 after reranking. No
unreviewed hard filters were enabled. Initial indexing took 31.327 seconds,
BM25 0.040 seconds, query vector retrieval 0.053 seconds, and reranking 2.063
seconds on this local run. First model startup/download took 168.030 seconds;
these timings are observations from this machine, not performance guarantees.

All 20 selected search documents were truncated to the encoder budget; no selected
query/document pair exceeded the reranker budget. Complete profiles remain
available, so later eligibility assessment must gather the full relevant evidence.
This run has no independent relevance labels and therefore makes no recall claim.

Pinned revisions:

- Encoder: `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`.
- Reranker: `233902d25c440f23af6f7d6e94d2946bac0bee0a`.

The separate real review worksheet freezes 20 patients and three actual trials,
with all 60 labels initially unresolved. Recall/nDCG remain undefined until review
is complete. The synthetic fixture is not a replacement for those judgments or a
separate development/test evaluation.
