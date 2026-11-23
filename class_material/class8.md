# Class 8 — Human Review and Durable Execution

Add a persistent review step to the screening workflow. A coordinator can resolve evidence, resume the same run, and recover interrupted work while preserving decisions and their history.

## 1. Pause with a Specific Review Request

**Example — an unresolved HbA1c conflict** for [TREC patient `trec-202129`][trec] and [trial NCT04450407][trial]:

```text
Original note: HbA1c 11.3%
Fictional test addendum: HbA1c 8.2% for the same encounter
Criterion: HbA1c 5.6–9.5%, inclusive
Current result: unknown / conflicting
Review question: Which source establishes the result for this encounter?
```

The 8.2% addendum is an invented test variant; keep it identified separately from the published patient.

- Present the criterion, both evidence references, assessment-time basis, and current report version. Offer actions to resolve evidence, retain uncertainty, or accept the report as presented.
- Store a stable `review_request_id` before entering the review node. Collect the reviewer's identity, action, rationale, and supporting source references.
- Keep review status separate from eligibility. Accepting a report acknowledges its findings, including remaining uncertainty or screening failures.

## 2. Persist the Pause and Resume the Same Run

**Example — a LangGraph review node:**

```python
from langgraph.types import Command, interrupt


def review_node(state):
    submission = interrupt({
        "review_request_id": state["review_request_id"],
        "report_version": state["report_version"],
        "question": state["review_question"],
        "evidence_refs": state["evidence_refs"],
    })
    return {"review_submission": submission}


# graph is compiled with a persistent checkpointer.
# run_id and validated_submission come from application storage.
config = {"configurable": {"thread_id": run_id}}
result = graph.invoke(
    Command(resume=validated_submission), config=config, durability="sync"
)
```

- Add `langgraph-checkpoint-postgres` through Pixi. Compile the graph with `PostgresSaver` against the application database, initialize its tables with `setup()`, and keep its connection open during execution. Lock the dependency version. [PostgreSQL checkpointer][postgres]
- On initial execution, pass the initial state; expose `result["__interrupt__"]` as the pending review. After restart, rebuild the same graph with the same database and `thread_id`. For a pending review use `Command(resume=...)`; inspect persisted state before resuming other interrupted work. [Interrupts][interrupts]
- The interrupted node runs again from its beginning. Put model calls and writes in separate nodes; keep the review node free of irreversible work before `interrupt()` and allow its pause signal to reach the runtime. [Interrupts][interrupts]

## 3. Apply Corrections and Reassess Dependencies

**Example — fictional review outcome:** a reviewed source confirms the original **11.3%** and identifies the 8.2% addendum as erroneous.

```text
Evidence revision v2 → HbA1c criterion: unknown → not_met
                     → parent logical groups recomputed
                     → patient aggregate recomputed
                     → report v2 returned for review
```

- Append a correction with provenance and retain the original records. Store the affected concept/source IDs so dependent evidence bundles, criteria, and logical groups can be invalidated.
- Reuse unaffected results only when their evidence, rule, and configuration versions still match. Broader profile changes may require rebuilding search documents and rerunning retrieval.
- Require review of the revised report before acceptance. Reject a submission targeting an outdated report version; retain the fixed assessment time unless a new screening run is requested.

## 4. Make Review and Report Writes Idempotent

**Example — the coordinator submits the same review twice after a network timeout:** both requests return the same accepted decision, producing one review event.

- In one database transaction, verify the pending request/report version, record the decision, and enqueue a durable resume job. Enforce uniqueness on `review_request_id`; an identical retry returns the saved response, while a different payload is rejected for reconciliation.
- Serialize workers for each run. Before resuming, check whether that decision has already been applied; persist reports by `(run_id, report_version)` and audit events by stable operation IDs.
- Treat writes as replayable: a process can fail after a database commit but before the next checkpoint. Checkpointing and idempotent application writes work together to prevent duplicate decisions. [Fault tolerance][faults]

## 5. Handle Timeouts, Retries, and Cancellation

**Example — a medication-history lookup times out:** allow one retry; after the second failed attempt, retain the completed checks and mark the medication criterion as unresolved due to a processing error.

- Start with a 10-second tool deadline and at most two attempts for classified transient failures. Set timeouts in the underlying client, use backoff, and count retries against the persisted call budget. Authentication and validation errors require correction rather than repeated calls.
- Track `running / waiting_review / completed / failed / cancelled` separately from clinical assessment. Return partial results with explicit failed or unassessed checks; distinguish a failed lookup from an empty successful result.
- Persist `cancel_requested` and check it before dispatch and report commits. Stop new work, attempt to cancel in-flight calls, and prevent late responses from marking a cancelled run complete. [Fault tolerance][faults]

## 6. Demonstrate Recovery Across a Restart

**Example — stop the worker while it is waiting for review, restart it, then submit the decision.** The same request and evidence should reappear, and the resumed run should produce one updated report.

| Scenario | Expected behavior |
|---|---|
| Restart during review | Same pending request restored |
| Crash after decision commit, before resume | Queued resume job eventually applies the saved decision |
| Duplicate submission | One accepted review event |
| Stale report version | Submission rejected without changing the report |
| Cancel during lookup | Partial work preserved; no completed report published |

- Test with the PostgreSQL checkpointer and a fresh worker process. Use `durability="sync"` for the recovery exercise so checkpoints are committed before the next graph step. [Checkpointers][checkpointers]
- For a recoverable failed execution with pending work and no review interrupt, continue from persisted state using `graph.invoke(None, config=...)`. Inspect status and pending tasks first; worker restart must not replay completed or cancelled jobs.
- Record run ID, report/evidence versions, review event, retry count, and final status. Confirm that completed work and audit history survive the restart.

## 7. Add Langfuse Observability and Evaluation

**Example — trace the HbA1c conflict and its reviewed correction:** group the initial assessment and resumed execution under one screening session. Each trace records the execution steps; evaluation scores describe the quality of a specific result.

Create a Langfuse project, obtain its API keys, and run `pixi add --pypi langfuse`. Add `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_BASE_URL` to your ignored `.env`, using your project's Cloud region or self-hosted URL. Load these before initializing the SDK and retain the resolved dependency version. [Integration setup][langfuse-tracing]

```python
from dotenv import load_dotenv
from langfuse import get_client
from langfuse.langchain import CallbackHandler

load_dotenv()
langfuse = get_client()
handler = CallbackHandler()  # A fresh handler for this invocation.
trace_config = {
    **config,  # Retains the persistent LangGraph thread_id.
    "callbacks": [*config.get("callbacks", []), handler],
    "run_name": "patient-screening",
    "metadata": {
        **config.get("metadata", {}),
        "langfuse_session_id": run_id,
        "report_version": report_version,
        "prompt_version": prompt_version,
    },
}
try:
    # invoke_input: initial state, Command(resume=...), or recovery input None.
    result = graph.invoke(invoke_input, config=trace_config, durability="sync")
finally:
    trace_id = handler.last_trace_id
    langfuse.flush()  # Flush before this short-lived worker exits.
```

- **Inspect execution:** verify node/tool calls, errors, retries, latency, token usage, and estimated cost where usage/pricing are available. Add explicit spans for backend operations outside the callback system. Start with public/synthetic cases; mask sensitive inputs and outputs before export.
- **Link resumes:** attach the callback on every invocation, preserve the session/run ID, and save each trace ID with its report, evidence, and prompt/model versions. Measure human waiting time separately from active execution. PostgreSQL remains the source of truth for checkpoints and review events. [Session grouping][langfuse-sessions]
- **Score results:** after review, use `langfuse.create_score` with the saved `trace_id` and, for criterion scores, `observation_id`. Define `criterion_correct` and `evidence_supported` as Boolean 0/1 scores against a reviewed answer for the **same input version**. A correct `unknown` before new evidence arrives remains correct. Persist `score_id`, score name, and timestamp so export retries reuse all three; publish scores through the durable queue. [Scores API][langfuse-scores]
- **Compare changes:** create a versioned Langfuse dataset of 10–20 reviewed synthetic/public cases. Keep expected labels outside model inputs and run `dataset.run_experiment(...)` for two prompt versions. Compare label agreement, evidence support, latency, and cost. Add a changed-evidence case as a separate version; report acceptance and clinical correctness as separate measures. [Dataset experiments][langfuse-eval]

## Deliverables

- A persistent review interrupt and resume endpoint with validated submissions.
- Correction/version handling and reassessment of affected results.
- Idempotent decision/report writes, a durable resume queue, and per-run worker coordination.
- Timeout, retry, cancellation, and partial-result handling with a recovery demonstration.
- Langfuse traces covering a pause/resume, review-linked scores, and a two-version evaluation experiment.
- A reviewed PR linked to an issue in your repository; Ruff and pytest passing.

## Sources

- Patient narrative: [TREC 2021 summaries][trec], row `trec-202129`. The conflicting addendum and its resolution are fictional test inputs.
- Trial: [NCT04450407][trial]; preserve the frozen criteria used for retrospective screening.
- Implementation: [interrupts][interrupts], [checkpointers][checkpointers], [PostgreSQL checkpoint setup][postgres], and [fault tolerance][faults].
- Observability and evaluation: [Langfuse integration][langfuse-tracing], [sessions][langfuse-sessions], [scores][langfuse-scores], and [dataset experiments][langfuse-eval].

[trec]: https://raw.githubusercontent.com/ncbi-nlp/TrialGPT/1b3242cd50c153a9c324661d4a0bd94fc68a4226/dataset/trec_2021/queries.jsonl
[trial]: https://clinicaltrials.gov/study/NCT04450407
[interrupts]: https://docs.langchain.com/oss/python/langgraph/interrupts
[checkpointers]: https://docs.langchain.com/oss/python/langgraph/checkpointers
[postgres]: https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres
[faults]: https://docs.langchain.com/oss/python/langgraph/fault-tolerance
[langfuse-tracing]: https://langfuse.com/integrations/frameworks/langchain
[langfuse-sessions]: https://langfuse.com/docs/observability/features/sessions
[langfuse-scores]: https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk
[langfuse-eval]: https://langfuse.com/docs/evaluation/get-started/offline
