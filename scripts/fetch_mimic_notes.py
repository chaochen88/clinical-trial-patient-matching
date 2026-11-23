"""Export linked discharge notes for an existing bounded MIMIC cohort."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import bigquery


def note_sql(dataset, hosp):
    for name in (dataset, hosp):
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_]+", name):
            raise ValueError("Invalid project.dataset identifier")
    return f"""
    SELECT n.note_id, n.subject_id, n.hadm_id, n.note_type, n.note_seq,
           n.charttime, n.storetime, n.text,
           a.admittime, a.dischtime,
           n.hadm_id = c.hadm_id AS is_selected_admission
    FROM `{dataset}.discharge` n
    JOIN UNNEST(@cohort) c ON n.subject_id = c.subject_id
    JOIN `{hosp}.admissions` a
      ON a.subject_id = n.subject_id AND a.hadm_id = n.hadm_id
    WHERE n.charttime BETWEEN DATETIME_SUB(c.assessment_time,
          INTERVAL @lookback_days DAY) AND c.assessment_time
      AND a.admittime <= c.assessment_time
      AND a.dischtime IS NOT NULL AND a.dischtime <= c.assessment_time
    ORDER BY n.subject_id, n.charttime, n.note_id
    """


def cohort_parameters(rows):
    return bigquery.ArrayQueryParameter(
        "cohort",
        "STRUCT",
        [
            bigquery.StructQueryParameter(
                None,
                bigquery.ScalarQueryParameter("subject_id", "INT64", r["subject_id"]),
                bigquery.ScalarQueryParameter("hadm_id", "INT64", r["hadm_id"]),
                bigquery.ScalarQueryParameter(
                    "assessment_time",
                    "DATETIME",
                    datetime.fromisoformat(r["assessment_time"]),
                ),
            )
            for r in rows
        ],
    )


def classify_note(row, cohort):
    if row["subject_id"] not in cohort:
        raise ValueError("Note patient is outside cohort")
    cutoff = datetime.fromisoformat(cohort[row["subject_id"]]["assessment_time"])
    for key in ("charttime", "admittime", "dischtime", "storetime"):
        value = row.get(key)
        if value and datetime.fromisoformat(str(value)).tzinfo is not None:
            raise ValueError("Expected naive MIMIC shifted times")
    if not row.get("note_id") or not isinstance(row.get("text"), str):
        raise ValueError("Missing note identity or text")
    if not row["text"].strip() or not row.get("hadm_id"):
        raise ValueError("Empty note or missing admission link")
    for key in ("charttime", "admittime", "dischtime"):
        if not row.get(key) or datetime.fromisoformat(str(row[key])) > cutoff:
            raise ValueError("Note/admission is not within retrospective cutoff")
    if not row.get("storetime"):
        return "unknown_availability"
    return (
        "available_by_cutoff"
        if datetime.fromisoformat(str(row["storetime"])) <= cutoff
        else "stored_after_cutoff"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=Path("data/mimic/NCT03926806/initial-20")
    )
    parser.add_argument("--dataset", default="physionet-data.mimiciv_note")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--max-gib", type=float, default=10)
    parser.add_argument("--max-rows", type=int, default=2000)
    args = parser.parse_args()
    if min(args.lookback_days, args.max_gib, args.max_rows) <= 0:
        parser.error("Limits must be positive")
    parent_bytes = (args.source / "manifest.json").read_bytes()
    parent = json.loads(parent_bytes)
    if parent["status"] != "completed":
        raise ValueError("Hospital cohort export must be complete")
    raw = (args.source / "cohort.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest() != parent["exports"]["cohort"]["sha256"]:
        raise ValueError("Cohort hash mismatch")
    rows = [json.loads(line) for line in raw.splitlines()]
    cohort = {r["subject_id"]: r for r in rows}
    if not rows or len(rows) > 20 or len(cohort) != len(rows):
        raise ValueError("Expected 1–20 unique existing cohort members")
    sql = note_sql(args.dataset, parent["dataset"])
    load_dotenv()
    client = bigquery.Client(project=parent["project"])
    location = client.get_dataset(args.dataset).location
    table = client.get_table(f"{args.dataset}.discharge")
    metadata = dict(
        dataset=args.dataset, location=location, columns=[f.name for f in table.schema]
    )
    print(json.dumps(metadata), flush=True)
    if args.inspect:
        return
    if location != parent["location"]:
        raise ValueError("Note and hospital datasets have different locations")
    parameters = [
        cohort_parameters(rows),
        bigquery.ScalarQueryParameter("lookback_days", "INT64", args.lookback_days),
    ]
    dry = client.query(
        sql,
        location=location,
        job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=parameters
        ),
    )
    estimated = dry.total_bytes_processed or 0
    print(f"Discharge note query estimate: {estimated / 1024**3:.3f} GiB", flush=True)
    if estimated > args.max_gib * 1024**3:
        raise ValueError("Dry-run estimate exceeds note query byte budget")
    if not args.execute:
        return
    output = args.output or args.source.parent / "notes" / datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "dataset": args.dataset,
        "hospital_dataset": parent["dataset"],
        "project": parent["project"],
        "location": location,
        "hospital_export": str(args.source),
        "hospital_manifest_sha256": hashlib.sha256(parent_bytes).hexdigest(),
        "cohort_sha256": hashlib.sha256(raw).hexdigest(),
        "lookback_days": args.lookback_days,
        "max_bytes_billed": int(args.max_gib * 1024**3),
        "estimated_bytes": estimated,
        "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
        "columns": metadata["columns"],
        "time_basis": parent["time_basis"],
        "late_note_policy": (
            "Retain locally, never merge as evidence available at cutoff"
        ),
    }
    manifest_path = output / "manifest.json"

    def save():
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    save()
    (output / "discharge.sql").write_text(sql, encoding="utf-8")
    try:
        job = client.query(
            sql,
            location=location,
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=manifest["max_bytes_billed"],
                query_parameters=parameters,
            ),
        )
        notes, ids, categories = [], set(), {}
        per_patient = {
            pid: {
                "available_by_cutoff": 0,
                "stored_after_cutoff": 0,
                "unknown_availability": 0,
            }
            for pid in cohort
        }
        for row in job.result(timeout=180):
            if len(notes) >= args.max_rows:
                raise ValueError("Note row limit exceeded; refusing partial export")
            note = dict(row)
            if note["note_id"] in ids:
                raise ValueError("Duplicate note ID")
            ids.add(note["note_id"])
            category = classify_note(note, cohort)
            note["availability_status"] = category
            notes.append(note)
            categories[category] = categories.get(category, 0) + 1
            per_patient[note["subject_id"]][category] += 1
        path = output / "discharge.jsonl"
        path.write_text("".join(json.dumps(r, default=str) + "\n" for r in notes))
        (output / "coverage.json").write_text(json.dumps(per_patient, indent=2))
        manifest.update(
            status="completed",
            notes=len(notes),
            patients_with_notes=sum(any(v.values()) for v in per_patient.values()),
            patients_with_available_notes=sum(
                v["available_by_cutoff"] > 0 for v in per_patient.values()
            ),
            availability_counts=categories,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            job_id=job.job_id,
            billed_bytes=job.total_bytes_billed,
            actual_bytes=job.total_bytes_processed,
            cache_hit=job.cache_hit,
        )
        print(
            json.dumps(
                {
                    k: manifest[k]
                    for k in (
                        "notes",
                        "patients_with_notes",
                        "patients_with_available_notes",
                        "availability_counts",
                    )
                }
            ),
            flush=True,
        )
    except Exception as error:
        manifest.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        save()
    print(f"Saved note export: {output}", flush=True)


if __name__ == "__main__":
    main()
