"""Narrow, scoped read-only evidence tools shared by direct and MCP callers."""

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from clinical_trial_patient_matching.patients import Fact, StrictModel


class TrialArgs(StrictModel):
    trial_id: str = Field(pattern=r"^NCT[0-9]{8}$")


class SearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=80)
    limit: int = Field(default=10, ge=1, le=20)


class RecordArgs(StrictModel):
    patient_id: str = Field(min_length=1)
    record_id: str = Field(min_length=1)


class ObservationArgs(StrictModel):
    patient_id: str = Field(min_length=1)
    loinc_code: str = Field(pattern=r"^[0-9]{1,5}-[0-9]$")
    as_of: AwareDatetime
    limit: int = Field(default=20, ge=1, le=20)


class ToolResult(StrictModel):
    status: Literal["ok", "not_found", "truncated", "error"]
    rows: list[dict] = Field(default_factory=list)
    source_version: str
    coverage: dict
    error: str | None = None


TOOLS = {
    "get_trial": (TrialArgs, "Read an authorized frozen trial and its criteria."),
    "search_patients": (
        SearchArgs,
        "Search concepts within the authorized patient scope.",
    ),
    "get_patient_record": (
        RecordArgs,
        "Read a scoped source record available by cutoff.",
    ),
    "get_patient_observations": (
        ObservationArgs,
        "Read dated LOINC observations for an authorized patient at the fixed cutoff.",
    ),
}


def tool_schemas():
    return [
        {
            "name": name,
            "description": description,
            "input_schema": schema.model_json_schema(),
        }
        for name, (schema, description) in TOOLS.items()
    ]


class EvidenceBackend:
    def __init__(self, config: Path):
        self.config = json.loads(config.read_text())
        self.database = Path(self.config["database"]).resolve()
        self.source_version = hashlib.sha256(self.database.read_bytes()).hexdigest()
        if self.source_version != self.config["source_version"]:
            raise ValueError("Evidence database changed")
        self.cutoff = datetime.fromisoformat(self.config["as_of"])
        if self.cutoff.utcoffset() is None:
            raise ValueError("Fixed cutoff must be timezone-aware")

    def validate(self, name, arguments):
        if (
            hashlib.sha256(self.database.read_bytes()).hexdigest()
            != self.source_version
        ):
            raise ValueError("Evidence database changed after authorization")
        if name not in TOOLS:
            raise ValueError("Unknown tool")
        args = TOOLS[name][0].model_validate(arguments)
        if (
            hasattr(args, "patient_id")
            and args.patient_id not in self.config["patients"]
        ):
            raise ValueError("Patient outside authorized scope")
        if hasattr(args, "trial_id") and args.trial_id not in self.config["trials"]:
            raise ValueError("Trial outside authorized scope")
        if hasattr(args, "as_of") and args.as_of != self.cutoff:
            raise ValueError("Assessment cutoff is fixed")
        if hasattr(args, "as_of"):
            args = args.model_copy(update={"as_of": self.cutoff})
        return args

    def call(self, name, arguments):
        try:
            args = self.validate(name, arguments)
            with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True) as db:
                db.execute("PRAGMA query_only=ON")
                scope = self.config["patients"]
                placeholders = ",".join("?" for _ in scope)
                if name == "get_trial":
                    rows = db.execute(
                        "SELECT body FROM trials WHERE id=?", (args.trial_id,)
                    ).fetchall()
                    limit = 1
                else:
                    cutoff = self.cutoff.timestamp()
                    sql = (
                        "SELECT body FROM facts WHERE patient_id IN ("
                        + placeholders
                        + ") AND clinical_time<=? AND available_time<=?"
                    )
                    params = [*scope, cutoff, cutoff]
                    limit = getattr(args, "limit", 20)
                    if name == "get_patient_observations":
                        sql += " AND patient_id=? AND system=? AND code=?"
                        params += [args.patient_id, "http://loinc.org", args.loinc_code]
                    elif name == "get_patient_record":
                        sql += " AND patient_id=? AND record_id=?"
                        params += [args.patient_id, args.record_id]
                    else:
                        sql += " AND instr(lower(concept), lower(?))>0"
                        params += [args.query]
                    sql += " ORDER BY clinical_time DESC, fact_id LIMIT ?"
                    rows = db.execute(sql, (*params, limit + 1)).fetchall()
                status = (
                    "truncated" if len(rows) > limit else "ok" if rows else "not_found"
                )
                return ToolResult(
                    status=status,
                    rows=[json.loads(row[0]) for row in rows[:limit]],
                    source_version=self.source_version,
                    coverage={
                        "as_of": self.cutoff.isoformat(),
                        "returned": min(len(rows), limit),
                        "complete": status != "truncated",
                        "scope": "frozen local snapshot; not a complete EHR",
                    },
                )
        except Exception as error:
            return ToolResult(
                status="error",
                source_version=self.source_version,
                coverage={"complete": False},
                error=str(error),
            )


def create_snapshot(path, profiles, trials, cutoff):
    """Explicit preparation step; runtime tools never write to the database."""
    if path.exists():
        raise ValueError("Refusing to overwrite an existing snapshot")
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE facts (fact_id TEXT PRIMARY KEY, patient_id TEXT, "
            "record_id TEXT, system TEXT, code TEXT, concept TEXT, "
            "clinical_time REAL, available_time REAL, body TEXT)"
        )
        db.execute("CREATE TABLE trials (id TEXT PRIMARY KEY, body TEXT)")
        for profile in profiles:
            for fact in profile.facts:
                Fact.model_validate(fact)
                db.execute(
                    "INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        fact.fact_id,
                        fact.patient_id,
                        fact.record_id,
                        fact.concept_system,
                        fact.concept_code,
                        fact.concept,
                        fact.clinical_time.timestamp() if fact.clinical_time else None,
                        fact.available_time.timestamp()
                        if fact.available_time
                        else None,
                        fact.model_dump_json(),
                    ),
                )
        for trial in trials:
            db.execute(
                "INSERT INTO trials VALUES (?,?)",
                (trial.trial_id, trial.model_dump_json()),
            )
    config = path.with_suffix(".json")
    config.write_text(
        json.dumps(
            {
                "database": str(path.resolve()),
                "source_version": hashlib.sha256(path.read_bytes()).hexdigest(),
                "patients": [p.patient_id for p in profiles],
                "trials": [t.trial_id for t in trials],
                "as_of": cutoff.isoformat(),
            },
            indent=2,
        )
    )
    return config
