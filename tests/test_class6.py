"""Synthetic Class 6 cases: predicate polarity, provenance and completeness."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from clinical_trial_patient_matching.assessment import (
    Criterion,
    Interpretation,
    Trial,
    aggregate,
    assess,
    build_workflow,
    combine,
    count_events,
    duration_status,
    eligibility_score,
)
from clinical_trial_patient_matching.criteria import Rule, Window
from clinical_trial_patient_matching.patients import (
    Claim,
    Record,
    build_profile,
    linked_fact,
)

CUTOFF = datetime(2021, 1, 13, 23, 59, tzinfo=UTC)


def fact(value=5.84, unit="%", concept="HbA1c", **updates):
    text = f"Synthetic source: {concept}={value} {unit}"
    record = Record(
        patient_id="synthetic",
        dataset="synthetic",
        record_id=text,
        text=text,
        available_time=CUTOFF,
    )
    claim = Claim(
        concept=concept,
        value=value,
        unit=unit,
        assertion="affirmed",
        experiencer="patient",
        clinical_time=CUTOFF,
        time_context="current presentation",
        quote=text,
    )
    return linked_fact(record, claim, {"text": text}).model_copy(update=updates)


def profile(*facts):
    return build_profile([], None, {"provider": "synthetic"}, list(facts))


def criterion(polarity="inclusion", **updates):
    fields = dict(
        kind="threshold",
        concept="HbA1c",
        operator="lt",
        value=8.5,
        unit="%",
        source_bullet_id="c1",
        criterion_type=polarity,
        source_quote="HbA1c <8.5%",
    )
    fields.update(updates)
    rule = Rule(**fields)
    return Criterion(
        criterion_id="c1",
        criterion_type=polarity,
        predicate=rule.source_quote,
        rule=rule,
        reviewed_mapping=True,
    )


def trial(c):
    return Trial(
        trial_id="synthetic-trial",
        trial_version="frozen-test",
        source_reference="synthetic",
        criteria=[c],
    )


def result(p, c=None, interpreter=None):
    c = c or criterion()
    return assess(p, trial(c), c, CUTOFF, "calendar", interpreter)


@pytest.mark.parametrize(
    ("value", "status"), [(5.84, "met"), (8.5, "not_met"), (8.51, "not_met")]
)
def test_strict_boundary_and_exclusion_predicate(value, status):
    p = profile(fact(value))
    assert result(p).status == status
    excluded = result(p, criterion("exclusion"))
    assert excluded.status == status  # Predicate semantics are identical.
    overall, _, _ = aggregate(trial(criterion("exclusion")), p, [excluded])
    assert overall == (
        "explicit_eligibility_conflict" if status == "met" else "potential_candidate"
    )


def test_unknown_units_future_uncertainty_and_source_integrity():
    for f in [
        fact(unit="mmol/mol"),
        fact(available_time=CUTOFF + timedelta(seconds=1)),
        fact(clinical_time=None),
        fact(available_time=None),
        fact(value=True),
        fact(assertion="possible"),
        fact(experiencer="family"),
    ]:
        assessed = result(profile(f))
        assert assessed.status == "unknown"
        assert assessed.follow_up
    for f in [fact(quote="invented source span"), fact(patient_id="other")]:
        p = profile(fact())
        p.facts = [f]
        assessed = result(p)
        assert assessed.processing_status == "failed"
        assert not assessed.verified


def test_conflicts_keep_both_values_and_historical_selection():
    p = profile(fact(11.3), fact(8.2))
    r = result(p)
    assert r.status == "unknown" and r.evidence_state == "conflicting"
    assert len(r.evidence_ids) == 2
    older = fact(11.3, clinical_time=CUTOFF - timedelta(days=5))
    assert result(profile(older, fact(8.2))).status == "met"


@pytest.mark.parametrize(
    ("statuses", "operator", "expected"),
    [
        (["met", "unknown"], "OR", "met"),
        (["met", "unknown"], "AND", "unknown"),
        (["not_met", "unknown"], "AND", "not_met"),
        (["not_met", "unknown"], "OR", "unknown"),
        (["not_met", "not_met"], "OR", "not_met"),
    ],
)
def test_three_valued_logic(statuses, operator, expected):
    assert combine(statuses, operator) == expected


def test_empty_and_unresolved_applicability_groups_rejected():
    for statuses in ([], ["not_applicable"], ["requires_review"]):
        with pytest.raises(ValueError):
            combine(statuses, "OR")


def test_group_polarity_applies_only_to_parent():
    child = criterion().rule
    alternative = child.model_copy(update={"concept": "BMI"})
    group = Rule(
        kind="group",
        concept="alternatives",
        logic="any",
        children=[child, alternative],
        source_bullet_id="c1",
        criterion_type="inclusion",
        source_quote="HbA1c <8.5% or BMI",
    )
    c = Criterion(
        criterion_id="c1",
        criterion_type="inclusion",
        rule=group,
        predicate=group.source_quote,
        reviewed_mapping=True,
    )
    p = profile(fact(), fact(2, concept="BMI", assertion="possible"))
    r = result(p, c)
    assert r.status == "met" and r.verified
    assert len(r.evidence_ids) == 1
    assert aggregate(trial(c), p, [r])[0] == "potential_candidate"


def test_explicit_scope_and_unknown_scope():
    c = criterion()
    c.applicability = Rule(
        kind="condition",
        source_bullet_id="c1",
        criterion_type="inclusion",
        source_quote="scope",
        concept="scope",
    )
    p = profile(fact(), fact(None, concept="scope", assertion="negated"))
    r = result(p, c)
    assert r.status == "not_applicable" and r.skipped_branches
    assert r.applicability_rule
    assert result(profile(fact()), c).status == "unknown"


def test_missing_required_parent_and_failure_precedence():
    c = criterion()
    p = profile(fact())
    assert aggregate(trial(c), p, [])[0] == "needs_review"
    assert aggregate(trial(c), p, [])[2]
    r = result(p)
    with pytest.raises(ValueError):
        aggregate(trial(c), p, [r, r])
    with pytest.raises(ValueError):
        aggregate(trial(c), p, [r.model_copy(update={"variant": "other"})])
    failed = result(profile(fact(11.3)))
    c2 = c.model_copy(update={"criterion_id": "unassessed", "rule": None})
    t = trial(c).model_copy(update={"criteria": [c, c2]})
    assert (
        aggregate(t, profile(fact(11.3)), [failed])[0]
        == "explicit_eligibility_conflict"
    )


def test_narrative_repair_limit_and_unknown_cannot_create_eligibility():
    c = criterion().model_copy(update={"rule": None, "reviewed_mapping": False})
    p = profile(fact())
    f = p.facts[0]
    good = dict(
        status="met",
        evidence_state="supported",
        reason="Synthetic",
        citations=[dict(fact_id=f.fact_id, quote=f.quote)],
        follow_up=None,
    )
    bad = {**good, "citations": [dict(fact_id="other-patient", quote=f.quote)]}
    provider = Mock(side_effect=[bad, good])
    r = result(p, c, provider)
    assert provider.call_count == 2 and r.processing_status == "complete"
    assert not r.verified
    assert aggregate(trial(c), p, [r])[0] == "needs_review"
    failing = Mock(return_value=bad)
    r = result(p, c, failing)
    assert failing.call_count == 2 and r.processing_status == "failed"
    assert r.status == "unknown"
    provider = Mock(return_value=good)
    result(p, c, provider)
    assert provider.call_count == 1


def test_windows_and_versioned_graph_report(tmp_path):
    c = criterion(window=Window(months=3, boundary="inclusive"))
    p = profile(fact(clinical_time=CUTOFF - timedelta(days=100)))
    assert result(p, c).status == "unknown"
    p = profile(fact())
    state = build_workflow(tmp_path).invoke(
        dict(profile=p, trial=trial(c), assessment_time=CUTOFF, time_basis="calendar")
    )
    assert state["report"].overall == "potential_candidate"
    assert (tmp_path / f"{state['report'].report_key}.json").exists()
    assert state["results"][0].trial_version == "frozen-test"
    with pytest.raises(ValueError):
        build_workflow(tmp_path).invoke(dict(profile=p, trial=trial(c)))


def test_schema_requires_evidence_and_follow_up():
    with pytest.raises(ValidationError):
        Interpretation(
            status="met", evidence_state="supported", citations=[], reason="x"
        )
    with pytest.raises(ValidationError):
        Interpretation(
            status="unknown", evidence_state="missing", citations=[], reason="x"
        )


def test_calendar_duration_and_distinct_event_count():
    rule = Rule(
        kind="duration",
        source_bullet_id="duration",
        criterion_type="inclusion",
        source_quote="Three continuous months",
        concept="insulin use",
        operator="ge",
        value=3,
        unit="months",
        uninterrupted=True,
    )
    start = datetime(2020, 10, 13, 23, 59, tzinfo=UTC)
    assert duration_status(rule, start, CUTOFF, CUTOFF, True) == "met"
    assert (
        duration_status(rule, start + timedelta(seconds=1), CUTOFF, CUTOFF, True)
        == "not_met"
    )
    assert duration_status(rule, start, CUTOFF, CUTOFF) == "unknown"
    assert duration_status(rule, start, CUTOFF, CUTOFF, False) == "not_met"
    assert (
        duration_status(rule, start, CUTOFF - timedelta(days=1), CUTOFF, True)
        == "unknown"
    )
    events = [
        ("a", start),
        ("a", start),
        ("b", CUTOFF),
        ("future", CUTOFF + timedelta(seconds=1)),
    ]
    assert count_events(events, start, CUTOFF, True) == 2
    assert count_events(events, start, CUTOFF) is None
    assert count_events([("unknown", None)], start, CUTOFF, True) is None
    with pytest.raises(ValueError):
        count_events([("a", start), ("a", CUTOFF)], start, CUTOFF, True)


def test_fhir_narrative_object_is_not_mistaken_for_plain_record_text():
    f = fact()
    f.original = {"text": {"status": "generated"}, "birthDate": "1958-09-18"}
    f.quote = '"1958-09-18"'
    f.available_time = None
    r = result(profile(f))
    assert r.status == "unknown" and r.processing_status == "complete"


@pytest.mark.parametrize(
    ("polarity", "label", "status"),
    [
        ("inclusion", "included", "met"),
        ("inclusion", "not included", "not_met"),
        ("exclusion", "excluded", "met"),
        ("exclusion", "not excluded", "not_met"),
        ("exclusion", "not enough information", "unknown"),
        ("inclusion", "not applicable", "not_applicable"),
    ],
)
def test_annotation_polarity_and_no_answer_leakage(polarity, label, status):
    path = Path(__file__).parents[1] / "scripts/prepare_class6_annotations.py"
    spec = importlib.util.spec_from_file_location("annotation_preparation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    row = dict(
        annotation_id=1,
        patient_id="synthetic",
        note="Synthetic note",
        trial_id="synthetic-trial",
        trial_title="Synthetic",
        criterion_type=polarity,
        criterion_text="Synthetic criterion",
        training=False,
        expert_eligibility=label,
        expert_sentences="[0]",
        gpt4_eligibility="do not include",
        gpt4_explanation="do not include",
    )
    inputs, gold = module.separate(row)
    assert gold["predicate_status"] == status
    assert not any(key.startswith(("expert", "gpt4")) for key in inputs)
    assert inputs["note"] == row["note"] and not inputs["training"]


def test_score_polarity_missing_unverified_and_coverage():
    p = profile(fact())
    passing = result(p)
    failing = result(profile(fact(9.0)))
    exclusion_pass = result(profile(fact(9.0)), criterion("exclusion"))
    rows = [
        passing,
        failing.model_copy(update={"criterion_id": "c2"}),
        exclusion_pass.model_copy(update={"criterion_id": "c3"}),
    ]
    score = eligibility_score(rows, ["c1", "c2", "c3", "missing"])
    assert score.value == 50 and score.coverage_percent == 75
    assert (score.passed, score.failed, score.unresolved) == (2, 1, 1)
    for update in (
        {"verified": False},
        {"processing_status": "failed"},
        {"status": "unknown"},
    ):
        score = eligibility_score([passing.model_copy(update=update)], ["c1"])
        assert score.value == 0 and score.coverage_percent == 0
        assert score.unresolved == 1
    with pytest.raises(ValueError):
        eligibility_score([passing, passing], ["c1"])


def test_score_nonapplicability_and_mandatory_conflict(tmp_path):
    c = criterion()
    c.applicability = Rule(
        kind="condition",
        source_bullet_id="c1",
        criterion_type="inclusion",
        source_quote="scope",
        concept="scope",
    )
    p = profile(fact(), fact(None, concept="scope", assertion="negated"))
    skipped = result(p, c)
    score = eligibility_score([skipped], ["c1"])
    assert score.value is None and score.coverage_percent is None
    assert score.not_applicable == 1 and score.applicable_total == 0
    score = eligibility_score([skipped.model_copy(update={"verified": False})], ["c1"])
    assert score.value == 0 and score.unresolved == 1
    p = profile(fact(9.0))
    passing = result(profile(fact()))
    failing = result(p)
    criteria = [
        criterion().model_copy(update={"criterion_id": f"c{i}"}) for i in range(10)
    ]
    rows = [
        passing.model_copy(
            update={"criterion_id": f"c{i}", "profile_version": p.profile_version}
        )
        for i in range(9)
    ]
    rows.append(failing.model_copy(update={"criterion_id": "c9"}))
    t = trial(c).model_copy(update={"criteria": criteria})
    assert eligibility_score(rows, [c.criterion_id for c in criteria]).value == 90
    assert aggregate(t, p, rows)[0] == "explicit_eligibility_conflict"


def test_report_serializes_and_validates_score(tmp_path):
    from clinical_trial_patient_matching.assessment import ScreeningReport

    state = build_workflow(tmp_path).invoke(
        dict(
            profile=profile(fact()),
            trial=trial(criterion()),
            assessment_time=CUTOFF,
            time_basis="calendar",
        )
    )
    report = state["report"]
    assert report.eligibility_score.value == 100
    reread = ScreeningReport.model_validate_json(report.model_dump_json())
    assert reread.eligibility_score == report.eligibility_score
    stale = report.model_dump()
    stale["eligibility_score"]["value"] = 50
    with pytest.raises(ValidationError):
        ScreeningReport.model_validate(stale)
