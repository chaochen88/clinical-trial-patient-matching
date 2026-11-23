"""PostgreSQL recovery, concurrency, review validation, cancellation and replay."""

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clinical_trial_patient_matching.assessment import Criterion, Trial
from clinical_trial_patient_matching.criteria import Rule
from clinical_trial_patient_matching.patients import (
    Claim,
    Record,
    build_profile,
    linked_fact,
)
from clinical_trial_patient_matching.review_api import create_app
from clinical_trial_patient_matching.review_cli import load_environment
from clinical_trial_patient_matching.review_evaluation import (
    reference_cases,
    reference_review,
)
from clinical_trial_patient_matching.review_observability import redact_export
from clinical_trial_patient_matching.review_store import ReviewConflict, ReviewStore
from clinical_trial_patient_matching.review_workflow import (
    ReviewSubmission,
    execute_run,
    initial_review_state,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(monkeypatch):
    load_environment()
    test_dsn = os.getenv("CLASS8_TEST_DATABASE_URL")
    if not test_dsn:
        pytest.skip(
            "Set CLASS8_TEST_DATABASE_URL for isolated PostgreSQL recovery tests"
        )
    if test_dsn == os.getenv("CLASS8_DATABASE_URL"):
        pytest.fail("Recovery tests must use a separate database")
    monkeypatch.setenv("CLASS8_DATABASE_URL", test_dsn)
    value = ReviewStore(test_dsn)
    value.setup()
    return value


def case_inputs():
    facts = []
    for concept, value, unit in (
        ("HbA1c", 11.3, "%"),
        ("HbA1c", 8.2, "%"),
        ("BMI", 29.38, "kg/m2"),
    ):
        text = f"Fictional encounter: {concept} {value} {unit}"
        record = Record(
            patient_id="class8-synthetic",
            variant="fictional-conflict",
            record_id=text,
            dataset="synthetic",
            text=text,
        )
        claim = Claim(
            concept=concept,
            value=value,
            unit=unit,
            assertion="affirmed",
            experiencer="patient",
            time_context="current presentation",
            quote=text,
        )
        facts.append(linked_fact(record, claim, {"text": text}))
    profile = build_profile([], None, {"provider": "synthetic"}, facts)
    criteria = []
    for concept, lower, upper, unit in (
        ("HbA1c", 5.6, 9.5, "%"),
        ("BMI", 27, 36, "kg/m2"),
    ):
        quote = f"{concept} {lower}-{upper} {unit}, inclusive"
        rule = Rule(
            kind="range",
            source_bullet_id=concept,
            criterion_type="inclusion",
            source_quote=quote,
            concept=concept,
            lower=lower,
            upper=upper,
            lower_inclusive=True,
            upper_inclusive=True,
            unit=unit,
        )
        criteria.append(
            Criterion(
                criterion_id=concept,
                criterion_type="inclusion",
                predicate=quote,
                rule=rule,
                reviewed_mapping=True,
            )
        )
    return profile, Trial(
        trial_id="synthetic",
        trial_version="frozen-v1",
        source_reference="synthetic",
        criteria=criteria,
    )


def create_case(store, **kwargs):
    profile, trial = case_inputs()
    return store.create(
        initial_review_state(
            profile, trial, time_basis="described_presentation", **kwargs
        )
    )


def decision(row, action="accept_report", **kwargs):
    request = row["pending_review"]
    return ReviewSubmission(
        review_request_id=request["review_request_id"],
        report_version=request["report_version"],
        reviewer="synthetic-test-reviewer",
        action=action,
        rationale="Synthetic recovery test decision",
        **kwargs,
    )


def worker(run_id):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "clinical_trial_patient_matching.review_cli",
            "worker",
            "--run-id",
            run_id,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_fresh_process_review_restart_and_duplicate(store):
    run_id = create_case(store)
    worker(run_id)
    before = store.get(run_id)
    worker(run_id)
    assert store.get(run_id)["pending_review"] == before["pending_review"]
    submission = decision(before)
    first = store.submit(run_id, submission)
    assert store.submit(run_id, submission) == first
    # Decision commits with no worker alive. A fresh process finds durable job.
    worker(run_id)
    after = store.get(run_id)
    assert after["status"] == "completed"
    assert after["report"]["overall"] == before["report"]["overall"]
    with store.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM screening.audit WHERE run_id=%s AN"
                "D kind='review_decision'",
                (run_id,),
            ).fetchone()["n"]
            == 1
        )
    worker(run_id)
    assert store.get(run_id)["report_version"] == 1


def test_correction_reassesses_and_requires_new_review(store):
    run_id = create_case(store)
    execute_run(store, run_id)
    before = store.get(run_id)
    refs = before["pending_review"]["evidence_refs"]
    original = [
        f["fact_id"] for f in refs if f["concept"] == "HbA1c" and f["value"] == 11.3
    ]
    erroneous = [
        f["fact_id"] for f in refs if f["concept"] == "HbA1c" and f["value"] == 8.2
    ]
    assert original and erroneous
    submission = decision(
        before,
        "resolve_evidence",
        supporting_source_refs=original,
        invalidated_source_refs=erroneous,
    )
    store.submit(run_id, submission)
    worker(run_id)
    after = store.get(run_id)
    assert after["status"] == "waiting_review"
    assert after["report_version"] == 2
    assert after["report"]["assessment_time"] == before["report"]["assessment_time"]
    hba1c = [
        r
        for r in after["report"]["results"]
        if r["rule"] and r["rule"].get("concept") == "HbA1c"
    ]
    assert hba1c and hba1c[0]["status"] == "not_met"
    assert store.submit(run_id, submission)[
        "decision_saved"
    ]  # Identical historical retry.
    stale = decision(before).model_copy(
        update={"review_request_id": after["pending_review"]["review_request_id"]}
    )
    with pytest.raises(ReviewConflict):
        store.submit(run_id, stale)
    store.submit(run_id, decision(after))
    worker(run_id)
    assert store.get(run_id)["status"] == "completed"
    with store.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM screening.reports WHERE run_id=%s", (run_id,)
            ).fetchone()["n"]
            == 2
        )


def test_concurrent_duplicate_and_conflicting_payload(store):
    run_id = create_case(store)
    execute_run(store, run_id)
    submission = decision(store.get(run_id))
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda _: store.submit(run_id, submission), range(2)))
    assert a == b
    with pytest.raises(ReviewConflict):
        store.submit(run_id, submission.model_copy(update={"rationale": "different"}))


def test_api_validation_and_unknown_sources(store):
    run_id = create_case(store)
    execute_run(store, run_id)
    submission = decision(store.get(run_id))
    client = TestClient(create_app(store))
    assert client.post(f"/runs/{run_id}/review", json={}).status_code == 422
    wrong = submission.model_copy(
        update={"supporting_source_refs": ["foreign-patient-fact"]}
    )
    assert (
        client.post(f"/runs/{run_id}/review", json=wrong.model_dump()).status_code
        == 409
    )
    assert (
        client.post(f"/runs/{run_id}/review", json=submission.model_dump()).status_code
        == 202
    )


def test_transient_lookup_retries_and_partial_failure(store):
    _, trial = case_inputs()
    criterion_id = trial.criteria[0].criterion_id
    run_id = create_case(store, tool_checks={criterion_id: "medication_history"})
    deadlines = []

    def timeout(*, timeout):
        deadlines.append(timeout)
        raise TimeoutError("synthetic client deadline")

    execute_run(store, run_id, tools={"medication_history": timeout})
    row = store.get(run_id)
    assert deadlines == [10, 10]
    failed = next(
        r for r in row["report"]["results"] if r["criterion_id"] == criterion_id
    )
    assert failed["status"] == "unknown" and failed["processing_status"] == "failed"
    assert any(r["processing_status"] == "complete" for r in row["report"]["results"])
    with store.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM screening.calls WHERE run_id=%s", (run_id,)
            ).fetchone()["n"]
            == 2
        )


def test_validation_failure_does_not_retry(store):
    _, trial = case_inputs()
    run_id = create_case(
        store, tool_checks={trial.criteria[0].criterion_id: "unauthorized"}
    )
    execute_run(store, run_id)
    with store.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM screening.calls WHERE run_id=%s", (run_id,)
            ).fetchone()["n"]
            == 1
        )


def test_cancel_in_flight_preserves_partial_no_report(store):
    _, trial = case_inputs()
    run_id = create_case(store, tool_checks={trial.criteria[0].criterion_id: "slow"})
    started, release, cancelled = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def slow(*, timeout):
        started.set()
        release.wait(timeout)
        return {
            "status": "not_found",
            "rows": [],
            "source_version": "synthetic",
            "coverage": {"complete": True},
        }

    def cancel():
        cancelled.set()
        release.set()

    slow.cancel = cancel

    with ThreadPoolExecutor() as pool:
        future = pool.submit(execute_run, store, run_id, {"slow": slow})
        assert started.wait(30)
        assert execute_run(store, run_id)["status"] == "busy"
        store.cancel(run_id)
        assert cancelled.wait(5)
        future.result(timeout=30)
    row = store.get(run_id)
    assert row["status"] == "cancelled" and row["partial_results"]
    assert row["report"] is None
    worker(run_id)
    assert store.get(run_id)["status"] == "cancelled"


def test_empty_success_is_not_processing_failure(store):
    run_id = create_case(store, tool_checks={"HbA1c": "empty"})

    def empty(*, timeout):
        return {
            "status": "not_found",
            "rows": [],
            "source_version": "synthetic",
            "coverage": {"complete": True},
        }

    execute_run(store, run_id, tools={"empty": empty})
    result = next(
        r
        for r in store.get(run_id)["report"]["results"]
        if r["criterion_id"] == "HbA1c"
    )
    assert result["status"] == "unknown" and result["processing_status"] == "complete"


def test_interrupted_attempt_retains_budget_without_repeating_read(store):
    run_id = create_case(store, tool_checks={"HbA1c": "empty"})
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO screening.calls(operation_id,run_id) VALUES (%s,%s)",
            (f"{run_id}:lookup:HbA1c:0", run_id),
        )
    reads = []

    def empty(*, timeout):
        reads.append(timeout)
        return {
            "status": "not_found",
            "rows": [],
            "source_version": "synthetic",
            "coverage": {"complete": True},
        }

    execute_run(store, run_id, tools={"empty": empty})
    assert reads == [10]
    with store.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM screening.calls WHERE run_id=%s", (run_id,)
            ).fetchone()["n"]
            == 2
        )


def test_score_queue_retries_preserve_id_and_timestamp(store):
    run_id = create_case(store)
    review_id = f"{run_id}:fixture-review"
    score = {
        "trace_id": "synthetic-trace",
        "observation_id": "synthetic-span",
        "name": "criterion_correct",
        "value": 1,
    }
    store.queue_scores(run_id, review_id, [score])
    with store.connection() as conn:
        first = conn.execute(
            "SELECT payload FROM screening.jobs WHERE run_id=%s", (run_id,)
        ).fetchone()["payload"]
    store.queue_scores(run_id, review_id, [score])
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM screening.jobs WHERE run_id=%s", (run_id,)
        ).fetchall()
    assert len(rows) == 1 and rows[0]["payload"] == first
    with pytest.raises(ReviewConflict):
        store.queue_scores(run_id, review_id, [{**score, "value": 0}])


def test_export_mask_removes_interrupt_payload_but_keeps_identity():
    attributes = {
        "langfuse.observation.status_message": "GraphInterrupt(full patient payload)",
        "langfuse.observation.input": "patient narrative",
        "langfuse.observation.output": "reviewer decision",
        "langfuse.observation.metadata.criterion_id": "HbA1c",
        "langfuse.session.id": "run-id",
    }
    params = SimpleNamespace(spans={"span": SimpleNamespace(attributes=attributes)})
    patch = redact_export(params=params).span_patches["span"]
    assert set(patch.delete_attributes) == {
        "langfuse.observation.status_message",
        "langfuse.observation.input",
        "langfuse.observation.output",
    }


def test_reference_approval_is_bound_to_exact_inputs_and_labels():
    cases = reference_cases()
    assert reference_review(cases)["reference_status"] == "project_owner_reviewed"
    cases[0]["expected_output"]["status"] = "unknown"
    with pytest.raises(ValueError, match="changed"):
        reference_review(cases)


def test_crash_after_report_commit_replays_idempotently(store, monkeypatch):
    run_id = create_case(store)
    publish = store.publish

    def crash(*args):
        publish(*args)
        raise RuntimeError("synthetic crash after application commit")

    monkeypatch.setattr(store, "publish", crash)
    with pytest.raises(RuntimeError):
        execute_run(store, run_id)
    assert store.get(run_id)["status"] == "failed"
    worker(run_id)
    assert store.get(run_id)["status"] == "waiting_review"
    with store.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM screening.requests WHERE run_id=%s",
                (run_id,),
            ).fetchone()["n"]
            == 1
        )
