"""Verify saved demo artifacts and server-side masked Langfuse observations."""

import json
from pathlib import Path

from clinical_trial_patient_matching.review_cli import load_environment
from clinical_trial_patient_matching.review_observability import ReviewTelemetry
from clinical_trial_patient_matching.review_store import ReviewStore


def main():
    load_environment()
    summary = json.loads(Path("data/class8/demo/summary.json").read_text())
    store = ReviewStore()
    client = ReviewTelemetry(store).client
    verification = []
    for trace in summary["traces"]:
        observations = client.api.observations.get_many(
            trace_id=trace["trace_id"],
            fields="basic,io,metadata",
            limit=100,
        ).data
        assert observations, "No server-side observations found"
        payloads = json.dumps([o.model_dump(mode="json") for o in observations])
        assert "The patient is a 24-year-old" not in payloads
        assert "fictional-demo-coordinator" not in payloads
        assert all(o.session_id == summary["run_id"] for o in observations)
        with store.connection() as conn:
            row = conn.execute(
                "SELECT observations FROM screening.traces WHERE trace_id=%s",
                (trace["trace_id"],),
            ).fetchone()
        observed_ids = {o.id for o in observations}
        assert set(row["observations"].values()) <= observed_ids
        verification.append(
            {
                "trace_id": trace["trace_id"],
                "observations": len(observations),
                "session_matches": True,
                "payload_mask_verified": True,
            }
        )
    with store.connection() as conn:
        jobs = conn.execute(
            "SELECT * FROM screening.jobs WHERE run_id=%s AND kind='score'",
            (summary["run_id"],),
        ).fetchall()
    assert len(jobs) == 4 and all(j["done"] for j in jobs)
    for job in jobs:
        assert client.api.scores.get_many(id=job["job_id"]).data
    result = {
        "run_id": summary["run_id"],
        "verified_scores": len(jobs),
        "traces": verification,
        "review_wait_seconds": store.get(summary["run_id"])["review_wait_seconds"],
    }
    Path("data/class8/demo/verification.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
