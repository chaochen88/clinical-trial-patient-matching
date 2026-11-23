"""PostgreSQL application records; checkpoints are stored by LangGraph separately."""

import os
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from clinical_trial_patient_matching.extraction import digest


class ReviewConflict(ValueError):
    """A submitted decision cannot be applied to the pending report."""


class CancelledRun(RuntimeError):
    """The durable cancellation flag prevents further publication."""


def database_url():
    value = os.getenv("CLASS8_DATABASE_URL")
    if not value:
        raise RuntimeError("Set CLASS8_DATABASE_URL for the dedicated review database")
    return value


class ReviewStore:
    def __init__(self, dsn=None):
        self.dsn = dsn or database_url()

    @contextmanager
    def connection(self):
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            yield conn

    def setup(self):
        with self.connection() as conn:
            conn.execute("CREATE SCHEMA IF NOT EXISTS screening")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS screening.runs (
                    run_id text PRIMARY KEY, initial_state jsonb NOT NULL,
                    status text NOT NULL DEFAULT 'running',
                    cancel_requested boolean NOT NULL DEFAULT false,
                    report_version integer NOT NULL DEFAULT 0,
                    partial_results jsonb NOT NULL DEFAULT '{}',
                    error text, created_at timestamptz NOT NULL DEFAULT now());
                CREATE TABLE IF NOT EXISTS screening.reports (
                    run_id text REFERENCES screening.runs, version integer,
                    payload jsonb NOT NULL, PRIMARY KEY(run_id, version));
                CREATE TABLE IF NOT EXISTS screening.requests (
                    request_id text PRIMARY KEY, run_id text REFERENCES screening.runs,
                    version integer NOT NULL, payload jsonb NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    UNIQUE(run_id, version));
                CREATE TABLE IF NOT EXISTS screening.decisions (
                    request_id text PRIMARY KEY REFERENCES screening.requests,
                    payload jsonb NOT NULL, fingerprint text NOT NULL,
                    response jsonb NOT NULL, applied boolean NOT NULL DEFAULT false,
                    created_at timestamptz NOT NULL DEFAULT now());
                CREATE TABLE IF NOT EXISTS screening.jobs (
                    job_id text PRIMARY KEY, run_id text REFERENCES screening.runs,
                    kind text NOT NULL, payload jsonb NOT NULL,
                    done boolean NOT NULL DEFAULT false);
                CREATE TABLE IF NOT EXISTS screening.audit (
                    operation_id text PRIMARY KEY,
                    run_id text REFERENCES screening.runs,
                    kind text NOT NULL, payload jsonb NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now());
                CREATE TABLE IF NOT EXISTS screening.calls (
                    operation_id text PRIMARY KEY,
                    run_id text REFERENCES screening.runs,
                    outcome jsonb, created_at timestamptz NOT NULL DEFAULT now());
                CREATE TABLE IF NOT EXISTS screening.traces (
                    invocation_id text PRIMARY KEY,
                    run_id text REFERENCES screening.runs,
                    trace_id text, report_version integer NOT NULL,
                    evidence_version text NOT NULL, prompt_version text NOT NULL,
                    model_version text NOT NULL, observations jsonb NOT NULL,
                    active_seconds double precision NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now());
            """)

    def create(self, state, run_id=None):
        run_id = run_id or str(uuid4())
        state = {**state, "run_id": run_id, "report_version": 1}
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO screening.runs(run_id, initial_state) VALUES (%s,%s)",
                (run_id, Jsonb(state)),
            )
        return run_id

    def get(self, run_id):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM screening.runs WHERE run_id=%s", (run_id,)
            ).fetchone()
            if not row:
                raise KeyError(run_id)
            report = conn.execute(
                "SELECT payload FROM screening.reports WHERE run_id=%s AND version=%s",
                (run_id, row["report_version"]),
            ).fetchone()
            pending = conn.execute(
                "SELECT payload FROM screening.requests r WHERE run_id=%s "
                "AND NOT EXISTS (SELECT 1 FROM screening.decisions d "
                "WHERE d.request_id=r.request_id) ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            row["report"] = report["payload"] if report else None
            row["review_wait_seconds"] = conn.execute(
                "SELECT COALESCE(sum(EXTRACT(EPOCH FROM "
                "(COALESCE(d.created_at,now())-r.created_at))),0)::float AS seconds "
                "FROM screening.requests r LEFT JOIN screening.decisions d "
                "USING(request_id) WHERE r.run_id=%s",
                (run_id,),
            ).fetchone()["seconds"]
            row["pending_review"] = (
                pending["payload"]
                if pending and row["status"] == "waiting_review"
                else None
            )
            return row

    def guard(self, conn, run_id):
        row = conn.execute(
            "SELECT * FROM screening.runs WHERE run_id=%s FOR UPDATE", (run_id,)
        ).fetchone()
        if not row:
            raise KeyError(run_id)
        if row["cancel_requested"] or row["status"] == "cancelled":
            raise CancelledRun(run_id)
        return row

    def check_cancel(self, run_id):
        if self.get(run_id)["cancel_requested"]:
            raise CancelledRun(run_id)

    def partial(self, run_id, criterion_id, result):
        with self.connection() as conn:
            self.guard(conn, run_id)
            conn.execute(
                "UPDATE screening.runs SET partial_results=partial_results || %s "
                "WHERE run_id=%s",
                (Jsonb({criterion_id: result}), run_id),
            )

    def publish(self, run_id, version, report, request):
        with self.connection() as conn:
            row = self.guard(conn, run_id)
            existing = conn.execute(
                "SELECT payload FROM screening.reports WHERE run_id=%s AND version=%s",
                (run_id, version),
            ).fetchone()
            if existing and existing["payload"] != report:
                raise ReviewConflict("A replay changed an existing report")
            conn.execute(
                "INSERT INTO screening.reports VALUES (%s,%s,%s) ON CONFLICT "
                "DO NOTHING",
                (run_id, version, Jsonb(report)),
            )
            conn.execute(
                "INSERT INTO screening.requests(request_id,run_id,version,payload) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (request["review_request_id"], run_id, version, Jsonb(request)),
            )
            if row["status"] != "completed":
                conn.execute(
                    "UPDATE screening.runs SET status='waiting_review', "
                    "report_version=%s WHERE run_id=%s",
                    (version, run_id),
                )

    def submit(self, run_id, submission):
        payload = submission.model_dump(mode="json")
        request_id = submission.review_request_id
        fingerprint = digest(payload)
        with self.connection() as conn:
            row = self.guard(conn, run_id)
            saved = conn.execute(
                "SELECT * FROM screening.decisions WHERE request_id=%s", (request_id,)
            ).fetchone()
            if saved:
                if saved["fingerprint"] != fingerprint:
                    raise ReviewConflict("Different payload for an accepted request")
                # Fingerprint includes the request ID; also verify run ownership.
                owner = conn.execute(
                    "SELECT run_id FROM screening.requests WHERE request_id=%s",
                    (request_id,),
                ).fetchone()
                if owner["run_id"] != run_id:
                    raise ReviewConflict("Review belongs to another run")
                return saved["response"]
            request = conn.execute(
                "SELECT * FROM screening.requests WHERE request_id=%s AND run_id=%s",
                (request_id, run_id),
            ).fetchone()
            if (
                not request
                or row["status"] != "waiting_review"
                or submission.report_version != row["report_version"]
                or request["version"] != submission.report_version
            ):
                raise ReviewConflict("Review request or report version is stale")
            allowed = {f["fact_id"] for f in request["payload"]["evidence_refs"]}
            selected = set(submission.supporting_source_refs)
            rejected = set(submission.invalidated_source_refs)
            if not (selected | rejected) <= allowed:
                raise ReviewConflict("Unknown supporting or invalidated evidence ID")
            if selected & rejected:
                raise ReviewConflict("A fact cannot be supported and invalidated")
            if submission.action == "resolve_evidence":
                if not selected or not rejected:
                    raise ReviewConflict(
                        "Resolution requires supported and rejected facts"
                    )
                facts = {f["fact_id"]: f for f in request["payload"]["evidence_refs"]}
                if any(
                    facts[f].get("source_status") in {"entered-in-error", "cancelled"}
                    for f in selected
                ):
                    raise ReviewConflict(
                        "Invalidated evidence cannot support a new resolution"
                    )
                if all(
                    facts[f].get("source_status") in {"entered-in-error", "cancelled"}
                    for f in rejected
                ):
                    raise ReviewConflict(
                        "Resolution must invalidate previously usable evidence"
                    )
                concepts = {facts[f]["concept"] for f in selected}
                if any(facts[f]["concept"] not in concepts for f in rejected):
                    raise ReviewConflict("Resolution must reference the same concept")
            elif rejected:
                raise ReviewConflict("Only evidence resolution can invalidate facts")
            response = {
                "run_id": run_id,
                "review_request_id": request_id,
                "report_version": submission.report_version,
                "decision_saved": True,
            }
            conn.execute(
                "INSERT INTO screening.decisions(request_id,payload,fingerpri"
                "nt,response) "
                "VALUES (%s,%s,%s,%s)",
                (request_id, Jsonb(payload), fingerprint, Jsonb(response)),
            )
            conn.execute(
                "INSERT INTO screening.jobs(job_id,run_id,kind,payload) "
                "VALUES (%s,%s,'resume',%s)",
                (request_id, run_id, Jsonb(payload)),
            )
            self.audit(conn, request_id, run_id, "review_decision", payload)
            return response

    @staticmethod
    def audit(conn, operation_id, run_id, kind, payload):
        conn.execute(
            "INSERT INTO screening.audit(operation_id,run_id,kind,payload) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (operation_id, run_id, kind, Jsonb(payload)),
        )

    def cancel(self, run_id):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT status FROM screening.runs WHERE run_id=%s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if not row:
                raise KeyError(run_id)
            if row["status"] == "completed":
                raise ReviewConflict("Completed runs cannot be cancelled")
            conn.execute(
                "UPDATE screening.runs SET cancel_requested=true,status='cancelled' "
                "WHERE run_id=%s",
                (run_id,),
            )
            self.audit(conn, run_id + ":cancel", run_id, "cancel_requested", {})

    def queue_scores(self, run_id, review_id, scores):
        with self.connection() as conn:
            for score in scores:
                score_id = digest(
                    [
                        review_id,
                        score["trace_id"],
                        score.get("observation_id"),
                        score["name"],
                    ]
                )[:32]
                payload = {
                    **score,
                    "score_id": score_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                existing = conn.execute(
                    "SELECT payload FROM screening.jobs WHERE job_id=%s",
                    (score_id,),
                ).fetchone()
                if existing:
                    previous = {
                        k: v for k, v in existing["payload"].items() if k != "timestamp"
                    }
                    incoming = {k: v for k, v in payload.items() if k != "timestamp"}
                    if previous != incoming:
                        raise ReviewConflict("A queued score cannot change on retry")
                conn.execute(
                    "INSERT INTO screening.jobs(job_id,run_id,kind,payload) "
                    "VALUES (%s,%s,'score',%s) ON CONFLICT DO NOTHING",
                    (score_id, run_id, Jsonb(payload)),
                )
