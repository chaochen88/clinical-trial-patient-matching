"""Synthetic safety cases plus source fidelity checks for the Class 4 pipeline."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clinical_trial_patient_matching.criteria import Rule, Window
from clinical_trial_patient_matching.patients import (
    Claim,
    Record,
    build_bundle,
    build_profile,
    linked_fact,
    narrative_workflow,
    offline_patient_extractor,
    parse_fhir,
)


def record(text="A1c: 11.3%", **kwargs):
    return Record(
        patient_id="synthetic-patient",
        dataset="synthetic_test_variant",
        record_id="note",
        text=text,
        **kwargs,
    )


def claim(**kwargs):
    return Claim.model_validate(
        {
            "concept": "HbA1c",
            "value": 11.3,
            "unit": "%",
            "assertion": "affirmed",
            "experiencer": "patient",
            "time_context": "current presentation",
            "quote": "A1c: 11.3%",
            **kwargs,
        }
    )


def rule(concept="HbA1c", **kwargs):
    return Rule(
        kind="threshold",
        concept=concept,
        operator="le",
        value=9.5,
        unit="%",
        source_bullet_id="inclusion-3",
        criterion_type="inclusion",
        source_quote="Synthetic test rule",
        **kwargs,
    )


def test_duplicate_ingestion_and_versions():
    source = record()
    one = build_profile([source], offline_patient_extractor, {})
    two = build_profile([source, source], offline_patient_extractor, {})
    assert one.facts == two.facts
    assert len(two.attempts) == 1
    assert one.profile_version == two.profile_version
    changed = build_profile([record(version="2")], offline_patient_extractor, {})
    assert changed.profile_version != one.profile_version
    assert changed.facts[0].source_hash != one.facts[0].source_hash


def test_possible_and_injection_evidence():
    text = (
        "concern for possible diabetic ketoacidosis "
        "after not taking his insulin for 3 days. "
        "Most recently, he has been using insulin glargine 55 units once daily, "
        "and insulin aspart sliding scale 3 times daily."
    )
    profile = build_profile([record(text)], offline_patient_extractor, {})
    possible = next(f for f in profile.facts if f.concept == "diabetic ketoacidosis")
    assert possible.assertion == "possible" and possible.clinical_time is None
    duration = Rule(
        kind="duration",
        concept="multiple daily injections",
        source_bullet_id="inclusion-2",
        criterion_type="inclusion",
        source_quote="synthetic",
        operator="ge",
        value=3,
        unit="calendar_months",
        uninterrupted=True,
    )
    bundle = build_bundle(profile, duration, {})
    assert {f["concept"] for f in bundle["facts"]} == {
        "multiple daily injections",
        "insulin interruption",
    }
    assert "Request full dated medication-use history" in bundle["gaps"]


def test_conflict_variant_and_historical_trend():
    first = record(variant="conflict")
    second = record("HbA1c for this screening encounter is 8.2%.", variant="conflict")
    second = second.model_copy(
        update={"record_id": "addendum", "label": "synthetic_test_variant"}
    )
    profile = build_profile([first, second], offline_patient_extractor, {})
    assert len(profile.facts) == 2 and len(profile.conflicts) == 1
    bundle = build_bundle(profile, rule(), {})
    assert len(bundle["facts"]) == 2 and bundle["conflicts"]
    assert profile.facts[1].label == "synthetic_test_variant"
    earlier = linked_fact(first, claim(clinical_time="2020-01-01T00:00:00Z"), {})
    later = linked_fact(
        second, claim(value=8.2, clinical_time="2020-02-01T00:00:00Z"), {}
    )
    trend = build_profile([], None, {}, [earlier, later])
    assert not trend.conflicts


def test_missing_bmi_and_context_limit():
    profile = build_profile([record()], offline_patient_extractor, {})
    missing = build_bundle(profile, rule("BMI"), {})
    assert not missing["facts"]
    assert "Request measured BMI or dated height and weight" in missing["gaps"]
    truncated = build_bundle(profile, rule(), {}, max_characters=1)
    assert truncated["truncated"] and truncated["omitted"]


def test_negation_family_and_quote_validation():
    text = "Denies nausea. Mother has diabetes."
    response = {
        "facts": [
            claim(
                concept="nausea",
                assertion="negated",
                value=None,
                quote="Denies nausea.",
            ).model_dump(),
            claim(
                concept="diabetes",
                experiencer="family",
                value=None,
                quote="Mother has diabetes.",
            ).model_dump(),
        ],
        "findings": [],
    }
    profile = build_profile([record(text)], lambda t, f: response, {})
    assert profile.facts[0].assertion == "negated"
    assert profile.facts[1].experiencer == "family"
    assert not build_bundle(profile, rule("diabetes"), {})["facts"]
    result = narrative_workflow(
        lambda t, f: {"facts": [claim().model_dump()], "findings": []}
    ).invoke({"record": record(text)})
    assert len(result["attempts"]) == 2 and not result["facts"]
    assert "exact source span" in result["findings"][0]


def test_one_repair_and_provider_failure():
    calls = []

    def broken(text, feedback):
        calls.append(feedback)
        raise ValueError("provider unavailable")

    profile = build_profile([record()], broken, {})
    assert len(calls) == 2 and calls[1]
    assert profile.status == "failed" and profile.attempts


def test_dates_and_incompatible_units():
    source = record()
    facts = [
        linked_fact(source, claim(clinical_time="2030-01-01T00:00:00Z"), {}),
        linked_fact(source, claim(value=40, unit="mmol/mol"), {}),
    ]
    profile = build_profile([], None, {}, facts)
    bundle = build_bundle(profile, rule(), {}, datetime(2026, 1, 1, tzinfo=UTC))
    assert len(bundle["facts"]) == 1
    assert any("Incompatible unit" in gap for gap in bundle["gaps"])
    assert any("future" in omission["reason"] for omission in bundle["omitted"])
    windowed = build_bundle(profile, rule(window=Window(months=6)), {})
    assert "Criterion time window cannot be verified" in windowed["gaps"]


def test_fhir_reference_coding_and_provenance():
    observation = {
        "resourceType": "Observation",
        "id": "obs",
        "status": "final",
        "subject": {"reference": "urn:uuid:p"},
        "meta": {"versionId": "1"},
        "effectiveDateTime": "2018-11-17T06:52:03-05:00",
        "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]},
        "valueQuantity": {"value": 6.938, "unit": "%", "code": "%"},
    }
    bundle = {
        "entry": [
            {
                "fullUrl": "urn:uuid:p",
                "resource": {"resourceType": "Patient", "id": "p"},
            },
            {"resource": observation},
        ]
    }
    facts, findings = parse_fhir(bundle, "synthetic_test_variant")
    assert not findings and facts[0].concept == "HbA1c"
    assert facts[0].patient_id == "p" and facts[0].source_version == "1"
    assert facts[0].original == observation and facts[0].source_status == "final"
    observation["code"]["coding"][0]["system"] = "unrelated-system"
    assert parse_fhir(bundle, "test")[0][0].concept != "HbA1c"
    observation["subject"]["reference"] = "Patient/unknown"
    facts, findings = parse_fhir(bundle, "test")
    assert not facts and "unresolved" in findings[0]


def test_mixed_patients_rejected():
    with pytest.raises(ValueError, match="one patient"):
        build_profile(
            [record(), record().model_copy(update={"patient_id": "other"})],
            offline_patient_extractor,
            {},
        )


def test_published_source_examples():
    fixtures = Path(__file__).parent / "fixtures/class4"
    row = json.loads((fixtures / "queries.jsonl").read_text(encoding="utf-8"))
    source = Record(
        patient_id=row["_id"],
        dataset="TREC-2021",
        record_id=row["_id"],
        text=row["text"],
    )
    profile = build_profile([source], offline_patient_extractor, {})
    assert len(profile.facts) == 4
    assert all(f.quote in row["text"] for f in profile.facts)
    bundle = json.loads((fixtures / "synthea-hba1c.json").read_text(encoding="utf-8"))
    facts, findings = parse_fhir(bundle, "Synthea-FHIR-R4-Sep2019-subset")
    assert not findings and len(facts) == 1
    assert facts[0].patient_id == "338e0f93-2fb1-4152-8703-518686c567a8"
    assert facts[0].value == 6.938225162130798
    assert facts[0].unit == "%"
