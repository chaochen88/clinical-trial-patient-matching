# Class 9 — TrialGPT Evaluation and Error Analysis

Evaluate criterion decisions, evidence selection, and patient retrieval with fixed data and explicit metrics. Use the results to identify which changes improve quality and what they cost.

## 1. Define the Evaluation Cases

**Example — [TrialGPT annotation 2][annotations]:**

```text
Patient: sigir-20141
Trial: NCT01397994
Input: original numbered patient note + inclusion criterion "Age 25 to 65 years"
Expert label: included
Expert evidence: sentence [0], which states the patient is 58
```

- Download the pinned [expert annotation release][annotation-files]. Preserve the complete note, criterion, IDs, and sentence numbering. It contains 1,015 criterion examples from 53 patients.
- Pass only patient evidence, criterion text/type, and necessary identifiers to the matcher. Keep `expert_eligibility`, `expert_sentences`, and all `gpt4_*` fields in evaluation storage.
- Freeze the source revision, input hashes, schema, model, prompt, and tool budgets. Use original trial snapshots for pair judgments; amended criteria or fictional patient variants need their own reference answers.

## 2. Split by Patient and Preserve Label Meaning

**Example — all rows for `sigir-20141` belong to one split.** Its age, diagnosis, and other criterion examples must stay together. Since this patient appears in teaching examples, assign it to development.

| Released criterion label | Our predicate status | Interpretation |
|---|---|---|
| `included` / `not included` | `met` / `not_met` | Inclusion requirement |
| `excluded` / `not excluded` | `met` / `not_met` | Exclusion condition |
| `not enough information` | `unknown` | Evidence insufficient |
| `not applicable` | `not_applicable` | Criterion outside applicable scope |

- Save explicit development, validation, and test patient-ID lists with a fixed seed. Keep duplicates and variants together; check class counts and overlap. Audit the supplied `training` flag instead of assuming it provides patient-separated splits.
- Tune prompts on development/validation cases, then freeze the configuration before the test run. Record trial overlap too; a patient-separated test measures generalization to new patients, not necessarily new protocols.
- Keep criterion type with every mapped label. A `met` exclusion indicates a conflict; do not interpret all `met` labels as favorable.

## 3. Score Decisions and Evidence Separately

**Example — for annotation 2, a hypothetical model predicts `included` with sentences `[0, 3]`.** The decision is correct, but sentence 3 is extra relative to the expert evidence `[0]`:

```text
Evidence precision = 1 matching sentence / 2 predicted = 0.50
Evidence recall    = 1 matching sentence / 1 reference = 1.00
Evidence F1        = 2 × 0.50 × 1.00 / (0.50 + 1.00) ≈ 0.67
```

- Report accuracy, macro-F1, per-class support, and a confusion matrix, with inclusion/exclusion breakdowns. Fix the label set and zero-division policy across comparisons. [F1 definition][f1]
- Calculate evidence metrics using unique sentence IDs; report empty-reference cases separately. Also check citation existence, patient identity, and whether the cited text actually supports the claim. Sentence overlap measures agreement with annotations, not explanation correctness.
- Count erroneous inclusion passes and erroneous exclusion clearances, especially when the reference is `unknown`. Retain timeout/invalid-output rows as failures; report completion coverage and never silently remove failed cases from the denominator.

## 4. Evaluate the Correct Retrieval Direction

**Example — a hypothetical, fully reviewed 20-patient pool contains five patients relevant for screening.** Retrieving four of them in the top ten gives **Recall@10 = 4/5 = 0.80**.

- For our **trial → patients** workflow, fix several trials and review every trial–patient pair in the evaluation pool. Define relevance as “worth detailed screening,” retain unresolved judgments, and report judgment coverage. Full-pool recall requires a known positive denominator.
- Report Recall@10 and nDCG@10 per trial and averaged across trials. Use binary relevance for this initial test, document tie handling, and mark trials with no reference positives as undefined for recall/nDCG. Compare BM25, vector search, fusion, and reranking on the same pool. [nDCG reference][ndcg]
- Report TREC/SIGIR **patient → trials** experiments separately using their original corpus and qrels. TREC label 1 means excluded; SIGIR label 1 means potential referral. Unjudged pairs remain unjudged. The inspected TREC 2021 slice has only one judged patient for `NCT04450407`, so reversing that slice cannot validate our cohort retrieval. [TREC judgments][trec] [TrialGPT files][trialgpt]

## 5. Compare Baselines in Langfuse

**Example — compare these assessment configurations on identical held-out inputs:**

| Run | Configuration |
|---|---|
| A | Deterministic rules; unresolved narrative criteria return `unknown` |
| B | Rules + one structured LLM assessment |
| C | B + bounded evidence lookup and verification |

- Hold the model, corpus access, labels, and overall budget constant where possible. Test one component removal at a time, such as C without verification, to understand its contribution. Extra evidence is limited to the frozen source available to the reference reviewer.
- Create a versioned Langfuse dataset with inputs and expected outputs. Adapt the graph through `dataset.run_experiment(...)`; evaluators alone read expected answers. Save per-case scores and trace IDs, then compute aggregate metrics from the complete results. [Experiments][langfuse]
- Compare quality with p50/p95 active latency, tokens, tool attempts, and total cost including retries. Record cache policy. Evaluate pre-review predictions separately from corrected results; a changed evidence version is a separate case.

## 6. Diagnose Errors and Report Uncertainty

**Example — TrialGPT annotation 0 requires a chronic-angina diagnosis and a specified abnormal scan.** Its expert answer is `not enough information`. If the system returns `met` based only on chest-pain sentences, record:

```text
Stage: criterion assessment
Error: unsupported inference from symptoms to diagnosis and scan findings
Effect: erroneous inclusion pass
Follow-up: require evidence for both parts; add a development regression case
```

- Review up to 20 failures across stages: retrieval miss, omitted evidence, extraction/negation, units/time, criterion polarity, logic, or processing failure. Record case ID, expected/predicted result, supporting sources, and a targeted fix.
- Estimate criterion-metric uncertainty with a **patient-level bootstrap**: sample test patients with replacement, retain all their rows, and recompute metrics for 1,000 replicates. Use identical sampled patients for paired system comparisons; report the 2.5th–97.5th percentiles and patient count. For retrieval, resample trial queries.
- Report rare-class counts, small-sample limits, and observed regressions alongside improvements. Once test cases guide changes, treat them as development material and reserve fresh cases for the next final evaluation.

## Deliverables

- A versioned evaluation manifest, patient-separated splits, and tested label mappings.
- Criterion/evidence scoring with failure coverage and unsupported-decision counts.
- A trial → patients retrieval report, with any TREC/SIGIR reproduction identified separately.
- Langfuse experiments comparing baselines, an ablation, latency/cost, and uncertainty estimates.
- An error-analysis table and reviewed PR; Ruff and pytest passing.

## Sources

- [TrialGPT expert annotations][annotations] and [pinned data files][annotation-files].
- [TrialGPT code and source cohorts][trialgpt]; [TREC 2021 judgments][trec].
- [Macro-F1][f1], [nDCG][ndcg], and [Langfuse dataset experiments][langfuse].
- Development corpus: [Synthea November 2021 FHIR](https://synthetichealth.github.io/synthea-sample-data/downloads/synthea_sample_data_fhir_r4_nov2021.zip) and [CSV](https://synthetichealth.github.io/synthea-sample-data/downloads/synthea_sample_data_csv_nov2021.zip). Use verified shared patients; matching labels require separate review.

[annotations]: https://huggingface.co/datasets/ncbi/TrialGPT-Criterion-Annotations
[annotation-files]: https://huggingface.co/datasets/ncbi/TrialGPT-Criterion-Annotations/tree/1cfcafde94a1560a33b4addc1638664fe28fc059
[trialgpt]: https://github.com/ncbi-nlp/TrialGPT/tree/1b3242cd50c153a9c324661d4a0bd94fc68a4226
[trec]: https://trec.nist.gov/data/trials2021.html
[f1]: https://scikit-learn.org/stable/modules/generated/sklearn.metrics.f1_score.html
[ndcg]: https://scikit-learn.org/stable/modules/generated/sklearn.metrics.ndcg_score.html
[langfuse]: https://langfuse.com/docs/evaluation/get-started/offline
