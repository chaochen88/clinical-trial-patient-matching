# Class 8 synthetic reference review

These 12 cases are fictional HbA1c examples. They test interpretation and evidence
selection, not clinical effectiveness. The inclusion predicate is: **patient HbA1c
5.6–9.5%, inclusive, for this encounter**, using information available by
January 13, 2021 at 23:59:59 UTC. No reviewed unit conversion is provided.

The reference labels below were authored during implementation and approved by
the project owner after review on November 19, 2026. This is a human reference
review, not a claim of clinical expert validation. Accepting a screening report
is separate from approving these reference labels.

| Case | Evidence | Expected predicate | Expected evidence IDs | Reason |
|---|---|---|---|---|
| inside | e1: patient result 8.2% | met | e1 | Within inclusive bounds |
| below | e1: patient result 5.5% | not_met | e1 | Below lower bound |
| above | e1: patient result 11.3% | not_met | e1 | Above upper bound |
| lower-bound | e1: patient result 5.6% | met | e1 | Lower bound included |
| upper-bound | e1: patient result 9.5% | met | e1 | Upper bound included |
| missing | No observations | unknown | none | No usable evidence |
| conflict-v1 | e1: original 11.3%; e2: fictional same-encounter 8.2% addendum, unresolved | unknown | e1, e2 | Conflict cannot be resolved automatically |
| family | e1: mother's HbA1c 8.2%; no patient result | unknown | none | Family result does not establish patient's value |
| target-only | e1: HbA1c not measured; 8.2% is a target | unknown | none | Target is not a measurement |
| conflict-resolved-v2 | e1: 11.3% confirmed by fictional review; e2: fictional 8.2% addendum marked erroneous | not_met | e1 | Reviewed original establishes value; changed input is a separate case |
| unsupported-unit | e1: 60 mmol/mol; no reviewed conversion | unknown | none | Does not establish the required value in percent |
| future-availability | e1: 8.2% measured January 12 but first available January 15 | unknown | none | Unavailable at the assessment cutoff |

Review all labels and citation expectations. Record any requested changes before
using these as reviewed ground truth. Exact evidence-ID agreement is an authored
citation convention; it is not a general clinical entailment measure.

Reviewer: **Project owner, by explicit approval in the Codex conversation**
Review date: **November 19, 2026**
Decision/rationale: **Approved all 12 labels and evidence-ID expectations after
reviewing the worksheet.** The matching reference hash and approval are recorded
in `class8_reference_review.json` and the Langfuse dataset metadata.
