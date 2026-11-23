"""Synthetic notes only; no restricted records, BigQuery jobs or live model calls."""

import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from clinical_trial_patient_matching.mimic_adapter import TIME_BASIS, shifted_time
from clinical_trial_patient_matching.mimic_notes import (
    availability,
    chunks,
    enrich_profiles,
    extract_note,
    quote_span,
)
from clinical_trial_patient_matching.patients import (
    Claim,
    Profile,
    Record,
    build_profile,
    linked_fact,
)


def fixture(tmp_path):
    source, hospital, notes = [
        tmp_path / name for name in ("source", "hospital", "notes")
    ]
    for path in (source, hospital / "profiles", notes):
        path.mkdir(parents=True)
    cohort = {"subject_id": 1, "hadm_id": 100, "assessment_time": "2120-01-10 23:59:59"}
    raw = (json.dumps(cohort) + "\n").encode()
    (source / "cohort.jsonl").write_bytes(raw)
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "project": "synthetic-project",
                "dataset": "synthetic.hosp",
                "location": "US",
                "exports": {"cohort": {"sha256": hashlib.sha256(raw).hexdigest()}},
            }
        )
    )
    source_hash = hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest()
    cutoff = shifted_time(cohort["assessment_time"])
    record = Record(
        patient_id="mimiciv:1",
        dataset="synthetic_hosp",
        record_id="lab1",
        text="Synthetic HbA1c 7.2%",
        available_time=cutoff,
    )
    fact = linked_fact(
        record,
        Claim(
            concept="HbA1c",
            value=7.2,
            unit="%",
            assertion="affirmed",
            experiencer="patient",
            clinical_time=cutoff,
            time_context="synthetic lab",
            quote=record.text,
        ),
        {"text": record.text},
    )
    profile = build_profile(
        [],
        None,
        {
            "source_manifest_sha256": source_hash,
            "time_basis": TIME_BASIS,
            "assessment_time": cutoff.isoformat(),
        },
        [fact],
    )
    (hospital / "profiles/1.json").write_text(profile.model_dump_json())
    note = {
        "note_id": "1-DS-1",
        "subject_id": 1,
        "hadm_id": 100,
        "charttime": "2120-01-09 12:00:00",
        "storetime": "2120-01-09 13:00:00",
        "text": "Synthetic history: drinks 1 drink per week.",
        "admittime": "2120-01-01 00:00:00",
        "dischtime": "2120-01-09 12:00:00",
        "is_selected_admission": True,
    }
    rows = [
        note,
        {**note, "note_id": "1-DS-2", "storetime": "2120-01-11 13:00:00"},
        {**note, "note_id": "1-DS-3", "storetime": None},
    ]
    note_raw = "".join(json.dumps(r) + "\n" for r in rows).encode()
    (notes / "discharge.jsonl").write_bytes(note_raw)
    manifest = {
        "status": "completed",
        "dataset": "synthetic_note",
        "hospital_export": str(source),
        "hospital_manifest_sha256": source_hash,
        "cohort_sha256": hashlib.sha256(raw).hexdigest(),
        "sha256": hashlib.sha256(note_raw).hexdigest(),
        "notes": len(rows),
    }
    (notes / "manifest.json").write_text(json.dumps(manifest))
    return notes, hospital, profile, note, manifest, cutoff


def response(quote="drinks 1 drink per week."):
    return dict(
        facts=[
            dict(
                concept="alcohol consumption",
                value=1,
                unit="drinks/week",
                assertion="affirmed",
                experiencer="patient",
                clinical_time=None,
                time_context="reported social history",
                quote=quote,
                temporal_scope="historical",
            )
        ],
        findings=[],
    )


def test_enrichment_preserves_provenance_and_does_not_process_late_notes(tmp_path):
    notes, hospital, old, note, _, _ = fixture(tmp_path)
    provider = Mock(return_value=response())
    output = tmp_path / "combined"
    report = enrich_profiles(
        notes, hospital, output, provider, {"provider": "synthetic"}
    )
    assert provider.call_count == 1
    assert len(report["deferred_notes"]) == 2
    combined = Profile.model_validate_json((output / "profiles/1.json").read_text())
    assert old.facts[0] in combined.facts
    added = next(f for f in combined.facts if f.concept == "alcohol consumption")
    assert added.clinical_time is None and added.available_time is not None
    assert added.original["temporal_scope"] == "historical"
    assert added.original["extraction_verified"] is False
    assert (
        note["text"][added.original["span_start"] : added.original["span_end"]]
        == added.quote
    )
    assert added.original["note_metadata"]["hadm_id"] == 100
    assert combined.profile_version != old.profile_version
    document = json.loads((output / "documents.jsonl").read_text())
    assert "alcohol consumption" in document["text"]
    assert "unknown date" in document["text"]


def test_bad_quote_repairs_once_and_success_does_not_retry(tmp_path):
    _, _, profile, note, manifest, cutoff = fixture(tmp_path)
    provider = Mock(side_effect=[response("invented quote"), response()])
    facts, attempts, _ = extract_note(note, profile, manifest, cutoff, provider)
    assert len(facts) == 1 and len(attempts) == 2
    assert attempts[0]["status"] == "partial"
    failing = Mock(return_value=response("invented quote"))
    facts, attempts, findings = extract_note(note, profile, manifest, cutoff, failing)
    assert not facts and len(attempts) == 2 and findings
    for invalid in (
        {**note, "storetime": "2120-02-01 12:00:00"},
        {**note, "subject_id": 2},
    ):
        provider.reset_mock()
        with pytest.raises(ValueError):
            extract_note(invalid, profile, manifest, cutoff, provider)
        provider.assert_not_called()


def test_changed_export_fails_before_provider_and_dates_are_not_invented(tmp_path):
    notes, hospital, profile, note, manifest, cutoff = fixture(tmp_path)
    invalid = response()
    invalid["facts"][0]["clinical_time"] = cutoff.isoformat()
    provider = Mock(return_value=invalid)
    facts, attempts, _ = extract_note(note, profile, manifest, cutoff, provider)
    assert not facts and len(attempts) == 2
    (notes / "discharge.jsonl").write_text("tampered")
    provider.reset_mock()
    with pytest.raises(ValueError, match="hash"):
        enrich_profiles(notes, hospital, tmp_path / "combined", provider, {})
    provider.assert_not_called()


def test_chunk_coverage_and_availability(tmp_path):
    text = "synthetic text\n" * 100
    covered = set()
    for offset, fragment in chunks(text, size=100, overlap=10):
        assert fragment == text[offset : offset + len(fragment)]
        covered.update(range(offset, offset + len(fragment)))
    assert covered == set(range(len(text)))
    _, _, _, note, _, cutoff = fixture(tmp_path)
    assert availability(note, cutoff) == "available_by_cutoff"
    assert availability({**note, "storetime": None}, cutoff) == "unknown_availability"
    script = Path(__file__).parents[1] / "scripts/fetch_mimic_notes.py"
    spec = importlib.util.spec_from_file_location("notes_fetcher", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sql = module.note_sql("synthetic.notes", "synthetic.hosp")
    assert "a.hadm_id = n.hadm_id" in sql and "c.subject_id" in sql
    with pytest.raises(ValueError):
        module.note_sql("unsafe`; select", "synthetic.hosp")
    assert module.classify_note(
        note, {1: {"assessment_time": "2120-01-10 23:59:59"}}
    ) == ("available_by_cutoff")


def test_quote_alignment_is_whitespace_only_and_bad_claims_do_not_discard_good_ones(
    tmp_path,
):
    start, exact, kind = quote_span("Denies\n alcohol use.", "Denies alcohol use.")
    assert exact == "Denies\n alcohol use." and start == 0
    assert kind == "unique_whitespace_alignment"
    for text, quote in [
        ("Denies alcohol use.", "Reports alcohol use."),
        ("No\n milk. No\t milk.", "No milk."),
    ]:
        with pytest.raises(ValueError):
            quote_span(text, quote)
    _, _, profile, note, manifest, cutoff = fixture(tmp_path)
    mixed = response()
    invalid = {**mixed["facts"][0], "quote": "invented quote"}
    mixed["facts"].append(invalid)
    provider = Mock(return_value=mixed)
    facts, attempts, findings = extract_note(note, profile, manifest, cutoff, provider)
    assert len(facts) == 1 and provider.call_count == 2
    assert all(a["status"] == "partial" for a in attempts)
    assert findings


def test_general_allergy_statement_does_not_establish_lactose_intolerance(tmp_path):
    _, _, profile, note, manifest, cutoff = fixture(tmp_path)
    note = {**note, "text": "Synthetic allergies: NKDA."}
    data = response("NKDA.")
    data["facts"][0]["concept"] = "lactose intolerance"
    facts, attempts, _ = extract_note(
        note, profile, manifest, cutoff, Mock(return_value=data)
    )
    assert not facts and all(a["rejected_claims"] for a in attempts)


def test_notes_can_supply_dated_narrative_context_without_inventing_event_date(
    tmp_path,
):
    from clinical_trial_patient_matching.assessment import (
        Criterion,
        Trial,
        assess,
        available_note_context,
    )

    _, _, profile, note, manifest, cutoff = fixture(tmp_path)
    facts, _, _ = extract_note(
        note, profile, manifest, cutoff, Mock(return_value=response())
    )
    enriched = build_profile([], None, {"time_basis": TIME_BASIS}, facts)
    assert available_note_context(facts[0], cutoff, TIME_BASIS)
    assert not available_note_context(facts[0], cutoff, "calendar")
    criterion = Criterion(
        criterion_id="c1",
        criterion_type="exclusion",
        predicate="Alcohol consumption >2 drinks per day",
    )
    trial = Trial(
        trial_id="synthetic",
        trial_version="synthetic-v1",
        source_reference="synthetic",
        criteria=[criterion],
    )

    def interpret(context, feedback):
        assert context["facts"][0]["clinical_time"] is None
        assert "text" not in context["facts"][0]["original"]
        assert len(context["source_texts"]) == 1
        assert next(iter(context["source_texts"].values()))["text"] == note["text"]
        return dict(
            status="unknown",
            evidence_state="supported",
            citations=[dict(fact_id=facts[0].fact_id, quote=facts[0].quote)],
            reason="Historical report does not establish current intake",
            follow_up="Confirm current dated intake",
        )

    result = assess(enriched, trial, criterion, cutoff, TIME_BASIS, interpret)
    assert result.processing_status == "complete" and not result.verified
    assert result.evidence_ids == [facts[0].fact_id]


def test_note_exporter_rejects_large_dry_run_before_execution(tmp_path, monkeypatch):
    from types import SimpleNamespace

    notes, _, _, _, manifest, _ = fixture(tmp_path)
    script = Path(__file__).parents[1] / "scripts/fetch_mimic_notes.py"
    spec = importlib.util.spec_from_file_location("notes_budget_fetcher", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = Mock()
    client.get_dataset.return_value = SimpleNamespace(location="US")
    client.get_table.return_value = SimpleNamespace(schema=[])
    client.query.return_value = SimpleNamespace(total_bytes_processed=11 * 1024**3)
    monkeypatch.setattr(module.bigquery, "Client", lambda **kwargs: client)
    monkeypatch.setattr(module, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "notes",
            "--source",
            manifest["hospital_export"],
            "--dataset",
            "synthetic.notes",
            "--execute",
        ],
    )
    with pytest.raises(ValueError, match="budget"):
        module.main()
    assert client.query.call_count == 1
    assert client.query.call_args.kwargs["job_config"].dry_run
