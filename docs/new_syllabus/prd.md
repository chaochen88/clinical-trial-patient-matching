# Trial-to-Patient Screening PRD

## 1. Overview

Build a reproducible screening pipeline that helps coordinators identify potentially suitable patients for a selected clinical trial using structured AACT data, eligibility criteria, and an authorized patient collection. A chatbot helps coordinators describe screening needs and ask questions about saved results.

## 2. Problem

Screening a patient collection against a trial's eligibility criteria is slow and inconsistent. Trial requirements and patient evidence use different formats, and missing or conflicting records make candidate selection difficult to verify.

## 3. Goal

Given one selected trial, its frozen source version, an assessment time, and an authorized patient collection, return an explainable patient screening report for coordinator review. Save a reviewed patient shortlist with supporting evidence and unresolved questions. Potential suitability is not a definitive eligibility decision.

## 4. MVP Scope

- Load trial metadata and eligibility criteria from PostgreSQL.
- Select one trial by `nct_id`, assessment time, patient collection, and screening mode; block inactive trials in live-recruitment mode.
- Parse trial criteria into source-linked rules with Boolean and temporal qualifiers, retaining ambiguous requirements for review.
- Search an authorized patient collection containing demographics, diagnoses, medications, dated measurements, and medical history.
- Gather evidence per candidate and assess inclusion and exclusion criteria using Python checks and validated narrative interpretation.
- Return potential candidates, screening failures, and cases needing review, with criterion-level evidence and retrieval/ranking explanations.
- Preserve original trial text and patient evidence identifiers, quotes, dates, and source versions.
- Support a chatbot that confirms screening inputs, starts jobs through the API, and explains saved results with citations.
- Pause for coordinator review; apply confirmed corrections, rerun affected checks, and save an approved screening report and shortlist.

## 5. Out of Scope

- Medical diagnosis or treatment recommendations.
- Replacing investigator review or informed consent.
- Automatic enrollment or communication with trial sites.
- Processing raw patient records without explicit normalization and consent.
- Treating an automated match as a definitive eligibility decision.
- Patient-to-trial discovery in the initial release.

## 6. Users

- Trial coordinators and clinical researchers screening candidates for a selected study.
- Authorized care-team members reviewing patient evidence and unresolved screening questions.
- Data scientists evaluating trial-to-patient retrieval and assessment methods using approved test data.

## 7. Functional Requirements

### Data

- Use PostgreSQL as the local trial-data store.
- Keep raw datasets outside version control.
- Store trial identifiers, titles, statuses, study metadata, and eligibility criteria.
- Track the source and retrieval date for exported or processed records.
- Store normalized patient evidence with patient IDs, observation dates, units, and record provenance.
- Freeze trial, patient-data, and extracted-rule versions for each screening run.
- Restrict patient collection access by authenticated user and organization.

### Matching

- Normalize patient and trial attributes before comparison.
- Distinguish inclusion criteria from exclusion criteria.
- Keep evidence quality (`supported`, `missing`, `conflicting`) separate from condition applicability and screening outcome (`pass`, `fail`, `unknown`, `requires_review`). An applicable exclusion fails screening; a satisfied inclusion passes that criterion.
- Do not infer that missing patient information satisfies a criterion.
- Search the patient index and retain unknown evidence; exclude candidates only on verified contradictions with mandatory criteria.
- Explain retrieval and ranking factors for each candidate. Disclose candidate limits and incomplete search coverage.
- Aggregate verified mandatory inclusion failures or applicable exclusions as screening failures; otherwise unresolved criteria need review; otherwise label the patient a potential candidate.
- Bound evidence-gathering rounds and retries, and return visible partial results when budgets are exhausted.

### Chatbot and Review

- Convert the coordinator's description into a proposed trial ID, assessment time, authorized collection, and screening mode; require confirmation before starting a job.
- Keep conversation state separate from versioned screening-run state and associate follow-up questions with a specific run and patient.
- Answer result questions by retrieving saved assessments and citing trial criteria and dated patient evidence; state when supporting information is unavailable.
- Treat information supplied in chat as a proposed correction until confirmed and recorded with provenance; do not silently overwrite patient records.
- Use authenticated, validated tools and API endpoints for start, status, correction/resume, and approval. Approval is an explicit authorized coordinator action.
- Rerun affected assessments after confirmed corrections and preserve prior results and audit events.

### Output

Each screening report should include:

- `nct_id`
- `brief_title`
- `overall_status`
- `run_id`, `assessment_time`, trial/rule/patient-data versions, and job/review status
- candidate IDs and retrieval scores or ranking values with explanations
- per-patient screening outcome and criterion-level assessment details
- patient evidence IDs, source quotes, dates, units, and evidence quality
- unknown or conflicting data, unresolved questions, errors, and coverage limits
- source metadata and the reviewed shortlist with reviewer and audit-event identifiers

## 8. Non-Functional Requirements

- Run reproducibly through Pixi and Docker.
- Use deterministic Python assessment for identical normalized inputs and versions; version and cache model extraction/interpretation outputs, and evaluate their variability.
- Avoid committing raw or sensitive data to Git.
- Provide structured logs for pipeline failures and skipped records.
- Keep the initial implementation testable without access to production patient data.
- Execute screening jobs in background workers with durable checkpoints, bounded concurrency, and idempotent resume and approval operations.
- Protect patient evidence in storage, tools, logs, and conversations; disable external tracing for restricted data and use approved synthetic inputs in public demos.
- Measure trial-to-patient retrieval coverage, assessment correctness, explanation fidelity, latency, and model cost.

## 9. Acceptance Criteria

- A local developer can start PostgreSQL and load the configured AACT dump.
- A known trial can be queried by `nct_id` and exported as valid JSON.
- A selected trial can be evaluated against a small approved synthetic patient collection at a specified assessment time.
- The system reports unknown criteria separately from confirmed mismatches.
- Results include enough source information for a reviewer to verify the output.
- Raw data remains excluded from Git commits and pushes.
- Tests verify inclusion/exclusion polarity, numerical and temporal boundaries, missing/conflicting evidence, and candidate truncation reporting.
- A coordinator can confirm chatbot inputs, start a screening job, and ask why a specific patient passed, failed, or needs review; answers cite stored evidence.
- A paused review can resume after worker restart, and repeated approval requests do not duplicate the saved shortlist or audit event.
- Confirmed corrections rerun affected checks and preserve the previous assessment version.

## 10. Initial Milestones

1. Finalize database schema and loading workflow.
2. Add trial and patient data models.
3. Add versioned eligibility-criteria parsing, source validation, and review labels.
4. Implement patient retrieval and dated evidence gathering.
5. Implement criterion assessment, candidate ranking, and evidence output.
6. Add durable screening jobs, human review, correction/resume, and audited shortlist persistence.
7. Add the chatbot for confirmed job inputs and evidence-grounded follow-up questions.
8. Add direction-appropriate evaluations and an end-to-end synthetic example; validate access controls and deployment behavior before real patient use.

## 11. Future Work: Evidence-Investigation Agent

After the fixed extraction and criterion-assessment workflows are implemented and evaluated, add an optional, user-directed evidence-investigation agent as a backend service alongside the screening workflows. The chatbot is its main conversational entry point; the human-review UI can invoke the same service through an "Investigate missing evidence" action. A coordinator can ask it to find information in available patient records and determine whether missing measurements can be derived from other evidence. The agent chooses approved retrieval and calculation tools within the selected patient, screening run, and assessment-time context. This extension is outside the MVP acceptance criteria and is not implemented yet.

### Backend placement and entry points

- The chatbot and review UI submit investigation requests through authenticated FastAPI endpoints. Both use the same backend agent, tools, validation, and audit path.
- FastAPI validates the request and access scope, creates an investigation job, and returns an `investigation_id`. A background worker executes the agent; either interface can retrieve status and source-linked findings.
- Each investigation references the screening `run_id`, `patient_id`, assessment time, frozen assessment/evidence versions, and the user's question. Clarify missing patient or run context before starting. Conversation state references the investigation rather than becoming the source of clinical evidence.
- The agent returns proposed evidence and calculations to a reviewable findings store. The fixed workflow validates these findings, and the existing human confirmation/resume path applies accepted evidence and reruns affected criteria. If the referenced assessment changed while the investigation was running, require review against the current version before applying findings.
- A request to investigate authorizes the scoped search and approved calculations. Confirmation is a separate action for applying findings to screening results; it is not required for every read-only tool call. Shortlist approval remains distinct.

For example, when BMI is missing, the agent could search for dated height and weight, check their suitability for the screening encounter, and call a tested Python BMI calculator. The derived evidence must retain source IDs, dates, units, calculation inputs, and method before it is submitted to the existing assessment workflow. If suitable evidence cannot be found, the criterion remains unknown.

### Example user interaction

1. The coordinator asks the chatbot: "BMI is missing for this patient. Can you find height and weight and calculate it?" Alternatively, they select the same investigation from the human-review UI.
2. FastAPI starts a scoped backend investigation. The agent searches authorized records for the selected patient, including structured measurements and available notes, respecting the assessment cutoff.
3. It returns the evidence found with dates, units, and source references. An approved validation tool checks that the inputs are compatible and appropriate for the same assessment; conflicting or unsuitable measurements remain unresolved.
4. If valid inputs exist, the agent calls a deterministic calculator using `BMI = weight_kg / height_m ** 2`. For example, synthetic inputs of 80 kg and 1.75 m yield approximately 26.1 kg/m2. The response labels this as calculated BMI and shows the inputs, formula, and sources, separately from any directly recorded BMI.
5. If inputs are missing, ambiguous, or unsuitable, the agent explains why a calculation cannot be supported and identifies the remaining information needed. It does not guess missing values.
6. The coordinator may review and confirm proposed evidence through the existing review flow. Only confirmed evidence triggers versioned reassessment; answering a chat question does not silently modify patient records or screening outcomes.

The same interaction may support other missing values when an approved calculator defines the required inputs, units, temporal compatibility, and validation rules. The agent must report whether evidence was found, a supported calculation was possible, or the request remains unresolved. This is on-demand investigation requested by the user; it does not automatically investigate every missing field.

### Tools, limits, and review

- Provide scoped, read-only patient-record retrieval tools and approved deterministic calculation tools. Enforce patient/collection access and the assessment-time cutoff on every call.
- Bound tool calls, investigation rounds, latency, and model cost; return partial findings and unresolved questions when limits are reached.
- Keep numerical calculations, criterion rules, verification, and versioned result persistence in the tested workflows. The agent may propose evidence for reassessment; it cannot silently resolve contradictions, overwrite records, approve shortlists, or authorize enrollment.
- Record investigation steps and source references in the run audit trail, within existing data-access and logging restrictions. Preserve prior assessments when confirmed evidence triggers reassessment.
- Evaluate whether investigation resolves information gaps correctly compared with the fixed workflow, including unsupported derivations, temporal leakage, explanation fidelity, and added cost, before enabling it for coordinators.
