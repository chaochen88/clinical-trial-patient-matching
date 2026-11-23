"""Local coordinator API. Bind to localhost; no production auth is implied."""

from fastapi import FastAPI, HTTPException
from pydantic import Field

from clinical_trial_patient_matching.patients import StrictModel
from clinical_trial_patient_matching.review_observability import enqueue_reviewed_scores
from clinical_trial_patient_matching.review_store import (
    CancelledRun,
    ReviewConflict,
    ReviewStore,
)
from clinical_trial_patient_matching.review_workflow import ReviewSubmission


class ScoreSubmission(StrictModel):
    review_request_id: str
    report_version: int = Field(ge=1)
    profile_version: str
    labels: dict[str, str]
    evidence_supported: dict[str, bool]


def create_app(store=None):
    store = store or ReviewStore()
    app = FastAPI(title="Class 8 coordinator review", version="0.1")

    @app.get("/runs")
    def list_runs():
        with store.connection() as conn:
            return conn.execute(
                "SELECT run_id,status,report_version,created_at FROM screenin"
                "g.runs ORDER BY created_at DESC"
            ).fetchall()

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        try:
            result = store.get(run_id)
            result.pop("initial_state")
            return result
        except KeyError as error:
            raise HTTPException(404, "Run not found") from error

    @app.post("/runs/{run_id}/review", status_code=202)
    def submit_review(run_id: str, submission: ReviewSubmission):
        try:
            return store.submit(run_id, submission)
        except KeyError as error:
            raise HTTPException(404, "Run not found") from error
        except (ReviewConflict, CancelledRun) as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        try:
            store.cancel(run_id)
            return {"run_id": run_id, "status": "cancelled"}
        except KeyError as error:
            raise HTTPException(404, "Run not found") from error
        except ReviewConflict as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/runs/{run_id}/scores", status_code=202)
    def score_report(run_id: str, submission: ScoreSubmission):
        try:
            count = enqueue_reviewed_scores(
                store,
                run_id,
                submission.review_request_id,
                submission.report_version,
                submission.profile_version,
                submission.labels,
                submission.evidence_supported,
            )
            return {"queued_scores": count}
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    return app
