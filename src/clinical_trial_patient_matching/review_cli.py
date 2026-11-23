"""Local Class 8 commands; a new worker process can resume any saved run."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from clinical_trial_patient_matching.assessment import Trial
from clinical_trial_patient_matching.patients import Profile
from clinical_trial_patient_matching.review_store import ReviewStore
from clinical_trial_patient_matching.review_workflow import (
    ReviewSubmission,
    execute_run,
    initial_review_state,
    review_runtime,
)


def load_environment():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def main():
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    create = commands.add_parser("create")
    create.add_argument("--profile", type=Path, required=True)
    create.add_argument("--trial", type=Path, required=True)
    create.add_argument("--assessment-time", type=datetime.fromisoformat)
    create.add_argument("--time-basis", default="described_presentation")
    worker = commands.add_parser("worker")
    worker.add_argument("--run-id")
    worker.add_argument("--langfuse", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("run_id")
    submit = commands.add_parser("submit")
    submit.add_argument("run_id")
    submit.add_argument("--file", type=Path, required=True)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("run_id")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8008)
    commands.add_parser("export-scores")
    args = parser.parse_args()
    store = ReviewStore()
    if args.command == "init":
        store.setup()
        with review_runtime(store):
            pass
        print("Class 8 application tables and PostgreSQL checkpoints initialized.")
    elif args.command == "create":
        profile = Profile.model_validate_json(args.profile.read_text(encoding="utf-8"))
        trial = Trial.model_validate_json(args.trial.read_text(encoding="utf-8"))
        print(
            store.create(
                initial_review_state(
                    profile, trial, args.assessment_time, args.time_basis
                )
            )
        )
    elif args.command == "worker":
        from clinical_trial_patient_matching.review_observability import ReviewTelemetry

        telemetry = ReviewTelemetry(store) if args.langfuse else None
        if args.run_id:
            runs = [args.run_id]
        else:
            with store.connection() as conn:
                runs = [
                    r["run_id"]
                    for r in conn.execute(
                        "SELECT run_id FROM screening.runs WHERE status IN ('running'"
                        ",'waiting_review','failed') ORDER BY created_at"
                    ).fetchall()
                ]
        for run_id in runs:
            execute_run(store, run_id, telemetry=telemetry)
            row = store.get(run_id)
            print(
                json.dumps(
                    {
                        "run_id": run_id,
                        "status": row["status"],
                        "report_version": row["report_version"],
                    }
                )
            )
    elif args.command == "status":
        row = store.get(args.run_id)
        row.pop("initial_state")
        print(json.dumps(row, indent=2, default=str))
    elif args.command == "submit":
        submission = ReviewSubmission.model_validate_json(
            args.file.read_text(encoding="utf-8")
        )
        print(json.dumps(store.submit(args.run_id, submission)))
    elif args.command == "cancel":
        store.cancel(args.run_id)
        print("Cancelled; partial work retained.")
    elif args.command == "serve":
        import uvicorn

        from clinical_trial_patient_matching.review_api import create_app

        uvicorn.run(create_app(store), host="127.0.0.1", port=args.port)
    elif args.command == "export-scores":
        from clinical_trial_patient_matching.review_observability import (
            ReviewTelemetry,
            export_scores,
        )

        telemetry = ReviewTelemetry(store)
        print(f"Exported {export_scores(store, telemetry.client)} scores")


if __name__ == "__main__":
    main()
