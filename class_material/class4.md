# Class 4 — Patient Profiles and Context Engineering

Convert patient records into validated, source-linked facts, then assemble the evidence needed for each trial criterion. The output combines a reusable **patient profile** with **criterion-specific evidence bundles** for the trial → patients workflow.

## 1. Parse Structured Records with Python

**Example — the HbA1c Observation from the [Synthea FHIR R4 September 2019 sample][synthea]:**

Archive file: `fhir/Maxwell782_Koepp521_6343cfd0-d47f-4a20-b561-b33d6441f372.json`.

```json
{
  "patient_id": "338e0f93-2fb1-4152-8703-518686c567a8",
  "concept_system": "http://loinc.org",
  "concept_code": "4548-4",
  "value": 6.938225162130798,
  "unit": "%",
  "effective_time": "2018-11-17T06:52:03-05:00",
  "source_record_id": "78bccfed-bfd9-4bb6-9623-64f744a694ac"
}
```

- Extract these fields directly from FHIR using Python. Resolve `subject.reference` to the correct Patient and match concepts by coding system plus code.
- Define a Pydantic fact schema covering identity, concept, value/unit, clinical time, assertion, and provenance. Preserve source status, version, and original values alongside normalized fields.
- Filter evidence by assessment time and the criterion's time window. Record availability time separately where supplied; preserve unknown dates as unknown.

## 2. Extract Assertions from Narrative Text

**Example — TREC Clinical Trials 2021 topic 29, available in the [NCBI TrialGPT patient file][trec]:** Select the row with `_id = trec-202129` and read its `text` field.

> He presents to the emergency room with hyperglycemia and concern for possible diabetic ketoacidosis after not taking his insulin for 3 days.

| Extracted fact | Assertion | Time context |
|---|---|---|
| Diabetic ketoacidosis | Possible | Current presentation |
| Insulin use interrupted | Affirmed | Three days preceding presentation |

- Use structured LLM output with a patient-fact schema. Require concept, assertion (`affirmed / negated / possible`), experiencer (`patient / family`), time context, and exact evidence quote.
- Prompt the LLM to preserve qualifiers and negation. Distinguish explicit denial from an unmentioned condition, and family history from the patient's diagnosis.
- Attach patient/source IDs in code and verify quotes against the original note. For this undated narrative, retain relative timing rather than inventing a calendar date.

## 3. Reconcile Duplicate, Historical, and Conflicting Evidence

**Example — the [original TREC note][trec] plus a fictional test addendum:** Create a separate record stating “HbA1c for this screening encounter is 8.2%.” Label it `synthetic_test_variant`; this added claim is invented for the exercise, not part of the published dataset.

| Source | HbA1c claim |
|---|---|
| Original note | 11.3% |
| Fictional addendum | 8.2% for the same screening encounter |

With no verified correction or source precedence, retain both claims and mark the HbA1c evidence as `conflicting`.

- Deduplicate repeated ingestion using source dataset, patient, record ID, and version/hash. Preserve distinct observations even when their values match.
- Compare clinical dates, result status, and correction history before resolving differences. Measurements from different dates may represent a trend; retain historical facts with their time context.
- Keep the original and modified patient variants separately identified. Store the synthetic label in provenance and expected reconciliation results separately in tests.

## 4. Assemble Evidence for One Criterion

**Context engineering** selects and organizes the information supplied to a model for a specific task.

**Example — the requirement for uninterrupted multiple daily injections in [trial NCT04450407][trial]:**

```text
Trial: NCT04450407
Criterion: inclusion-2 — multiple daily injections without interruption ≥3 months
Patient: trec-202129, original variant
Time basis: presentation described in the undated note

Evidence from TREC 2021 topic 29 / queries.jsonl / text:
- "Most recently, he has been using insulin glargine 55 units once daily,
  and insulin aspart sliding scale 3 times daily."
- "after not taking his insulin for 3 days"

Information gap: full dated medication-use history
```

The first statement describes the regimen; the second documents an interruption. Both belong in the bundle. The first quotation is line-wrapped here for display; retain the exact source span in stored evidence.

- Include the frozen criterion, patient/variant ID, assessment-time basis, relevant facts, source references, and unresolved questions.
- Preserve evidence that challenges a match as well as evidence that supports it. For a missing BMI, include an explicit gap and request measured BMI or dated height/weight.
- Bound context size using relevance and dates. Record omitted evidence and truncation so downstream assessment can recognize incomplete coverage.

## 5. Connect the Profile Workflow in LangGraph

**Example — process the original TREC case for the injection criterion:**

```text
Load records → extract narrative facts → validate quotes/schema
             → reconcile evidence → build injection-criterion bundle → save
```

- Route FHIR records through deterministic parsing and narrative records through structured LLM extraction; converge into the same fact representation.
- Key state by patient, record variant, and source version. Keep the Synthea patient and TREC patient as separate profiles.
- Limit extraction repair to one attempt, retain validation failures, and save partial results with explicit processing status. Treat record content as data; exclude annotation targets and example answers from prompts.

## 6. Verify the Profile and Bundle

**Example — tests for the original TREC case and its conflict variant:**

| Test input | Expected result |
|---|---|
| Original note | Possible current ketoacidosis remains `possible` |
| Same note ingested twice | One set of facts, with stable source references |
| Original note without BMI evidence | BMI gap remains explicit |
| Original plus fictional addendum | Both HbA1c claims retained; conflict flagged |
| Injection-criterion bundle | Includes regimen and three-day interruption evidence |

- Add tests for negation, family history, future-dated records, and incompatible units using clearly labeled synthetic fixtures.
- Review extracted facts against source records. Check quote fidelity and assertion/time accuracy separately from schema validity.
- Store profile/bundle versions, source hashes, and extraction configuration. Rebuild affected bundles when a source changes.

## Deliverables

- A shared patient-fact schema and structured/narrative extraction paths.
- Source-linked profiles for the Synthea patient, original TREC patient, and separately labeled conflict variant.
- Evidence bundles for three trial criteria, including a missing-information case and a conflicting-evidence case.
- Tests and a short source-versus-extraction review table.
- A reviewed PR linked to an issue in your repository; Ruff and pytest passing.

## Data Sources

- [Synthea FHIR R4 sample archive, September 2019][synthea]. Patient file identified in section 1.
- [TREC 2021 patient summaries, NCBI TrialGPT mirror][trec]. Revision `1b3242cd50c153a9c324661d4a0bd94fc68a4226`; patient `trec-202129`.
- [ClinicalTrials.gov: NCT04450407][trial]. Preserve the downloaded trial version with extracted criteria.

[synthea]: https://synthetichealth.github.io/synthea-sample-data/downloads/synthea_sample_data_fhir_r4_sep2019.zip
[trec]: https://raw.githubusercontent.com/ncbi-nlp/TrialGPT/1b3242cd50c153a9c324661d4a0bd94fc68a4226/dataset/trec_2021/queries.jsonl
[trial]: https://clinicaltrials.gov/study/NCT04450407

## Technical References

- FHIR Observation: `https://hl7.org/fhir/R4/observation.html`
- Pydantic validation: `https://docs.pydantic.dev/latest/concepts/validators/`
