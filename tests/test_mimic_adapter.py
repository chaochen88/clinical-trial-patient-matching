"""Synthetic adapter fixtures only; these tests do not read real patient data."""

import hashlib
import json
from datetime import UTC, datetime

import pytest

from clinical_trial_patient_matching.mimic_adapter import (
    TIME_BASIS,
    convert_export,
    shifted_time,
)
from clinical_trial_patient_matching.patients import Profile


def fixture_export(root, *, future=False, duplicate=False, invalid=False):
    root.mkdir()
    lab = {
        "subject_id": 1,
        "itemid": 1,
        "labevent_id": 101,
        "charttime": "2120-01-11 12:00:00" if future else "2120-01-02 12:00:00",
        "storetime": "2120-01-02 13:00:00",
        "value": "7.2",
        "valuenum": "NaN" if invalid else 7.2,
        "valueuom": "%",
    }
    tables = {
        "cohort": [
            {
                "subject_id": 1,
                "hadm_id": 100,
                "gender": "F",
                "approximate_age": 60,
                "admittime": "2120-01-01 00:00:00",
                "assessment_time": "2120-01-10 23:59:59",
            }
        ],
        "lab_dictionary": [{"itemid": 1, "label": "Hemoglobin A1c", "fluid": "Blood"}],
        "labs": [lab, lab] if duplicate else [lab],
        "diagnoses": [
            {
                "subject_id": 1,
                "hadm_id": 100,
                "icd_code": "E119",
                "icd_version": 10,
                "long_title": "Synthetic diabetes code",
                "availability_basis": "2120-01-10 23:59:59",
            }
        ],
        "measurements": [
            {
                "subject_id": 1,
                "chartdate": "2120-01-03",
                "seq_num": 1,
                "result_name": "BMI (kg/m2)",
                "result_value": "29",
            },
            {
                "subject_id": 1,
                "chartdate": "2120-01-03",
                "seq_num": 2,
                "result_name": "BMI (kg/m2)",
                "result_value": "31",
            },
        ],
        "prescriptions": [
            {
                "subject_id": 1,
                "drug": "synthetic drug",
                "dose_val_rx": "5-10",
                "dose_unit_rx": "mg",
                "route": "PO",
                "starttime": "2120-01-01 12:00:00",
                "stoptime": "2121-01-01",
            }
        ],
        "administrations": [
            {
                "subject_id": 1,
                "medication": "synthetic drug",
                "event_txt": "Not Given",
                "charttime": "2120-01-03 12:00:00",
                "storetime": "2120-01-03 13:00:00",
            }
        ],
    }
    manifest = {
        "status": "completed",
        "dataset": "synthetic_test_variant",
        "gaps": [],
        "exports": {},
    }
    for name, rows in tables.items():
        path = root / f"{name}.jsonl"
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        manifest["exports"][name] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_local_conversion_preserves_qualifiers_and_conflicts(tmp_path):
    source = fixture_export(tmp_path / "source", duplicate=True)
    out = tmp_path / "processed"
    report = convert_export(source, out)
    profile = Profile.model_validate_json((out / "profiles/1.json").read_text())
    assert report["patients"] == 1 and not report["row_failures"]
    assert report["network_or_llm_calls"] == 0 and profile.status == "partial"
    assert len([f for f in profile.facts if f.concept == "HbA1c"]) == 1
    bmi = [f for f in profile.facts if f.concept == "BMI"]
    assert all(f.clinical_time is None for f in bmi)
    assert all(f.original["time_precision"] == "date" for f in bmi)
    assert any(set(pair) == {f.fact_id for f in bmi} for pair in profile.conflicts)
    diagnosis = next(f for f in profile.facts if f.source_status == "billing_code")
    assert diagnosis.clinical_time is None and diagnosis.available_time is None
    prescription = next(f for f in profile.facts if f.source_status == "prescribed")
    assert prescription.value == "5-10"
    assert (
        next(f for f in profile.facts if f.source_status == "Not Given").assertion
        == "negated"
    )
    doc = json.loads((out / "documents.jsonl").read_text())
    assert "2121" not in doc["text"]
    assert doc["source_metadata"]["time_basis"] == TIME_BASIS
    first_version = profile.profile_version
    convert_export(source, out)
    assert (
        Profile.model_validate_json(
            (out / "profiles/1.json").read_text()
        ).profile_version
        == first_version
    )


def test_future_rows_omitted_and_invalid_values_retained_as_failures(tmp_path):
    future = convert_export(
        fixture_export(tmp_path / "future", future=True), tmp_path / "out1"
    )
    assert len(future["omitted_rows"]) == 1
    assert future["patients_with_evidence"]["HbA1c"] == 0
    invalid = convert_export(
        fixture_export(tmp_path / "invalid", invalid=True), tmp_path / "out2"
    )
    assert len(invalid["row_failures"]) == 1
    assert invalid["patients_with_evidence"]["HbA1c"] == 0


def test_source_hash_and_shifted_clock(tmp_path):
    source = fixture_export(tmp_path / "source")
    with (source / "labs.jsonl").open("a") as handle:
        handle.write("\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        convert_export(source, tmp_path / "out")
    tagged = shifted_time("2120-01-02 12:00:00")
    assert tagged == datetime(2120, 1, 2, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="naive"):
        shifted_time("2120-01-02T12:00:00Z")
