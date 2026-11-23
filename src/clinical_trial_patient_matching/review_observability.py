"""Opt-in masked Langfuse traces and an idempotent score export queue."""

import os
import time
from contextlib import contextmanager
from datetime import datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from clinical_trial_patient_matching.review_workflow import PROMPT_VERSION


def redact_payload(*, data, **kwargs):
    # Raw graph state includes source narratives and reviewer identity. Export
    # metadata and explicit span identity, never callback inputs/outputs.
    return "[screening payload redacted]"


def redact_export(*, params):
    """Interrupt exception messages also contain the complete review payload."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for identifier, span in params.spans.items():
        sensitive = tuple(
            key
            for key in span.attributes
            if any(
                part in key.lower()
                for part in (
                    "input",
                    "output",
                    "exception",
                    "status_message",
                    "statusmessage",
                    "error.message",
                    "error.stack",
                )
            )
            and not (
                ("usage" in key.lower() or "tokens" in key.lower())
                and isinstance(span.attributes[key], (int, float))
            )
        )
        patches[identifier] = OtelSpanPatch(delete_attributes=sensitive)
    return MaskOtelSpansResult(span_patches=patches)


class ReviewTelemetry:
    def __init__(self, store):
        from langfuse import Langfuse

        required = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")
        if not all(os.getenv(k) for k in required):
            raise RuntimeError(
                "Save LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, and LANGFUSE_"
                "BASE_URL in .env"
            )
        self.store = store
        self.client = Langfuse(
            mask=redact_payload,
            mask_otel_spans=redact_export,
            timeout=15,
        )
        self.observations = {}
        self.result = None

    @contextmanager
    def invocation(self, run_id, state):
        from langfuse import propagate_attributes
        from langfuse.langchain import CallbackHandler

        self.observations, self.result = {}, None
        invocation_id = str(uuid4())
        trace_id = self.client.create_trace_id(seed=invocation_id)
        handler = CallbackHandler(trace_context={"trace_id": trace_id})
        started = time.monotonic()
        try:
            with propagate_attributes(
                session_id=run_id,
                metadata={
                    "prompt_version": PROMPT_VERSION,
                    "model_version": "deterministic",
                    "evidence_version": state["profile"]["profile_version"],
                    "report_version": str(state["report_version"]),
                },
            ):
                with self.client.start_as_current_observation(
                    name="patient-screening",
                    trace_context={"trace_id": trace_id},
                ):
                    yield {
                        "callbacks": [handler],
                        "run_name": "patient-screening",
                        "metadata": {
                            "langfuse_session_id": run_id,
                            "prompt_version": PROMPT_VERSION,
                        },
                    }
        finally:
            final = self.result or state
            with self.store.connection() as conn:
                conn.execute(
                    "INSERT INTO screening.traces VALUES (%s,%s,%s,%s,%s,%s,%s,%s"
                    ",%s,now())",
                    (
                        invocation_id,
                        run_id,
                        trace_id,
                        final["report_version"],
                        final["profile"]["profile_version"],
                        PROMPT_VERSION,
                        "deterministic",
                        Jsonb(self.observations),
                        time.monotonic() - started,
                    ),
                )
            self.client.flush()

    @contextmanager
    def span(self, name, criterion_id):
        with self.client.start_as_current_observation(
            name=name,
            metadata={"criterion_id": criterion_id},
        ) as span:
            if name == "assess-criterion":
                self.observations[criterion_id] = span.id
            yield span


def enqueue_reviewed_scores(
    store,
    run_id,
    review_id,
    report_version,
    profile_version,
    labels,
    evidence_supported,
):
    """Labels and support judgments are explicitly supplied by a reviewer.

    Acceptance of the report never supplies correctness labels automatically.
    """
    with store.connection() as conn:
        review = conn.execute(
            "SELECT d.request_id FROM screening.decisions d JOIN screening.requests r "
            "USING(request_id) WHERE r.run_id=%s AND r.version=%s AND d.request_id=%s",
            (run_id, report_version, review_id),
        ).fetchone()
        report = conn.execute(
            "SELECT payload FROM screening.reports WHERE run_id=%s AND version=%s",
            (run_id, report_version),
        ).fetchone()
        traces = conn.execute(
            "SELECT * FROM screening.traces WHERE run_id=%s AND report_ve"
            "rsion=%s AND evidence_version=%s",
            (run_id, report_version, profile_version),
        ).fetchall()
    if (
        not review
        or not report
        or report["payload"]["profile_version"] != profile_version
    ):
        raise ValueError("Scores must match a saved review and the same input version")
    results = {r["criterion_id"]: r for r in report["payload"]["results"]}
    if not set(labels) <= results.keys() or set(evidence_supported) != set(labels):
        raise ValueError(
            "Score labels and support judgments must target existing criteria"
        )
    if any(
        v not in {"met", "not_met", "unknown", "not_applicable"}
        for v in labels.values()
    ):
        raise ValueError("Invalid reviewed criterion label")
    if any(type(v) is not bool for v in evidence_supported.values()):
        raise ValueError("Evidence support requires explicit Boolean judgments")
    scores = []
    for trace in traces:
        for criterion_id, expected in labels.items():
            observation = trace["observations"].get(criterion_id)
            if not observation:
                continue
            for name, value in (
                ("criterion_correct", results[criterion_id]["status"] == expected),
                ("evidence_supported", evidence_supported[criterion_id]),
            ):
                scores.append(
                    {
                        "name": name,
                        "value": int(value),
                        "trace_id": trace["trace_id"],
                        "observation_id": observation,
                        "data_type": "BOOLEAN",
                        "metadata": {
                            "review_id": review_id,
                            "criterion_id": criterion_id,
                            "report_version": report_version,
                            "profile_version": profile_version,
                        },
                    }
                )
    if not scores:
        raise ValueError("No matching criterion traces; run with --langfuse first")
    store.queue_scores(run_id, review_id, scores)
    return len(scores)


def export_scores(store, client):
    # Session-level lock prevents two exporters submitting the same pending job.
    with store.connection() as conn:
        if not conn.execute(
            "SELECT pg_try_advisory_xact_lock(812008) AS locked"
        ).fetchone()["locked"]:
            return 0
        jobs = conn.execute(
            "SELECT * FROM screening.jobs WHERE kind='score' AND NOT done"
            " ORDER BY job_id"
        ).fetchall()
        for job in jobs:
            payload = dict(job["payload"])
            payload["timestamp"] = datetime.fromisoformat(payload["timestamp"])
            client.create_score(**payload)
        # Check server visibility before acknowledging the durable jobs. This
        # also catches background exporter failures hidden by an SDK flush.
        client.flush()
        for job in jobs:
            for attempt in range(8):
                if client.api.scores.get_many(id=job["job_id"]).data:
                    break
                time.sleep(min(0.5 * 2**attempt, 3))
            else:
                raise RuntimeError("Score not yet visible; durable export job retained")
            conn.execute(
                "UPDATE screening.jobs SET done=true WHERE job_id=%s", (job["job_id"],)
            )
        return len(jobs)
