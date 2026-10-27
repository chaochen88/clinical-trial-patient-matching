"""Local deterministic MIMIC export -> shared profiles and search projections."""

import argparse
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

from clinical_trial_patient_matching.extraction import digest
from clinical_trial_patient_matching.patients import (
    Claim,
    Record,
    build_profile,
    linked_fact,
)
from clinical_trial_patient_matching.search_documents import build_document

VERSION = "0.1"
TIME_BASIS = "mimic_shifted_clock_utc_tag_not_real_utc"


def shifted_time(value):
    """A UTC tag satisfies AwareDatetime; it does NOT unshift MIMIC timestamps."""
    if not value:
        return None
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is not None:
        raise ValueError("Expected MIMIC naive shifted DATETIME")
    return result.replace(tzinfo=UTC)


def number_or_text(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a measurement")
    try:
        number = float(value)
    except (ValueError, TypeError):
        return str(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite measurement")
    return number


def row_fact(
    table,
    row,
    manifest,
    *,
    concept,
    value=None,
    unit=None,
    clinical=None,
    available=None,
    context,
    assertion="affirmed",
    code=None,
    system=None,
    category="measurement",
    precision="datetime",
    status=None,
):
    source = json.dumps(row, sort_keys=True)
    # Full row digest distinguishes distinct observations and detects source changes.
    record = Record(
        patient_id=f"mimiciv:{row['subject_id']}",
        dataset=manifest["dataset"],
        record_id=f"{table}:{digest(row)}",
        version=manifest["exports"][table]["sha256"],
        text=source,
        available_time=available,
    )
    claim = Claim(
        concept=concept,
        value=value,
        unit=unit,
        clinical_time=clinical,
        assertion=assertion,
        experiencer="patient",
        time_context=context,
        quote=source,
        concept_code=code,
        concept_system=system,
    )
    original = {
        "source_format": "mimic_bigquery",
        "table": table,
        "row": row,
        "time_basis": TIME_BASIS,
        "time_precision": precision,
        "category": category,
    }
    return linked_fact(record, claim, original, status)


def convert_row(table, row, manifest, definitions):
    if table == "diagnoses":
        return row_fact(
            table,
            row,
            manifest,
            concept=f"Diagnosis coding: {row['long_title']}",
            code=row["icd_code"],
            system=f"MIMIC-ICD-{row['icd_version']}",
            context=f"Billing code for admission {row['hadm_id']}; "
            "onset and coding time unknown",
            category="condition",
            precision="unknown",
            status="billing_code",
        )
    if table == "labs":
        definition = definitions.get(row["itemid"])
        if definition is None:
            raise ValueError("Lab item absent from exported dictionary")
        label = definition["label"].casefold()
        concept = (
            "HbA1c"
            if re.search(r"hba1c|hemoglobin a1c|glycohemoglobin", label)
            else ("creatinine" if label == "creatinine" else None)
        )
        if concept is None or definition["fluid"].casefold() != "blood":
            raise ValueError("Lab concept requires a reviewed blood-item mapping")
        value = number_or_text(
            row["valuenum"] if row.get("valuenum") is not None else row.get("value")
        )
        return row_fact(
            table,
            row,
            manifest,
            concept=concept,
            value=value,
            unit=row.get("valueuom"),
            clinical=shifted_time(row.get("charttime")),
            available=shifted_time(row.get("storetime")),
            code=str(row["itemid"]),
            system=f"{manifest['dataset']}.d_labitems",
            context="Laboratory measurement; MIMIC shifted clock",
            status="recorded",
        )
    if table == "measurements":
        label = row["result_name"].casefold()
        if re.search(r"bmi|body mass", label):
            concept, unit = "BMI", "kg/m2" if re.search(r"kg/m[²2]", label) else None
        elif "height" in label:
            concept = "height"
            unit = "in" if "inch" in label else "cm" if "cm" in label else None
        elif "weight" in label:
            concept = "weight"
            unit = (
                "lb"
                if "lbs" in label or "pound" in label
                else "kg"
                if "kg" in label
                else None
            )
        else:
            raise ValueError("Unknown OMR concept")
        return row_fact(
            table,
            row,
            manifest,
            concept=concept,
            value=number_or_text(row["result_value"]),
            unit=unit,
            context=f"Recorded date {row['chartdate']}; "
            "exact time and availability unknown",
            precision="date",
            status="recorded",
        )
    if table == "prescriptions":
        return row_fact(
            table,
            row,
            manifest,
            concept=f"Medication prescription: {row['drug']}",
            value=number_or_text(row.get("dose_val_rx")),
            unit=row.get("dose_unit_rx"),
            clinical=shifted_time(row.get("starttime")),
            context=f"Prescription start; route {row.get('route')}; adherence unknown",
            category="medication",
            status="prescribed",
        )
    if table == "administrations":
        event = (row.get("event_txt") or "unknown").casefold()
        assertion = (
            "affirmed"
            if event == "administered"
            else "negated"
            if event in {"not given", "notgiven", "refused", "held"}
            else "possible"
        )
        return row_fact(
            table,
            row,
            manifest,
            concept=f"Medication administration: {row.get('medication')}",
            value=row.get("event_txt"),
            assertion=assertion,
            clinical=shifted_time(row.get("charttime")),
            available=shifted_time(row.get("storetime")),
            context="Administration event; no inference of outpatient adherence",
            category="medication",
            status=row.get("event_txt"),
        )
    raise ValueError("Unsupported source table")


def convert_export(source: Path, output: Path):
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["status"] != "completed":
        raise ValueError("Conversion requires a completed export")

    def read(table):
        path = source / f"{table}.jsonl"
        raw = path.read_bytes()
        expected = manifest["exports"][table]
        if hashlib.sha256(raw).hexdigest() != expected["sha256"]:
            raise ValueError(f"Source hash mismatch: {table}")
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
        if len(rows) != expected["rows"]:
            raise ValueError(f"Source row count mismatch: {table}")
        return rows

    cohort = read("cohort")
    if not cohort or len({row["subject_id"] for row in cohort}) != len(cohort):
        raise ValueError("Cohort must contain unique patient IDs")
    cutoffs = {
        row["subject_id"]: shifted_time(row["assessment_time"]) for row in cohort
    }
    if any(time is None for time in cutoffs.values()):
        raise ValueError("Assessment cutoff is required")
    definitions = {row["itemid"]: row for row in read("lab_dictionary")}
    facts = {pid: [] for pid in cutoffs}
    findings = {pid: list(manifest.get("gaps", [])) for pid in cutoffs}
    failures, omitted = [], []
    for row in cohort:
        pid = row["subject_id"]
        # Year-based age is approximate; older ages are additionally obscured.
        facts[pid].append(
            row_fact(
                "cohort",
                row,
                manifest,
                concept="approximate age",
                value=number_or_text(row["approximate_age"]),
                unit="years",
                clinical=shifted_time(row.get("admittime")),
                context="Approximate admission age; older ages obscured",
                category="demographic",
                precision="year-derived",
                status="approximate",
            )
        )
        facts[pid].append(
            row_fact(
                "cohort",
                row,
                manifest,
                concept="recorded gender",
                value=row.get("gender"),
                context="Patient table demographic",
                precision="unknown",
                category="demographic",
                status="recorded",
            )
        )
    for table in (
        "diagnoses",
        "labs",
        "measurements",
        "prescriptions",
        "administrations",
    ):
        for index, row in enumerate(read(table)):
            pid = row["subject_id"]
            if pid not in cutoffs:
                raise ValueError(f"Patient outside cohort in {table}")
            try:
                if table == "measurements":
                    event = shifted_time(row.get("chartdate"))
                elif table == "diagnoses":
                    event = shifted_time(row.get("availability_basis"))
                else:
                    event = shifted_time(row.get("charttime", row.get("starttime")))
                available = shifted_time(row.get("storetime"))
                if any(time and time > cutoffs[pid] for time in (event, available)):
                    omitted.append(
                        {
                            "table": table,
                            "row_index": index,
                            "reason": "future relative to patient cutoff",
                        }
                    )
                    findings[pid].append("Source rows omitted after patient cutoff")
                    continue
                fact = convert_row(table, row, manifest, definitions)
                facts[pid].append(fact)
                if fact.unit is None and fact.concept in {
                    "HbA1c",
                    "creatinine",
                    "BMI",
                    "height",
                    "weight",
                }:
                    findings[pid].append(
                        f"Missing unit for {fact.concept}; review source"
                    )
            except (ValueError, TypeError, KeyError) as error:
                # Retain errors and source pointers without inventing evidence.
                failures.append(
                    {"table": table, "row_index": index, "error": str(error)}
                )
                findings[pid].append(
                    f"Row conversion failed in {table}; see processing report"
                )
    output.mkdir(parents=True, exist_ok=True)
    profile_dir = output / "profiles"
    profile_dir.mkdir(exist_ok=True)
    totals, statuses, present = 0, {}, {"HbA1c": 0, "BMI": 0}
    with (output / "documents.jsonl").open("w", encoding="utf-8") as docs:
        for pid in sorted(cutoffs):
            configuration = {
                "provider": "python-mimic",
                "adapter_version": VERSION,
                "time_basis": TIME_BASIS,
                "source_manifest_sha256": hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
                "assessment_time": cutoffs[pid].isoformat(),
            }
            for concept in present:
                if not any(f.concept == concept for f in facts[pid]):
                    findings[pid].append(
                        f"Missing {concept} evidence; do not infer a match"
                    )
                else:
                    present[concept] += 1
            findings[pid].extend(
                [
                    "Exact age and date-only OMR timing require review",
                    "Billing coding does not establish onset or current diagnosis",
                ]
            )
            profile = build_profile(
                [], None, configuration, facts[pid], sorted(set(findings[pid]))
            )
            path = profile_dir / f"{pid}.json"
            path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
            document = build_document(
                profile,
                cutoffs[pid],
                {
                    "time_basis": TIME_BASIS,
                    "profile_path": str(path),
                    "source_dataset": manifest["dataset"],
                    "source_export": str(source),
                    "adapter_version": VERSION,
                },
            )
            docs.write(document.model_dump_json() + "\n")
            totals += len(profile.facts)
            statuses[profile.status] = statuses.get(profile.status, 0) + 1
    report = {
        "adapter_version": VERSION,
        "patients": len(cutoffs),
        "facts": totals,
        "profiles_by_status": statuses,
        "patients_with_evidence": present,
        "row_failures": failures,
        "omitted_rows": omitted,
        "time_basis": TIME_BASIS,
        "assessment_basis": "per-patient discharge",
        "source_manifest_sha256": hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
        "network_or_llm_calls": 0,
        "eligibility_assessed": False,
    }
    (output / "processing_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=Path("data/mimic/NCT03926806/initial-20")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/mimic/NCT03926806/processed-20")
    )
    args = parser.parse_args()
    report = convert_export(args.source, args.output)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "patients",
                    "facts",
                    "profiles_by_status",
                    "patients_with_evidence",
                )
            }
        )
    )
    print(
        f"Row failures: {len(report['row_failures'])}; "
        f"omitted rows: {len(report['omitted_rows'])}"
    )


if __name__ == "__main__":
    main()
