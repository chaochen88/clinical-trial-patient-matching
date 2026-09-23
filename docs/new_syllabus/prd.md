# Clinical Trial Patient Matching PRD

## 1. Overview

Build a reproducible pipeline that helps identify clinical trials relevant to a patient's profile using structured AACT data and eligibility criteria.

## 2. Problem

Clinical trial eligibility information is difficult to search and compare manually. Patient information and trial criteria are often expressed in different formats, making relevant-trial discovery slow and inconsistent.

## 3. Goal

Given a patient profile, return a ranked list of potentially matching clinical trials with clear evidence for each match and mismatch.

## 4. MVP Scope

- Load trial metadata and eligibility criteria from PostgreSQL.
- Accept a structured patient profile containing demographics, diagnoses, medications, measurements, and relevant medical history.
- Filter trials by basic structured constraints such as age, sex, recruitment status, study type, and location when available.
- Compare patient attributes with inclusion and exclusion criteria.
- Return matching trials with the trial identifier, title, status, and matched criteria.
- Preserve the source text used to support each result.

## 5. Out of Scope

- Medical diagnosis or treatment recommendations.
- Replacing investigator review or informed consent.
- Automatic enrollment or communication with trial sites.
- Processing raw patient records without explicit normalization and consent.
- Treating an automated match as a definitive eligibility decision.

## 6. Users

- Clinical researchers screening candidates for studies.
- Care teams looking for potentially relevant trials.
- Data scientists evaluating patient-trial matching methods.

## 7. Functional Requirements

### Data

- Use PostgreSQL as the local trial-data store.
- Keep raw datasets outside version control.
- Store trial identifiers, titles, statuses, study metadata, and eligibility criteria.
- Track the source and retrieval date for exported or processed records.

### Matching

- Normalize patient and trial attributes before comparison.
- Distinguish inclusion criteria from exclusion criteria.
- Label each criterion as matched, unmatched, unknown, or requiring review.
- Do not infer that missing patient information satisfies a criterion.
- Explain the factors contributing to each trial's ranking.

### Output

Each result should include:

- `nct_id`
- `brief_title`
- `overall_status`
- `match_score` or ranking value
- criterion-level match details
- unknown or conflicting data
- source metadata

## 8. Non-Functional Requirements

- Run reproducibly through Pixi and Docker.
- Use deterministic matching behavior for the same inputs and dataset version.
- Avoid committing raw or sensitive data to Git.
- Provide structured logs for pipeline failures and skipped records.
- Keep the initial implementation testable without access to production patient data.

## 9. Acceptance Criteria

- A local developer can start PostgreSQL and load the configured AACT dump.
- A known trial can be queried by `nct_id` and exported as valid JSON.
- A patient profile can be evaluated against at least one trial's structured metadata and eligibility text.
- The system reports unknown criteria separately from confirmed mismatches.
- Results include enough source information for a reviewer to verify the output.
- Raw data remains excluded from Git commits and pushes.

## 10. Initial Milestones

1. Finalize database schema and loading workflow.
2. Add trial and patient data models.
3. Implement structured filtering.
4. Add eligibility-criteria parsing and review labels.
5. Implement ranking and evidence output.
6. Add unit tests and an end-to-end example.
