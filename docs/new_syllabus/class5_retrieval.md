# Class 5: hybrid patient retrieval

This stage retrieves patients for detailed assessment of one frozen trial. It
does not determine eligibility. It adds a separate retrieval LangGraph alongside
the Class 3/4 extraction workflows:

```text
                  ┌─ retrieve_lexical ─┐
build_query ───────┤                   ├─ fuse ─ rerank ─ save_shortlist
                  └─ retrieve_vector ──┘
```

## Run the public baseline

From the repository root, in WSL:

```bash
pixi install
pixi run retrieval-sources
pixi run retrieve-patients
```

Preparation downloads the November 2021 FHIR and CSV Synthea samples into
`data/class5/sources`. It checks their shared patient IDs number exactly 555,
creates one FHIR-derived document per shared patient, freezes assessment time at
`2021-01-13T23:59:59-05:00`, and saves URL/archive hashes and patient IDs in a
manifest. CSV is used to establish the intersection; FHIR supplies facts, avoiding
duplicate patients and mixed measurement precision. Existing `data/synthea` and
Class 4 sources/results remain separate.

`NCT03926806` is downloaded from the ClinicalTrials.gov API and saved unchanged.
The query includes condition terms and inclusion bullets; exclusion bullets are
stored only in the assessment checklist. Approved synonym expansions are recorded
alongside the original query. The example runs in retrospective mode. It does not
check whether a completed trial can recruit today.

Numeric hard filters are **opt-in reviewed rules** in `query.json`:
`verified_filters` uses the existing `Rule` schema, and each rule's exact source
quote must appear in `inclusion_quotes`. No hard filter is automatically inferred
from unreviewed prose. Missing dates/values, incompatible units, and conflicts
retain the patient with a question. For a dated numeric concept the latest
measurement is used, retaining all same-time disagreements. Every rejection saves
the rule and evidence ID in `filter_audit.json`. Recency windows and more complex
clinical requirements belong to later assessment.

Retrieval uses:

- `rank_bm25.BM25Okapi`, with the same lowercase tokenizer for documents and query.
- `sentence-transformers/all-MiniLM-L6-v2`, normalized embeddings and exact cosine
  search in memory.
- Reciprocal rank fusion, `1 / (60 + rank)`, over unique IDs from both top-50 lists.
- `cross-encoder/ms-marco-MiniLM-L6-v2`, reranking the top 50 fused candidates and
  selecting 20. Equal scores break ties by patient ID.

Hub commit revisions are resolved during preparation and saved in `models.json`;
inference uses those immutable revisions. Both models run locally on CPU, without
Claude calls. First use downloads weights. External API inference cost is zero;
local compute cost is unmeasured, not asserted to be zero. Model scores are
retrieval relevance values, not eligibility probabilities.

## Outputs and provenance

Complete profiles with quotes and original source records are stored under
`data/class5/profiles`. Search documents contain compact fact projections and
point to those profiles, keeping the retrieval corpus lightweight.

`data/class5/documents.jsonl` retains text and structured source-linked facts with
profile/document versions and a frozen assessment time. The Class 5 FHIR adapter
adds computed age, coded Condition history, and MedicationRequest/Statement
history to Class 4's supported Observation parsing. Historical medication orders
do not prove current treatment or adherence. Unsupported resources/value types
remain processing findings.

Future clinical/available evidence is excluded before text construction. Family
experiencers and invalidated/cancelled records are omitted with reasons. Search
text places distinct concepts before repeated measurement history, preserving
assertion and dates. Source quotes and original values remain in structured facts,
separate from encoder text. The model-token limit bounds text for both retrievers;
truncation is recorded, and structured evidence remains available for assessment.
The pair limit and query truncation are recorded separately.

`data/class5/run/shortlist.json` includes both retrievers' scores/ranks, fused ranks,
all reranked candidates, top-20 selection flags, source evidence IDs, unresolved
questions, model/settings versions, candidate counts, assessment time, timings,
and cache status. Evidence references identify the full profile evidence pool,
not a claim that the truncated encoder saw every fact. Embeddings are separate
`.npy` cache files keyed by patient document versions, ordering and model settings.
`run/documents.jsonl` stores the actual bounded inputs.

`retrieval.py` accepts `--documents`, `--query`, `--models`, `--output`, and `--cache`
to reuse the workflow with another frozen corpus or trial. It rejects duplicate
patient IDs and mixed assessment times. No vector database is required for this
small corpus; AACT tables are untouched.

## Evaluation

See [the saved engineering results and run review](class5_engineering_results.md)
for the first measured comparison and the 555-patient baseline's limitations.

Run the synthetic engineering comparison with actual local models:

```bash
pixi run retrieval-engineering
```

`tests/fixtures/class5/synthetic_pool.json` defines three fictional trials and a
fixed 20-patient pool. Its authored labels are engineering expectations, **not
independent clinical judgments**. Outputs in `data/class5/engineering` compare
BM25, vector, RRF and RRF+reranker using Recall@10, binary nDCG@10, stage timings,
indexing/cache cost and missed positives. No tuning is performed or improvement
claimed from these examples. Model-loading/indexing costs are separate from
query-stage latencies.

For the real shared cohort, create a review worksheet before inspecting rankings:

```bash
pixi run python scripts/prepare_class5_review.py
```

This freezes a model-independent hash-selected 20-patient pool and three source
trials (`NCT03926806`, `NCT04450407`, `NCT03548935`). It creates 60 `unresolved`
judgments under `data/class5/review`. An independent reviewer should examine
every pair and fill in `relevant`, `irrelevant`, or retain `unresolved`, with their
identity and rationale. Do not overwrite completed judgments by rerunning this
preparation script. Run retrieval using that pool's `documents.jsonl` and each
frozen query, with separate output directories.

`pixi run python scripts/run_class5_review.py` runs all three prepared real queries
and writes their comparison reports while preserving unresolved judgments.

```bash
pixi run python -m clinical_trial_patient_matching.retrieval \
  --documents data/class5/review/documents.jsonl \
  --query data/class5/review/NCT03926806-query.json \
  --output data/class5/review/NCT03926806-run

pixi run python -m clinical_trial_patient_matching.retrieval_evaluation \
  --run data/class5/review/NCT03926806-run/shortlist.json \
  --pool data/class5/review/pool.json \
  --judgments data/class5/review/judgments.jsonl \
  --output data/class5/review/NCT03926806-evaluation.json
```

The evaluator refuses mixed/duplicate judgments and rankings outside the fixed
pool. Missing judgments become unresolved. It reports judgment coverage, pools
with no positives, missed top-10 positives and positives absent from fused
candidates. Recall/nDCG remain null until all pairs are resolved, and are undefined
for zero-positive pools. TrialGPT patient-to-trial qrels are never relabeled as
judgments for these Synthea patients. Separate development/test sets and independent
review are still needed before reporting general retrieval quality.

## Verification and remaining collaboration

```bash
pixi run test
pixi run check
```

New tests cover duplicate IDs, rank fusion, inclusion/exclusion query separation,
missing/conflicting/unit retention, audited numeric rejections, future clinical
and availability times, age computation, future end-date leakage, graph fan-out
and join, evidence references, and incomplete/zero-positive evaluation pools.

Independent relevance review, issue linkage and a reviewed PR remain human
collaboration steps; generated example rankings do not claim to complete those.

References: [Sentence Transformers retrieval and reranking](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html),
[encoder model card](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2),
[BM25 implementation](https://github.com/dorianbrown/rank_bm25).
