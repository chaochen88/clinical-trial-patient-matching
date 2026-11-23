# Class 8 issue and PR drafts

## Issue draft

**Title:** Add durable coordinator review and screening recovery

A screening case with conflicting evidence needs a persisted review request,
validated coordinator decisions, and recovery after a worker restart. Implement
versioned corrections, idempotent writes, queued resume jobs, cancellation and
partial results, and Langfuse tracing/evaluation.

Acceptance criteria:

- [x] PostgreSQL-backed review interrupt and validated resume endpoint.
- [x] Evidence corrections produce a new report that requires review.
- [x] Idempotent decisions/reports and durable resume jobs.
- [x] Per-run worker coordination, retry budgets, cancellation and partial work.
- [x] Fresh-process recovery and crash-boundary integration tests.
- [x] Langfuse pause/resume traces and server-confirmed review-linked scores.
- [x] Two live prompt experiments across 12 versioned synthetic cases.
- [x] Project-owner review and explicit approval of the 12 reference cases.
- [ ] Human-reviewed PR linked to the issue.

## PR draft

**Title:** Persist screening review decisions and resume versioned assessments

When a screening report needs human review, the workflow now persists its report
and request before pausing. Validated coordinator decisions are saved together
with a durable resume job. Workers restore the same checkpoint across processes,
apply corrections with provenance, and require review of the revised report.
Duplicate submissions reuse the saved response; stale or conflicting submissions
are rejected.

The local coordinator API exposes run inspection, review, cancellation, and score
submission. Worker-owned lookups have persisted attempt reservations and bounded
transient retries; partial checks survive failure and cancellation. Per-run
PostgreSQL advisory locks prevent concurrent execution.

Langfuse integration masks graph payloads, groups resumes by screening session,
and saves trace/criterion IDs alongside evidence/report versions. Review-linked
scores use an idempotent export queue. The separate experiment compares two real
prompt versions on 12 authored synthetic inputs, keeping labels outside prompts.

**Validation:** 109 pytest tests passed, including 13 Class 8 tests and recovery
from an empty, isolated PostgreSQL database. Ruff lint and formatting passed.
The live pause/resume demonstration and server-side Langfuse verification passed:
three traces, 44 observations, and four review-linked scores. See the implementation
guide for experiment results and artifact locations.

**Review focus:** replay after application commits, stale report rejection,
cancellation before publication, evidence-version score matching, and data masking.

**Limits:** local API without production auth; conservative full reassessment;
client providers must enforce tool deadlines; synthetic references have project-owner
approval, not clinical expert validation. A separate scheduler invokes the one-pass worker.

**Publication scope:** the workspace already contains uncommitted Class 7 and
other work. Review the intended base branch and prerequisite files before staging
or publishing. Do not include unrelated changes or `.env`/ignored data artifacts.
Replace this draft's issue reference with the actual linked issue when published.
