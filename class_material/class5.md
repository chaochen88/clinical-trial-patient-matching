# Class 5 — Hybrid Patient Retrieval and Reranking

Given one trial, retrieve a shortlist of patients for detailed eligibility assessment. Combine keyword and semantic search, then rerank the candidates using their evidence.

## 1. Create One Search Document per Patient

**Example — an abbreviated profile from the November 2021 [Synthea FHIR export][fhir]:**

```text
Patient: 1cfa5a70-7f3c-4227-5cf1-e182fcff4cd4
Assessment time: 2021-01-13T23:59:59-05:00
Age: 62
Diagnosis evidence: diabetes since 2000; type-2-specific complications
HbA1c: 5.84%, measured 2021-01-13
BMI: 29.38 kg/m², measured 2021-01-13
```

- Build searchable text from the validated profile: diagnoses, medications, measurements, and dated history. Preserve assertion/negation and source references.
- Use the 555 shared November 2021 patient IDs as the initial corpus; represent each patient once. Keep FHIR/CSV precision differences in provenance rather than duplicating patients.
- Freeze the assessment time and profile version; exclude later evidence. Store text, structured facts, and embeddings separately. Bound text to the encoder's token limit and record truncation.

## 2. Turn Trial Criteria into a Retrieval Query

**Example — [NCT03926806][trial], the type-2 diabetes trial used in earlier exercises:**

```text
Search concepts: type 2 diabetes, T2DM, HbA1c, body mass index, oral medication
Structured requirements: age 45–75; BMI 27–36; HbA1c <8.5%
Detailed checks: disease duration, treatment stability, and exclusions
```

- Derive search concepts from the target condition and inclusion criteria. Expand approved synonyms such as `T2DM → type 2 diabetes`; retain the original query for comparison.
- Keep exclusion conditions in a separate assessment checklist. Searching positively for “insulin” because the trial excludes insulin use can retrieve the wrong population.
- Apply hard filters only to verified, time-appropriate facts. Missing or conflicting BMI remains a candidate with a question; record the reason for every hard rejection. Use this completed trial in retrospective mode.

## 3. Run Lexical and Vector Retrieval

**Example — two routes to the same candidate:** BM25 can match “type 2 diabetes” literally; an embedding model can connect it with related phrasing such as “T2DM.”

Add the baseline packages in your repository: `pixi add --pypi rank-bm25 sentence-transformers`. The first model load downloads its weights.

- **Lexical baseline:** use `rank_bm25.BM25Okapi` over patient documents. Apply the same tokenization to queries/documents. Return the top 50 unique patient IDs with scores. [BM25 documentation][bm25]
- **Vector baseline:** start with `sentence-transformers/all-MiniLM-L6-v2`. Encode profiles and the query, normalize vectors, and rank by cosine similarity. Record the model revision; treat this general-purpose model as a baseline to evaluate. A later biomedical comparison can use MedCPT's paired query/article encoders. [Model cards][encoder] [MedCPT][medcpt]
- For this small corpus, begin with exact in-memory vector search. For PostgreSQL persistence, use pgvector in a separate application schema/database; verify extension availability before use. Keep AACT tables unchanged. [pgvector documentation][pgvector]

## 4. Fuse the Two Rankings

**Example — illustrative rankings, not measured patient results:**

```text
BM25:   A, B
Vector: B, C
```

Reciprocal rank fusion (RRF) adds `1 / (60 + rank)` for each list containing a patient. Here B ranks first, followed by A and C.

```python
from collections import defaultdict


def fuse(rankings, c=60):
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, patient_id in enumerate(dict.fromkeys(ranking), start=1):
            scores[patient_id] += 1 / (c + rank)
    return sorted(scores, key=lambda pid: (-scores[pid], pid))


assert fuse([["A", "B"], ["B", "C"]]) == ["B", "A", "C"]
```

- Fuse ranks rather than adding BM25 and cosine scores, which use different scales.
- Merge the union, deduplicate by patient ID, and retain each retriever's rank. A patient appearing in one list still contributes.
- Keep the top 50 fused candidates for reranking. Treat 50 and the RRF constant 60 as initial settings; tune on development judgments.

## 5. Rerank with the Trial and Patient Evidence

**Example — score the type-2 trial's retrieval query against each shortlisted patient profile:**

```python
from sentence_transformers import CrossEncoder

reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L6-v2")
# query_text and candidate_texts come from the preceding steps.
scores = reranker.predict([(query_text, text) for text in candidate_texts])
```

- A cross-encoder reads query and candidate together. Sort paired patient IDs by score and return the top 20 with evidence references and unresolved questions. This model is a general retrieval baseline; its scores are not eligibility probabilities. [Reranking reference][reranker]
- Preserve relevant contradictory evidence in the candidate text. Evaluate numerical, temporal, and exclusion rules explicitly in the later criterion-assessment stage.
- Connect the steps in LangGraph: `build_query → retrieve_lexical + retrieve_vector → fuse → rerank → save_shortlist`. Cache patient embeddings and record candidate counts, latency, and model configuration.

## 6. Evaluate Retrieval Separately from Eligibility

**Example — a hypothetical, fully reviewed 20-patient test pool:** reviewers identify four patients worth detailed screening; the top ten results include three. **Recall@10 = 3/4 = 0.75.** This measures retrieval relevance, not confirmed enrollment eligibility.

- Start with three frozen trials and a fixed 20-patient pool. Review every pair independently of model output: `relevant for screening / irrelevant / unresolved`. Resolve uncertain judgments before reporting complete-pool recall; report coverage and cases with no positives.
- Compare **BM25**, **vector**, **RRF**, and **RRF + reranker** using Recall@10, nDCG@10, latency, and inference cost. Use binary relevance for nDCG initially; report offline indexing cost separately. Tune and test on separate trial/patient sets, and label small synthetic results as engineering evaluation.
- Inspect missed positives after filtering, retrieval, and reranking. A reranker cannot recover a patient absent from its candidate set. TrialGPT's patient-to-trial judgments are a separate evaluation resource, not automatic labels for this patient corpus.

## Deliverables

- Versioned patient search documents and a trial-query builder.
- BM25/vector baselines, RRF fusion, and reranking connected in one workflow.
- A shortlist containing patient IDs, retrieval ranks, reranker scores, and evidence references.
- A small reviewed evaluation table and comparison report; tests for duplicate IDs, missing-field retention, rank fusion, and time cutoffs.
- A reviewed PR linked to an issue in your repository; Ruff and pytest passing.

## Sources

[fhir]: https://synthetichealth.github.io/synthea-sample-data/downloads/synthea_sample_data_fhir_r4_nov2021.zip
[trial]: https://clinicaltrials.gov/study/NCT03926806
[bm25]: https://github.com/dorianbrown/rank_bm25
[encoder]: https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
[medcpt]: https://huggingface.co/ncbi/MedCPT-Query-Encoder
[pgvector]: https://github.com/pgvector/pgvector
[reranker]: https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2

- Patient data: [November 2021 FHIR][fhir] and [November 2021 CSV](https://synthetichealth.github.io/synthea-sample-data/downloads/synthea_sample_data_csv_nov2021.zip). Shared patient example: `Alaine226_Willms744_1cfa5a70-7f3c-4227-5cf1-e182fcff4cd4.json`.
- Trial: [NCT03926806][trial]; preserve the source snapshot with extracted criteria.
- Retrieval design: [Sentence Transformers retrieve-and-rerank guide](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html).
