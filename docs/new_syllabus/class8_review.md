# Class 8: persistent review and durable execution

The new review workflow wraps the existing deterministic criterion engine in a
PostgreSQL-checkpointed LangGraph. It preserves the frozen trial, patient variant,
assessment cutoff/time basis, and source references. Class 7's evidence agent and
its current defaults remain available separately.

## Implemented flow

```text
assess all criteria → optional bounded lookup → reassess
       → save report/request → review interrupt
       → validated queued submission → apply review
           ├─ resolve evidence → append correction → reassess → new review
           └─ accept report / retain uncertainty → completed
```

The default demonstration uses the existing public `trec-202129` conflict variant
and frozen `NCT04450407` trial from `data/class6`. The 8.2% addendum and coordinator
decisions are explicitly fictional. The original published narrative is unchanged.
The undated narrative retains `described_presentation`; no calendar date is invented.

## Setup and commands

Run commands in the repository's WSL environment:

```bash
pixi install --locked
docker compose up -d aact-postgres
# One-time creation of an additional database, if not already present:
docker exec aact-postgres createdb -U postgres screening_class8
docker exec aact-postgres createdb -U postgres screening_class8_tests
pixi run class8-init
pixi run class8-demo --langfuse
pixi run class8-api
```

The existing AACT database is not used for review tables. Set
`CLASS8_DATABASE_URL` in ignored `.env` to the dedicated review database.
`.env.example` lists the required settings without real credentials. The locked
environment includes `langgraph-checkpoint-postgres`, `psycopg[binary]`, Langfuse,
FastAPI, and Uvicorn. This implementation was exercised with Langfuse 5.2.0 and
the PostgreSQL checkpointer 3.2.1; it uses the installed v5 score-read interface.

The coordinator API is available at **http://127.0.0.1:8008/docs**. Its interactive
forms provide a local way to inspect requests and submit decisions. It binds to
localhost and does not implement production authentication or a custom frontend.

Start a new case from frozen inputs:

```bash
pixi run python -m clinical_trial_patient_matching.review_cli create \
  --profile data/class6/trec-conflict-profile.json \
  --trial data/class6/NCT04450407-trial.json
# Use the printed run ID:
pixi run class8-worker --run-id RUN_ID --langfuse
pixi run python -m clinical_trial_patient_matching.review_cli status RUN_ID
```

For dated profiles, supply both `--assessment-time` with a timezone-aware value
and `--time-basis calendar`. An undated case must use `described_presentation`.
The CLI loads the project `.env` using an explicit path, independent of the
worker's current directory.

## Coordinator review

1. `GET /runs` lists runs and their workflow status.
2. `GET /runs/{run_id}` returns the latest report, pending review, partial checks,
   and human review waiting time separately from execution time.
3. `POST /runs/{run_id}/review` validates and saves a decision, returning HTTP 202.
4. A separate worker invocation consumes the durable job. Run
   `pixi run class8-worker --langfuse` to process resumable runs once. An external
   scheduler can invoke this command periodically; the API does not rely on an
   in-process background task to keep a decision alive.

Example submission, using IDs from the pending review:

```json
{
  "review_request_id": "COPY_FROM_PENDING_REVIEW",
  "report_version": 1,
  "reviewer": "coordinator-identity",
  "action": "resolve_evidence",
  "rationale": "Reviewed source confirms 11.3%; fictional addendum is erroneous.",
  "supporting_source_refs": ["ORIGINAL_FACT_ID"],
  "invalidated_source_refs": ["FICTIONAL_ADDENDUM_FACT_ID"]
}
```

Allowed actions are `resolve_evidence`, `retain_uncertainty`, and `accept_report`.
Resolution requires supporting and invalidated evidence from the current request.
The system rejects unknown IDs, overlapping selections, invalidated supporting
evidence, and stale report versions. Reviewer identity and rationale cannot be
blank. For acceptance or retaining uncertainty, omit invalidated IDs.

Acceptance acknowledges the report's findings; it never overrides eligibility.
Resolution produces a new profile/report version and another review request. All
criteria and parent groups are recomputed conservatively; this implementation
does not reuse potentially stale assessments. Corrections retain original facts
and record their invalidation, affected concepts, reviewer, rationale, sources,
and previous/new evidence versions. Broader search-profile corrections would
require a separate retrieval rebuild; this local correction path handles evidence
resolution within a saved screening case.

## Persistence and replay guarantees

Application tables live in the `screening` schema; LangGraph owns its checkpoint
tables in the same dedicated database. `PostgresSaver.setup()` initializes the
checkpoint schema, and its connection remains open for each graph invocation.
Every invocation uses the run ID as `thread_id` and `durability="sync"`.

- A stable review request ID is saved before entering the interrupt node.
- Review nodes have no writes or model calls before `interrupt()`.
- A single transaction locks the run, validates the pending version, records the
  unique decision, and inserts its resume job.
- Identical retries return the saved response. Changed payloads conflict.
- Reports are unique by `(run_id, version)`; replay must produce the same payload.
- Audit operations have stable unique IDs. Old reports and requests are retained.
- A PostgreSQL session advisory lock serializes workers per run. Busy workers
  return without dispatching duplicate work.
- Worker lock connections use autocommit, and checkpoint migrations have a
  separate setup lock. This prevents fresh-database concurrent index creation
  from waiting on a worker's own open transaction.
- Workers inspect checkpoints before choosing initial input, `Command(resume=...)`,
  or recovery input `None`. Terminal runs are not restarted.
- Applied decision flags let workers reconcile queued work after a crash between
  application commits and the next graph checkpoint.

## Timeouts, retries, cancellation, and partial work

The optional worker-owned tool registry accepts callables returning the existing
tool-result contract. Each callable receives `timeout=10` and must set that
deadline in its underlying client. Tool names are supplied by trusted run setup,
not by coordinator submissions. These checks record lookup availability; profile
enrichment remains the separate Class 7 responsibility.

Timeout and connection exceptions are classified transient and receive at most
one retry with backoff. Validation/authentication failures are not retried. The
application persists each reservation before dispatch and permits at most four
attempts per run, including attempts interrupted by a crash. Replaying a reserved
attempt uses its saved result or records it as interrupted rather than issuing
the same read again.

Completed criterion checks are saved individually. Failed/incomplete lookups
produce explicit `unknown` results with failed processing; an empty successful
lookup is distinct. The workflow status is independent of clinical assessment.

`POST /runs/{run_id}/cancel` persists cancellation. The worker checks it before
dispatch, between checks, and under the run lock before publishing or accepting
a report. During a tool call a watcher attempts the registered client's optional
`cancel()` method. Cancellation is best effort at the client boundary; a late
response cannot publish a completed result. Partial checks remain available.

## Langfuse traces and reviewed scores

`--langfuse` attaches a fresh callback on every actual graph invocation and groups
traces under the run's session ID. Explicit spans cover criterion assessment and
backend lookups. Trace IDs, criterion observation IDs, report/evidence versions,
prompt/model versions, and active execution time are stored in PostgreSQL.

Raw callback inputs and outputs are masked before export. Trace metadata retains
identifiers needed for debugging; traces are not the authoritative review store.
The deterministic assessment graph has no model-token cost. The separate prompt
experiment records actual model usage.

`POST /runs/{run_id}/scores` accepts a saved review ID, report/profile versions,
reviewed criterion labels, and explicit Boolean evidence-support judgments. It
only scores matching input versions and matching criterion observations. Correct
`unknown` results before a correction are not scored against later evidence.
Report acceptance is never automatically treated as clinical correctness.

Scores are persisted to the durable queue with stable IDs, names, and timestamps.
Retries reuse those values, and changing a queued score is rejected. Export them:

```bash
pixi run python -m clinical_trial_patient_matching.review_cli export-scores
```

Export waits briefly for server-side visibility before acknowledging jobs. If
ingestion or the network is unavailable, jobs remain queued for a later run.

## Dataset experiment and recorded results

```bash
# No paid calls: write the 12 reference cases for review.
pixi run class8-evaluate
# 24 bounded paid model calls, two prompt versions, with Langfuse experiments:
pixi run class8-evaluate --live
```

The versioned dataset is `class8-hba1c-synthetic-v1`. It includes inclusive
boundaries, missing evidence, conflicts, changed evidence as a separate version,
family history, targets, unsupported units, and unavailable future evidence.
The task passes only `item.input` to the model; expected labels remain in evaluators.

Recorded development run:

| Prompt | Cases | Label agreement | Exact evidence-ID agreement | Mean seconds | Input/output tokens |
|---|---:|---:|---:|---:|---:|
| v1 | 12 | 100% | 58.3% | 1.406 | 10,547 / 1,512 |
| v2 | 12 | 100% | 100% | 1.326 | 11,795 / 1,392 |

Monetary cost is left unspecified because pricing was not supplied; token usage
is measured. These are small synthetic references approved by the project owner
on November 19, 2026 after reviewing the
[reference worksheet](class8_reference_review.md). They are not clinically
validated ground truth. The approval and hash bind the review to the exact
inputs/expected outputs. Human report acceptance is not an experiment quality metric.

Local ignored artifacts are in `data/class8/demo/` and `data/class8/evaluation/`.
The demo saves both reports, request/decision identities, trace IDs, active timing,
and a summary. It starts a fresh worker at every step and verifies restored review
identity, duplicate response equality, a version-2 report, and final acceptance.

The completed demonstration run `85e14b4d-c1b2-4124-81f3-f79145aa02ce` generated
three traces and four server-confirmed review-linked scores. In Langfuse, search
that session ID. The two experiment runs are named `class8-review-v1` and
`class8-review-v2`.

`python scripts/verify_class8.py` checks the saved demo against server-side
observations: session IDs, criterion observation IDs, masking, and four scores.
The latest run passed this verification. Earlier development traces contain
public fixture payloads from before interrupt-message export masking was added.
Both ordinary payloads and interrupt status messages are now masked before export.

## Validation and handoff

Final local validation on November 19, 2026:

- **109 tests passed**, including the 13 Class 8 tests, in 146.53 seconds.
- PostgreSQL recovery tests passed from an empty, isolated test database.
- `pixi run check` passed: source lint and formatting checks succeeded.
- New tests and demonstration/verification scripts also passed Ruff checks.
- The running API's OpenAPI endpoint returned HTTP 200.
- Langfuse verification confirmed all three demo traces (44 observations), matching
  session/criterion IDs, masked source/reviewer payloads, and four review-linked scores.
- Pytest emitted two upstream Google SDK Python-support warnings; no test failures.

```bash
pixi run check
pixi run test
```

`tests/test_class8.py` constructs its own fictional patient and trial. Its recovery
tests use a real PostgreSQL checkpointer and subprocess workers; no model calls
are made by pytest. Set `CLASS8_TEST_DATABASE_URL` to a separate test database;
tests refuse to use the application database. They skip without test configuration.
The live public demo additionally requires the existing
Class 6 frozen inputs.

Issue and PR drafts are in [class8_handoff.md](class8_handoff.md). A human-reviewed
PR remains an external review step; automated tests and fictional demonstration
decisions are not substitutes.

## Sources

- [Class requirements](../../class_material/class8.md)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [PostgreSQL checkpointer](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres)
- [Langfuse dataset experiments](https://langfuse.com/docs/evaluation/get-started/offline)
- [Langfuse score API](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk)
