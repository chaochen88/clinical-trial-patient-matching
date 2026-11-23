"""Public fictional review demonstration, using a fresh worker at every step."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from clinical_trial_patient_matching.review_cli import load_environment
from clinical_trial_patient_matching.review_observability import (
    ReviewTelemetry,
    enqueue_reviewed_scores,
    export_scores,
)
from clinical_trial_patient_matching.review_store import ReviewStore
from clinical_trial_patient_matching.review_workflow import (
    ReviewSubmission,
    initial_review_state,
)

ROOT = Path(__file__).resolve().parents[1]


def main():
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--langfuse", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("data/class8/demo"))
    args = parser.parse_args()
    store = ReviewStore()
    store.setup()
    profile = json.loads((ROOT / "data/class6/trec-conflict-profile.json").read_text())
    trial = json.loads((ROOT / "data/class6/NCT04450407-trial.json").read_text())
    run_id = store.create(
        initial_review_state(profile, trial, time_basis="described_presentation")
    )

    def worker():
        command = [
            sys.executable,
            "-m",
            "clinical_trial_patient_matching.review_cli",
            "worker",
            "--run-id",
            run_id,
        ]
        if args.langfuse:
            command.append("--langfuse")
        subprocess.run(command, check=True, cwd=ROOT, timeout=120)

    worker()
    initial = store.get(run_id)
    request = initial["pending_review"]
    worker()
    assert store.get(run_id)["pending_review"] == request
    refs = request["evidence_refs"]
    original = [
        f["fact_id"] for f in refs if f["concept"] == "HbA1c" and f["value"] == 11.3
    ]
    erroneous = [
        f["fact_id"] for f in refs if f["concept"] == "HbA1c" and f["value"] == 8.2
    ]
    correction = ReviewSubmission(
        review_request_id=request["review_request_id"],
        report_version=1,
        reviewer="fictional-demo-coordinator",
        action="resolve_evidence",
        rationale="Fictional exercise: original source confirmed; synthetic 8.2"
        "% addendum is erroneous.",
        supporting_source_refs=original,
        invalidated_source_refs=erroneous,
    )
    first = store.submit(run_id, correction)
    assert first == store.submit(run_id, correction)
    # No worker running at commit time; queued decision is applied by a fresh process.
    worker()
    revised = store.get(run_id)
    assert revised["report_version"] == 2 and revised["status"] == "waiting_review"
    acceptance = ReviewSubmission(
        review_request_id=revised["pending_review"]["review_request_id"],
        report_version=2,
        reviewer="fictional-demo-coordinator",
        action="accept_report",
        rationale="Accept the revised report, including remaining unknowns and "
        "failed criteria.",
        supporting_source_refs=original,
    )
    store.submit(run_id, acceptance)
    worker()
    final = store.get(run_id)
    assert final["status"] == "completed"
    summary = {
        "run_id": run_id,
        "status": final["status"],
        "report_versions": [1, 2],
        "same_review_after_restart": True,
        "duplicate_response_equal": True,
        "correction_review_id": correction.review_request_id,
        "acceptance_review_id": acceptance.review_request_id,
        "assessment_time": final["report"]["assessment_time"],
        "time_basis": final["report"]["time_basis"],
        "review_wait_seconds": final["review_wait_seconds"],
        "reviewer_kind": "fictional authored fixture; not independent clinical review",
    }
    if args.langfuse:
        criterion = next(
            c["criterion_id"]
            for c in trial["criteria"]
            if c["rule"] and c["rule"].get("concept") == "HbA1c"
        )
        for row, submission, expected in (
            (initial, correction, "unknown"),
            (revised, acceptance, "not_met"),
        ):
            enqueue_reviewed_scores(
                store,
                run_id,
                submission.review_request_id,
                row["report_version"],
                row["report"]["profile_version"],
                {criterion: expected},
                {criterion: True},
            )
        telemetry = ReviewTelemetry(store)
        summary["exported_scores"] = export_scores(store, telemetry.client)
        with store.connection() as conn:
            summary["traces"] = conn.execute(
                "SELECT trace_id,report_version,evidence_version,active_secon"
                "ds FROM screening.traces WHERE run_id=%s ORDER BY created_at",
                (run_id,),
            ).fetchall()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("summary", summary),
        ("report-v1", initial["report"]),
        ("report-v2", final["report"]),
    ):
        (args.output / f"{name}.json").write_text(
            json.dumps(payload, indent=2, default=str)
        )
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
