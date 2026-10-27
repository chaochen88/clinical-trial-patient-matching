"""Export bounded MIMIC evidence for NCT03926806; never calls an external LLM."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import bigquery


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")


def cohort_sql(dataset):
    return f"""
    WITH candidates AS (
      SELECT p.subject_id, p.gender, p.anchor_age, p.anchor_year,
             a.hadm_id, a.admittime, a.dischtime AS assessment_time,
             p.anchor_age + EXTRACT(YEAR FROM a.admittime) - p.anchor_year
               AS approximate_age,
             ROW_NUMBER() OVER (PARTITION BY p.subject_id
                               ORDER BY a.dischtime DESC, a.hadm_id DESC)
               AS admission_rank
      FROM `{dataset}.patients` p
      JOIN `{dataset}.admissions` a USING (subject_id)
      WHERE a.dischtime IS NOT NULL
        AND p.anchor_age + EXTRACT(YEAR FROM a.admittime) - p.anchor_year >= 18
        AND EXISTS (
          SELECT 1 FROM `{dataset}.diagnoses_icd` d
          JOIN `{dataset}.d_icd_diagnoses` names USING (icd_code, icd_version)
          WHERE d.subject_id = a.subject_id AND d.hadm_id = a.hadm_id
            AND REGEXP_CONTAINS(LOWER(names.long_title), r'type (2|ii)([^a-z0-9]|$)')
            AND REGEXP_CONTAINS(LOWER(names.long_title), r'diabet')
        )
    )
    SELECT * EXCEPT(admission_rank) FROM candidates WHERE admission_rank = 1
    ORDER BY subject_id LIMIT @patient_count
    """


def evidence_queries(dataset):
    cohort = "UNNEST(@cohort) c"
    cutoff = "c.assessment_time"
    start = "DATETIME_SUB(c.assessment_time, INTERVAL @lookback_days DAY)"
    return {
        "diagnoses": f"""
          SELECT d.subject_id, d.hadm_id, d.seq_num, d.icd_code, d.icd_version,
                 n.long_title, a.admittime, a.dischtime AS availability_basis
          FROM `{dataset}.diagnoses_icd` d
          JOIN `{dataset}.d_icd_diagnoses` n USING(icd_code, icd_version)
          JOIN `{dataset}.admissions` a USING(subject_id, hadm_id)
          JOIN {cohort} ON c.subject_id = d.subject_id
          WHERE a.dischtime <= {cutoff}
          ORDER BY d.subject_id, a.dischtime, d.hadm_id, d.seq_num
        """,
        "labs": f"""
          SELECT l.labevent_id, l.subject_id, l.hadm_id, l.itemid, n.label, n.fluid,
                 l.charttime, l.storetime, l.value, l.valuenum, l.valueuom, l.flag
          FROM `{dataset}.labevents` l
          JOIN `{dataset}.d_labitems` n USING(itemid)
          JOIN {cohort} ON c.subject_id = l.subject_id
          WHERE l.itemid IN UNNEST(@lab_itemids)
            AND l.charttime BETWEEN {start} AND {cutoff}
            AND (l.storetime IS NULL OR l.storetime <= {cutoff})
          ORDER BY l.subject_id, l.charttime, l.labevent_id
        """,
        "measurements": f"""
          SELECT o.subject_id, o.chartdate, o.seq_num, o.result_name, o.result_value
          FROM `{dataset}.omr` o JOIN {cohort} ON c.subject_id = o.subject_id
          WHERE o.chartdate BETWEEN DATE({start}) AND DATE({cutoff})
            AND REGEXP_CONTAINS(LOWER(o.result_name), r'bmi|body mass|height|weight')
          ORDER BY o.subject_id, o.chartdate, o.seq_num, o.result_name
        """,
        "prescriptions": f"""
          SELECT p.subject_id, p.hadm_id, p.pharmacy_id, p.starttime, p.stoptime,
                 p.drug_type, p.drug, p.dose_val_rx, p.dose_unit_rx, p.route,
                 p.form_val_disp, p.form_unit_disp
          FROM `{dataset}.prescriptions` p JOIN {cohort} ON c.subject_id = p.subject_id
          WHERE p.starttime BETWEEN {start} AND {cutoff}
          ORDER BY p.subject_id, p.starttime, p.pharmacy_id
        """,
        "administrations": f"""
          SELECT e.subject_id, e.hadm_id, e.emar_id, e.emar_seq, e.charttime,
                 e.storetime, e.medication, e.event_txt, e.pharmacy_id
          FROM `{dataset}.emar` e JOIN {cohort} ON c.subject_id = e.subject_id
          WHERE e.charttime BETWEEN {start} AND {cutoff}
            AND (e.storetime IS NULL OR e.storetime <= {cutoff})
          ORDER BY e.subject_id, e.charttime, e.emar_id
        """,
    }


def cohort_parameter(rows):
    return bigquery.ArrayQueryParameter(
        "cohort",
        "STRUCT",
        [
            bigquery.StructQueryParameter(
                None,
                bigquery.ScalarQueryParameter("subject_id", "INT64", row["subject_id"]),
                bigquery.ScalarQueryParameter(
                    "assessment_time", "DATETIME", row["assessment_time"]
                ),
            )
            for row in rows
        ],
    )


def audit_export(root):
    """Check stored rows locally; only aggregate counts are printed."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["status"] != "completed":
        raise ValueError("Cannot audit an incomplete export as complete")

    def read(name):
        path = root / f"{name}.jsonl"
        expected = manifest["exports"][name]
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
            raise ValueError(f"Content hash changed for {name}")
        rows = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]
        if len(rows) != expected["rows"]:
            raise ValueError(f"Row count changed for {name}")
        return rows

    cohorts = read("cohort")
    cutoffs = {
        row["subject_id"]: datetime.fromisoformat(row["assessment_time"])
        for row in cohorts
    }
    if len(cutoffs) != len(cohorts):
        raise ValueError("Duplicate patient in cohort")
    definitions = {row["itemid"]: row["label"] for row in read("lab_dictionary")}
    coverage = {
        pid: {
            "subject_id": pid,
            "counts": {},
            "has_hba1c": False,
            "has_bmi": False,
            "has_height": False,
            "has_weight": False,
        }
        for pid in cutoffs
    }
    counts = {}
    for name, date_field in (
        ("diagnoses", "availability_basis"),
        ("labs", "charttime"),
        ("measurements", "chartdate"),
        ("prescriptions", "starttime"),
        ("administrations", "charttime"),
    ):
        records = read(name)
        counts[name] = len(records)
        for row in records:
            pid = row["subject_id"]
            if pid not in cutoffs:
                raise ValueError(f"Unexpected patient in {name}")
            value = row.get(date_field)
            if value:
                event = datetime.fromisoformat(value)
                if event > cutoffs[pid]:
                    raise ValueError(f"Future evidence in {name}")
            if (
                row.get("storetime")
                and datetime.fromisoformat(row["storetime"]) > cutoffs[pid]
            ):
                raise ValueError(f"Future availability in {name}")
            item = coverage[pid]
            item["counts"][name] = item["counts"].get(name, 0) + 1
            if name == "labs":
                label = definitions.get(row["itemid"], "").lower()
                if row.get("valuenum") is not None and re.search(
                    r"hba1c|hemoglobin a1c|glycohemoglobin", label
                ):
                    item["has_hba1c"] = True
            if name == "measurements":
                label = row["result_name"].lower()
                for field, pattern in (
                    ("bmi", r"bmi|body mass"),
                    ("height", r"height"),
                    ("weight", r"weight"),
                ):
                    if re.search(pattern, label):
                        item[f"has_{field}"] = True
    summary = {
        "patients": len(cutoffs),
        "rows": counts,
        "patients_with": {
            key: sum(row[f"has_{key}"] for row in coverage.values())
            for key in ("hba1c", "bmi", "height", "weight")
        },
        "membership_and_cutoffs": "passed",
        "meaning": "Record availability only; not eligibility or verified completeness",
    }
    write_json(
        root / "coverage.json",
        {"summary": summary, "patients": list(coverage.values())},
    )
    print(json.dumps(summary, indent=2))
    return summary


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="clinical-trial-matching-512314")
    parser.add_argument(
        "--dataset", help="Full project.dataset name; auto-detect versioned hosp"
    )
    parser.add_argument("--patients", type=int, default=20)
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--max-gib-per-query", type=float, default=5)
    parser.add_argument("--max-gib-total", type=float, default=20)
    parser.add_argument("--max-rows-per-table", type=int, default=100000)
    parser.add_argument("--inspect", action="store_true", help="Metadata only")
    parser.add_argument(
        "--audit-only", action="store_true", help="Validate local completed export"
    )
    parser.add_argument(
        "--execute", action="store_true", help="Run bounded exports after dry runs"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.audit_only:
        if not args.output:
            parser.error("--audit-only requires --output")
        audit_export(args.output)
        return
    if (
        not 1 <= args.patients <= 20
        or min(
            args.lookback_days,
            args.max_gib_per_query,
            args.max_gib_total,
            args.max_rows_per_table,
        )
        <= 0
    ):
        parser.error("Use 1–20 patients and positive limits")
    client = bigquery.Client(project=args.project)
    dataset = args.dataset
    if not dataset:
        visible = [d.dataset_id for d in client.list_datasets("physionet-data")]
        choices = [
            name
            for name in ("mimiciv_3_1_hosp", "mimiciv_v3_1_hosp")
            if name in visible
        ]
        if not choices:
            raise SystemExit(
                "No versioned hosp dataset visible; specify its ID with --dataset"
            )
        dataset = "physionet-data." + choices[0]
    if not re.fullmatch(r"[a-zA-Z0-9_-]+\.[a-zA-Z0-9_]+", dataset):
        parser.error("dataset must be a project.dataset identifier")
    location = client.get_dataset(dataset).location
    tables = (
        "patients",
        "admissions",
        "diagnoses_icd",
        "d_icd_diagnoses",
        "labevents",
        "d_labitems",
        "omr",
        "prescriptions",
        "emar",
    )
    schemas = {
        name: [f.name for f in client.get_table(f"{dataset}.{name}").schema]
        for name in tables
    }
    print(
        json.dumps(
            {
                "project": args.project,
                "dataset": dataset,
                "location": location,
                "tables": schemas,
            },
            indent=2,
        ),
        flush=True,
    )
    if args.inspect:
        return
    root = args.output or Path("data/mimic/NCT03926806") / datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "trial_id": "NCT03926806",
        "project": args.project,
        "dataset": dataset,
        "location": location,
        "limits": {
            "max_gib_per_query": args.max_gib_per_query,
            "max_gib_total_estimate": args.max_gib_total,
            "max_rows_per_table": args.max_rows_per_table,
        },
        "schemas": schemas,
        "queries": [],
        "lookback_days": args.lookback_days,
        "exports": {},
        "time_basis": "MIMIC deidentified DATETIME, not real-world UTC",
        "assessment": "Latest T2/II-coded admission; discharge cutoff",
        "cohort_limitation": (
            "Dictionary title selection includes some ICD9 unspecified-type codes; "
            "not confirmed T2DM or eligibility"
        ),
        "gaps": [
            "Diet, alcohol, supplements and allergies are incompletely covered",
            "Billing diagnoses do not establish onset or verified coding availability",
            "Medication records do not verify adherence or stable outpatient treatment",
            "One-year measurement/medication lookback is incomplete history",
            "Prescription stoptime may reflect a later change; availability unknown",
            "OMR has date precision; same-day availability is unknown",
            "OMR missing height/weight units must be reviewed before BMI calculation",
            "Raw exports require a MIMIC adapter before existing profile workflows",
        ],
    }
    per_query = int(args.max_gib_per_query * 1024**3)
    total_limit = int(args.max_gib_total * 1024**3)
    estimated = 0

    def fetch(name, sql, parameters):
        nonlocal estimated
        dry = client.query(
            sql,
            location=location,
            job_config=bigquery.QueryJobConfig(
                dry_run=True, use_query_cache=False, query_parameters=parameters
            ),
        )
        size = dry.total_bytes_processed or 0
        info = {
            "name": name,
            "estimated_bytes": size,
            "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
        }
        manifest["queries"].append(info)
        (root / f"{name}.sql").write_text(sql, encoding="utf-8")
        write_json(root / "manifest.json", manifest)
        print(f"{name}: dry-run estimate {size / 1024**3:.3f} GiB", flush=True)
        if size > per_query or estimated + size > total_limit:
            raise RuntimeError(
                "Query exceeds configured byte budget; no execution for this query"
            )
        estimated += size
        if not args.execute:
            return []
        job = client.query(
            sql,
            location=location,
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=per_query, query_parameters=parameters
            ),
        )
        data = []
        for row in job.result(timeout=180):
            if len(data) >= args.max_rows_per_table:
                raise RuntimeError(
                    f"{name}: export row limit reached; refusing an incomplete export"
                )
            data.append(dict(row))
        info.update(
            job_id=job.job_id,
            actual_bytes=job.total_bytes_processed,
            billed_bytes=job.total_bytes_billed,
            cache_hit=job.cache_hit,
            rows=len(data),
        )
        path = root / f"{name}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in data:
                handle.write(json.dumps(row, default=str) + "\n")
        manifest["exports"][name] = {
            "rows": len(data),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        write_json(root / "manifest.json", manifest)
        return data

    try:
        count = bigquery.ScalarQueryParameter("patient_count", "INT64", args.patients)
        rows = fetch("cohort", cohort_sql(dataset), [count])
        if not args.execute:
            manifest["status"] = "dry_run_only"
            return
        if (
            len(rows) != args.patients
            or len({r["subject_id"] for r in rows}) != args.patients
        ):
            raise RuntimeError("Did not obtain the requested number of unique patients")
        labs = fetch(
            "lab_dictionary",
            f"""
          SELECT itemid, label, fluid, category FROM `{dataset}.d_labitems`
          WHERE REGEXP_CONTAINS(LOWER(label),
                r'hba1c|hemoglobin a1c|glycohemoglobin|^creatinine$')
            AND LOWER(fluid) = 'blood' ORDER BY itemid
        """,
            [],
        )
        if not labs:
            raise RuntimeError(
                "No lab definitions matched; review the source dictionary"
            )
        parameters = [
            cohort_parameter(rows),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", args.lookback_days),
        ]
        for name, sql in evidence_queries(dataset).items():
            supplied = list(parameters) if "@lookback_days" in sql else [parameters[0]]
            if name == "labs":
                supplied.append(
                    bigquery.ArrayQueryParameter(
                        "lab_itemids", "INT64", [r["itemid"] for r in labs]
                    )
                )
            fetch(name, sql, supplied)
        manifest["status"] = "completed"
        print(f"Saved evidence for {len(rows)} patients to {root}", flush=True)
    except Exception as error:
        manifest.update(
            status="partial_or_failed", error=f"{type(error).__name__}: {error}"
        )
        raise
    finally:
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        write_json(root / "manifest.json", manifest)
    if manifest["status"] == "completed":
        audit_export(root)


if __name__ == "__main__":
    main()
