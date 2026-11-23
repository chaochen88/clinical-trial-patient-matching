# Class 6 — Criterion-Level Assessment and Aggregation

Assess each shortlisted patient against the trial's criteria, then produce a screening report containing evidence, explicit eligibility conflicts, and follow-up questions.

## 1. Define One Criterion Result

**Example — HbA1c assessment for [NCT03926806][trial-t2] using the November 2021 [Synthea patient][fhir]:**

```json
{
  "patient_id": "1cfa5a70-7f3c-4227-5cf1-e182fcff4cd4",
  "criterion_id": "inclusion-2",
  "criterion_type": "inclusion",
  "predicate": "HbA1c < 8.5%",
  "assessment_time": "2021-01-13T23:59:59-05:00",
  "status": "met",
  "evidence_state": "supported",
  "evidence_ids": ["00b9e893-d5b1-6e91-d633-8b863797d43b"],
  "reason": "HbA1c is 5.84% at 2021-01-13T23:22:51-05:00.",
  "follow_up": null
}
```

- Validate results with Pydantic. Use `met / not_met / unknown / not_applicable` for the **stated predicate**, and retain `inclusion` or `exclusion` separately.
- For inclusion, `met` supports candidacy. For an exclusion such as “Receiving insulin,” `met` identifies an eligibility conflict; `not_met` requires evidence supporting its absence.
- Keep evidence quality (`supported / missing / conflicting`) separate from the decision. Persist the exact criterion, trial/profile versions, and source references with each result.

## 2. Apply Deterministic Rules to Verified Facts

**Example — the trial requires HbA1c strictly below 8.5%:**

| Evidence after unit/time validation | Criterion result |
|---|---|
| 5.84% | `met` |
| 8.5% | `not_met` |
| No usable HbA1c | `unknown` |

- Use Python for numerical comparisons, units, durations, and event counts. Respect strict versus inclusive boundaries.
- Select evidence available by the assessment time. Apply stated measurement windows; record unspecified windows without inventing a recency requirement.
- Preserve export precision. The shared patient's FHIR HbA1c is `5.84` and CSV value is `5.8`; both pass this threshold. Near a boundary, use verified precision or flag uncertainty rather than silently changing the value.

## 3. Assess Narrative Evidence with Structured LLM Output

**Example — [TREC patient `trec-202129`][trec] and [NCT04450407][trial-t1]:**

```text
Inclusion: multiple daily injections without interruption for ≥3 months
Patient evidence: "after not taking his insulin for 3 days"
Status: not_met
Reason: the reported interruption breaks the required continuous-use period.
```

- Supply one criterion, its type, and that patient's evidence bundle. Require a status, supporting evidence IDs/quotes, concise reasoning, and an unresolved question where needed.
- Verify that citations belong to the patient and exist in the source. Preserve negation, possibility, and family-history context; unexplained assumptions become `unknown`.
- Use the described presentation as the time basis for this undated case. Keep model/provider and prompt versions, validate output, and allow at most one repair attempt before recording a processing failure.

## 4. Handle Missing and Conflicting Evidence

**Example — original TREC patient versus a clearly labeled fictional variant:**

| Evidence | Requirement | Result and follow-up |
|---|---|---|
| No BMI supplied | BMI ≤35 | `unknown / missing`; request dated BMI or height/weight |
| Original HbA1c 11.3% plus an invented same-encounter result of 8.2%, with no verified correction | HbA1c 5.6–9.5% | `unknown / conflicting`; obtain the original lab report and timestamps |

- Preserve both assertions and their provenance until a documented correction or source review resolves the conflict.
- Treat missing evidence as unresolved. Use `not_applicable` only when an explicit applicability condition is demonstrably false, with a reason; unknown applicability stays `unknown`.
- Turn uncertainty into a specific request: which fact, source, date, or clarification would resolve the criterion? Keep fictional variants separate from original benchmark cases.

## 5. Evaluate Logical Groups Before Aggregation

**Example — a hypothetical exclusion group:** severe hypoglycemia **OR** hypoglycemia unawareness within six months. If one branch is `met` and the other is `unknown`, the group is `met` and the exclusion applies.

For applicable, nonempty groups:

```python
def combine(statuses, operator):
    if not statuses or operator not in {"AND", "OR"}:
        raise ValueError("Supply a nonempty AND/OR group.")
    if any(s not in {"met", "not_met", "unknown"} for s in statuses):
        raise ValueError("Resolve applicability before combining.")
    decisive = "not_met" if operator == "AND" else "met"
    unanimous = "met" if operator == "AND" else "not_met"
    if decisive in statuses:
        return decisive
    return unanimous if all(s == unanimous for s in statuses) else "unknown"


assert combine(["met", "unknown"], "OR") == "met"
assert combine(["met", "unknown"], "AND") == "unknown"
```

- Preserve the parsed expression tree, negation, and time-window scope. Apply inclusion/exclusion polarity to the **parent result**; a failed alternative inside an inclusion OR group need not reject the patient.
- Resolve applicability from explicit rule scope before combining. Record skipped branches and their justification.
- Test AND/OR combinations containing unknowns. Validate that every required parent criterion has a result or an explicit unresolved entry.

## 6. Aggregate, Save, and Evaluate the Screening Report

**Example — original TREC patient at the described presentation, assessed retrospectively against NCT04450407:**

```text
Supported inclusion: age 24 satisfies minimum age 18
Failed inclusion: HbA1c 11.3% exceeds 9.5%
Failed inclusion: reported three-day insulin interruption
Unresolved: BMI and several exclusion histories
Overall: explicit_eligibility_conflict
```

- Aggregate in this order: a verified failed mandatory inclusion or applicable exclusion → `explicit_eligibility_conflict`; otherwise unresolved/failed/unassessed checks → `needs_review`; otherwise all applicable criteria pass, with justified nonapplicability recorded → `potential_candidate`. A high retrieval score or multiple passes cannot cancel a mandatory failure.
- Connect LangGraph nodes: `load_candidate → assess_criteria → validate_evidence → evaluate_groups → aggregate → save_report`. Key results by trial version, patient variant, criterion ID, and assessment time. If screening stops early after a decisive failure, mark remaining criteria `unassessed` and retain the failure evidence.
- Compare deterministic-only assessment with rules plus LLM interpretation on a small reviewed set. Measure criterion macro-F1, evidence validity, unsupported `met` decisions, and aggregate errors. Use [TrialGPT expert annotations][annotations] for their original note/criterion pairs; keep answer fields outside prompts.

## Deliverables

- A validated criterion-result schema and deterministic/LLM assessment paths.
- Logical-group evaluation and aggregation with completeness checks.
- Reports for the shared Synthea patient, original TREC patient, and separately labeled conflict variant.
- Tests covering polarity, boundaries, unknowns, applicability, AND/OR groups, and incomplete processing; a short comparison against reviewed judgments.
- A reviewed PR linked to an issue in your repository; Ruff and pytest passing.

## Sources

- Trials: [NCT03926806][trial-t2] and [NCT04450407][trial-t1]. Preserve the frozen criteria used in these retrospective exercises.
- Patient data: [November 2021 FHIR][fhir] and [November 2021 CSV][csv]. Shared example: `Alaine226_Willms744_1cfa5a70-7f3c-4227-5cf1-e182fcff4cd4.json`.
- Narrative: [TREC 2021 patient summaries][trec], row `trec-202129`.
- Evaluation: [TrialGPT criterion annotations][annotations].
- Workflow: [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api).

[trial-t2]: https://clinicaltrials.gov/study/NCT03926806
[trial-t1]: https://clinicaltrials.gov/study/NCT04450407
[fhir]: https://synthetichealth.github.io/synthea-sample-data/downloads/synthea_sample_data_fhir_r4_nov2021.zip
[csv]: https://synthetichealth.github.io/synthea-sample-data/downloads/synthea_sample_data_csv_nov2021.zip
[trec]: https://raw.githubusercontent.com/ncbi-nlp/TrialGPT/1b3242cd50c153a9c324661d4a0bd94fc68a4226/dataset/trec_2021/queries.jsonl
[annotations]: https://huggingface.co/datasets/ncbi/TrialGPT-Criterion-Annotations
