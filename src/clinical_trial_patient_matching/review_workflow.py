"""Durable, versioned human review around the existing criterion engine."""

import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Literal, TypedDict

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import Field, field_validator

from clinical_trial_patient_matching.assessment import (
    CriterionResult,
    ScreeningReport,
    Trial,
    aggregate,
    assess,
    validate_citations,
)
from clinical_trial_patient_matching.extraction import digest
from clinical_trial_patient_matching.patients import Profile, StrictModel, reconcile
from clinical_trial_patient_matching.review_store import CancelledRun, ReviewStore

PROMPT_VERSION = "class8-review-v1"


class ReviewSubmission(StrictModel):
    review_request_id: str = Field(min_length=1)
    report_version: int = Field(ge=1)
    reviewer: str = Field(min_length=1)
    action: Literal["resolve_evidence", "retain_uncertainty", "accept_report"]
    rationale: str = Field(min_length=1)
    supporting_source_refs: list[str] = Field(default_factory=list)
    invalidated_source_refs: list[str] = Field(default_factory=list)

    @field_validator("reviewer", "rationale")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Reviewer and rationale cannot be blank")
        return value.strip()


class ReviewState(TypedDict, total=False):
    run_id: str
    profile: dict
    trial: dict
    assessment_time: str | None
    time_basis: str
    report_version: int
    report: dict
    request: dict
    review_submission: dict
    finished: bool
    tool_checks: dict
    tool_failures: list[str]
    lookup_index: int


def initial_review_state(
    profile, trial, cutoff=None, time_basis="calendar", tool_checks=None
):
    profile = Profile.model_validate(profile)
    trial = Trial.model_validate(trial)
    if cutoff is None and time_basis != "described_presentation":
        raise ValueError("An undated case requires described_presentation")
    if cutoff is not None and cutoff.utcoffset() is None:
        raise ValueError("Assessment time must be timezone-aware")
    checks = tool_checks or {}
    if not set(checks) <= {c.criterion_id for c in trial.criteria}:
        raise ValueError("Tool check references an unknown criterion")
    return {
        "profile": profile.model_dump(mode="json"),
        "trial": trial.model_dump(mode="json"),
        "assessment_time": cutoff.isoformat() if cutoff else None,
        "time_basis": time_basis,
        "tool_checks": checks,
        "tool_failures": [],
        "lookup_index": 0,
    }


def build_review_graph(store, checkpointer, tools=None, telemetry=None):
    """Tools are worker-owned clients: callable(timeout=10) -> ToolResult.

    They must enforce the supplied deadline in their underlying client. No tool
    name or URL is taken from an untrusted review submission. This extension
    records lookup availability; sourced profile enrichment remains Class 7's job.
    """
    tools = tools or {}

    def call_tool(tool, run_id):
        finished = threading.Event()

        def watch_cancellation():
            while not finished.wait(0.1):
                if store.get(run_id)["cancel_requested"]:
                    cancel = getattr(tool, "cancel", None)
                    if cancel:
                        cancel()
                    return

        watcher = threading.Thread(target=watch_cancellation, daemon=True)
        watcher.start()
        try:
            # The client enforces its own deadline; cancellation is best effort.
            return tool(timeout=10)
        finally:
            finished.set()
            watcher.join(timeout=1)

    def lookup(state):
        run_id = state["run_id"]
        checks = list(state.get("tool_checks", {}).items())
        index = state.get("lookup_index", 0)
        if index >= len(checks):
            return {}
        criterion_id, tool_name = checks[index]
        failures = list(state.get("tool_failures", []))
        outcome = None
        # Reservations survive a crash even before the graph checkpoints.
        for attempt in range(2):
            operation = f"{run_id}:lookup:{criterion_id}:{attempt}"
            with store.connection() as conn:
                store.guard(conn, run_id)
                used = conn.execute(
                    "SELECT count(*) AS n FROM screening.calls WHERE run_id=%s",
                    (run_id,),
                ).fetchone()["n"]
                reserved = conn.execute(
                    "SELECT outcome FROM screening.calls WHERE operation_id=%s",
                    (operation,),
                ).fetchone()
                if reserved is None:
                    if used >= 4:
                        outcome = {
                            "status": "error",
                            "error": "Persisted call budget exhausted",
                        }
                        break
                    conn.execute(
                        "INSERT INTO screening.calls(operation_id,run_id) "
                        "VALUES (%s,%s)",
                        (operation, run_id),
                    )
            if reserved is not None:
                outcome = reserved["outcome"] or {
                    "status": "error",
                    "transient": True,
                    "error": "Interrupted attempt; reserved budget retained",
                }
            else:
                try:
                    if tool_name not in tools:
                        raise ValueError("No authorized worker tool registered")
                    if telemetry:
                        with telemetry.span("evidence-backend", criterion_id):
                            value = call_tool(tools[tool_name], run_id)
                    else:
                        value = call_tool(tools[tool_name], run_id)
                    from clinical_trial_patient_matching.patient_tools import ToolResult

                    value = ToolResult.model_validate(value)
                    # Empty successful lookup is not an infrastructure failure.
                    outcome = {
                        "status": (
                            "truncated"
                            if value.status == "ok"
                            and value.coverage.get("complete") is not True
                            else value.status
                        ),
                        "transient": False,
                        "error": value.error,
                    }
                except (TimeoutError, ConnectionError) as error:
                    outcome = {
                        "status": "error",
                        "transient": True,
                        "error": type(error).__name__,
                    }
                except Exception as error:
                    outcome = {
                        "status": "error",
                        "transient": False,
                        "error": type(error).__name__,
                    }
                with store.connection() as conn:
                    conn.execute(
                        "UPDATE screening.calls SET outcome=%s WHERE operation_id=%s",
                        (Jsonb(outcome), operation),
                    )
            store.check_cancel(run_id)
            if outcome["status"] != "error" or not outcome.get("transient"):
                break
            if attempt == 0:
                time.sleep(0.1)
        if outcome["status"] in {"error", "truncated"}:
            failures.append(criterion_id)
        return {"lookup_index": index + 1, "tool_failures": failures}

    def evaluate(state):
        profile, trial = (
            Profile.model_validate(state["profile"]),
            Trial.model_validate(state["trial"]),
        )
        cutoff = (
            datetime.fromisoformat(state["assessment_time"])
            if state["assessment_time"]
            else None
        )
        results = []
        # Reassess all criteria conservatively, including every dependent group.
        for criterion in trial.criteria:
            store.check_cancel(state["run_id"])
            if telemetry:
                with telemetry.span("assess-criterion", criterion.criterion_id):
                    result = assess(
                        profile, trial, criterion, cutoff, state["time_basis"]
                    )
            else:
                result = assess(profile, trial, criterion, cutoff, state["time_basis"])
            if criterion.criterion_id in state.get("tool_failures", []):
                data = result.model_dump()
                data.update(
                    status="unknown",
                    evidence_state="missing",
                    verified=False,
                    processing_status="failed",
                    citations=[],
                    evidence_ids=[],
                    source_references=[],
                    reason="Evidence lookup failed or was incomplete.",
                    follow_up=(
                        "Retry the failed evidence lookup after correcting its cause."
                    ),
                )
                result = CriterionResult.model_validate(data)
            validate_citations(result, profile, profile.facts)
            results.append(result)
            store.partial(
                state["run_id"], criterion.criterion_id, result.model_dump(mode="json")
            )
        overall, conflicts, findings = aggregate(trial, profile, results)
        report = ScreeningReport(
            trial_id=trial.trial_id,
            trial_version=trial.trial_version,
            trial_source_reference=trial.source_reference,
            patient_id=profile.patient_id,
            variant=profile.variant,
            profile_version=profile.profile_version,
            assessment_time=cutoff,
            time_basis=state["time_basis"],
            report_key=digest(
                [state["run_id"], state["report_version"], profile.profile_version]
            ),
            configuration={
                "engine": PROMPT_VERSION,
                "model": "deterministic",
                "report_version": state["report_version"],
            },
            results=results,
            required_criterion_ids=[c.criterion_id for c in trial.criteria],
            overall=overall,
            conflicts=conflicts,
            findings=findings,
            follow_up=[r.follow_up for r in results if r.follow_up],
        )
        return {"report": report.model_dump(mode="json")}

    def prepare_review(state):
        request_id = digest([state["run_id"], state["report_version"], "review"])
        request = {
            "review_request_id": request_id,
            "report_version": state["report_version"],
            "assessment_time": state["assessment_time"],
            "time_basis": state["time_basis"],
            "question": "Which sources establish the result? Resolve evidence, retain"
            " uncertainty, or accept this report.",
            "criteria": state["report"]["results"],
            "evidence_refs": state["profile"]["facts"],
            "actions": ["resolve_evidence", "retain_uncertainty", "accept_report"],
        }
        store.publish(
            state["run_id"], state["report_version"], state["report"], request
        )
        return {"request": request}

    def review(state):
        # No model calls, writes, or broad exception handler before interrupt.
        submission = interrupt(state["request"])
        return {"review_submission": submission}

    def apply_review(state):
        submission = ReviewSubmission.model_validate(state["review_submission"])
        request_id = state["request"]["review_request_id"]
        if (
            submission.review_request_id != request_id
            or submission.report_version != state["report_version"]
        ):
            raise ValueError("Resume payload does not target this checkpoint")
        with store.connection() as conn:
            store.guard(conn, state["run_id"])
            decision = conn.execute(
                "SELECT * FROM screening.decisions WHERE request_id=%s", (request_id,)
            ).fetchone()
            if not decision or decision["payload"] != submission.model_dump(
                mode="json"
            ):
                raise ValueError("Resume requires a validated stored decision")
            conn.execute(
                "UPDATE screening.decisions SET applied=true WHERE request_id=%s",
                (request_id,),
            )
            if submission.action == "resolve_evidence":
                profile = Profile.model_validate(state["profile"])
                rejected = set(submission.invalidated_source_refs)
                facts = [
                    f.model_copy(update={"source_status": "entered-in-error"})
                    if f.fact_id in rejected
                    else f
                    for f in profile.facts
                ]
                _, conflicts = reconcile(
                    [
                        f
                        for f in facts
                        if f.source_status not in {"entered-in-error", "cancelled"}
                    ]
                )
                # Keep original records in initial_state, old reports, and requests.
                revised = profile.model_copy(
                    update={
                        "facts": facts,
                        "conflicts": conflicts,
                        "profile_version": digest(
                            [profile.profile_version, request_id]
                        ),
                    }
                )
                store.audit(
                    conn,
                    request_id + ":correction",
                    state["run_id"],
                    "evidence_correction",
                    {
                        **submission.model_dump(mode="json"),
                        "affected_concepts": sorted(
                            {f.concept for f in facts if f.fact_id in rejected}
                        ),
                        "previous_profile_version": profile.profile_version,
                        "profile_version": revised.profile_version,
                        "assessment_time": state["assessment_time"],
                    },
                )
                return {
                    "profile": revised.model_dump(mode="json"),
                    "report_version": state["report_version"] + 1,
                    "finished": False,
                }
            conn.execute(
                "UPDATE screening.runs SET status='completed' WHERE run_id=%s",
                (state["run_id"],),
            )
            store.audit(
                conn,
                request_id + ":accepted",
                state["run_id"],
                "report_reviewed",
                {
                    "action": submission.action,
                    "report_version": state["report_version"],
                    "eligibility_unchanged": True,
                },
            )
        return {"finished": True}

    graph = StateGraph(ReviewState)
    for name, node in (
        ("lookup", lookup),
        ("assess", evaluate),
        ("prepare_review", prepare_review),
        ("review", review),
        ("apply_review", apply_review),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "assess")
    # Assess first so cancellation during a lookup retains completed checks.
    graph.add_conditional_edges(
        "assess",
        lambda s: (
            "lookup"
            if s.get("lookup_index", 0) < len(s.get("tool_checks", {}))
            else "prepare_review"
        ),
    )
    graph.add_edge("lookup", "assess")
    graph.add_edge("prepare_review", "review")
    graph.add_edge("review", "apply_review")
    graph.add_conditional_edges(
        "apply_review", lambda s: END if s["finished"] else "assess"
    )
    return graph.compile(checkpointer=checkpointer)


@contextmanager
def review_runtime(store):
    with psycopg.connect(
        store.dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as conn:
        saver = PostgresSaver(conn)
        # Migrations include concurrent index creation. Serialize setup across
        # workers and do not hold a separate open transaction while it runs.
        conn.execute("SELECT pg_advisory_lock(812009)")
        try:
            saver.setup()
        finally:
            conn.execute("SELECT pg_advisory_unlock(812009)")
        yield saver


def execute_run(store: ReviewStore, run_id, tools=None, telemetry=None):
    """One worker owns a run; all nodes and application writes are replayable."""
    with store.connection() as lock_conn:
        lock_conn.autocommit = True
        locked = lock_conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked", (run_id,)
        ).fetchone()["locked"]
        if not locked:
            return {"status": "busy"}
        try:
            run = store.get(run_id)
            if run["status"] in {"completed", "cancelled"}:
                with store.connection() as conn:
                    conn.execute(
                        "UPDATE screening.jobs j SET done=true "
                        "FROM screening.decisions d "
                        "WHERE j.job_id=d.request_id AND j.run_id=%s AND d.applied",
                        (run_id,),
                    )
                return run
            with review_runtime(store) as saver:
                graph = build_review_graph(store, saver, tools, telemetry)
                config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
                snapshot = graph.get_state(config)
                with store.connection() as conn:
                    jobs = conn.execute(
                        "SELECT * FROM screening.jobs WHERE run_id=%s AND kind='resum"
                        "e' AND NOT done ORDER BY job_id",
                        (run_id,),
                    ).fetchall()
                pending_ids = [
                    i.value["review_request_id"]
                    for task in snapshot.tasks
                    for i in task.interrupts
                ]
                matching = next((j for j in jobs if j["job_id"] in pending_ids), None)
                if snapshot.values and not snapshot.next:
                    # Completed graph checkpoint: do not replay the initial state.
                    return store.get(run_id)
                if pending_ids and not matching:
                    return store.get(run_id)
                value = (
                    Command(resume=matching["payload"])
                    if matching
                    else None
                    if snapshot.values
                    else run["initial_state"]
                )
                if telemetry:
                    with telemetry.invocation(
                        run_id, snapshot.values or run["initial_state"]
                    ) as trace_config:
                        result = graph.invoke(
                            value, config={**config, **trace_config}, durability="sync"
                        )
                        telemetry.result = result
                else:
                    result = graph.invoke(value, config=config, durability="sync")
                # Crash before this commit is safe: applied decisions are detectable.
                with store.connection() as conn:
                    conn.execute(
                        "UPDATE screening.jobs j SET done=true FROM screening.decisio"
                        "ns d WHERE j.job_id=d.request_id AND j.run_id=%s AND d.appli"
                        "ed",
                        (run_id,),
                    )
                return result
        except CancelledRun:
            return store.get(run_id)
        except Exception as error:
            with store.connection() as conn:
                conn.execute(
                    "UPDATE screening.runs SET status='failed',error=%s WHERE run"
                    "_id=%s AND NOT cancel_requested AND status!='completed'",
                    (type(error).__name__, run_id),
                )
            raise
        finally:
            lock_conn.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s,0))", (run_id,)
            )
