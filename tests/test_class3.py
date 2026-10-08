import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from clinical_trial_patient_matching.criteria import (
    Extraction,
    Rule,
    Window,
    combine,
    evaluate_numeric,
    in_window,
    months_before,
    screening_result,
    validate_source,
)
from clinical_trial_patient_matching.extraction import (
    build_workflow,
    bullets,
    offline_extract,
    run_snapshot,
    write_review,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests/fixtures/class3_trial.json"


@pytest.fixture
def source_bullets():
    return bullets(json.loads(SOURCE.read_text(encoding="utf-8"))["criteria"])


@pytest.fixture
def hba1c(source_bullets):
    return Rule.model_validate(offline_extract(source_bullets[2])["rules"][0])


@pytest.mark.parametrize(
    "value,expected",
    [
        (5.5, "not_met"),
        (5.6, "met"),
        (9.5, "met"),
        (9.6, "not_met"),
        (None, "unknown"),
    ],
)
def test_range_boundaries(hba1c, value, expected):
    assert evaluate_numeric(hba1c, value, "%") == expected
    assert hba1c.window is None


def test_incompatible_units(hba1c):
    assert evaluate_numeric(hba1c, 40, "mmol/mol") == "requires_review"


def test_invalid_range_and_extra_fields(hba1c):
    with pytest.raises(ValidationError):
        Rule.model_validate({**hba1c.model_dump(), "lower": 10})
    with pytest.raises(ValidationError):
        Rule.model_validate({**hba1c.model_dump(), "invented": True})
    with pytest.raises(ValidationError):
        Rule.model_validate({**hba1c.model_dump(), "lower": float("nan")})


def test_quotes_and_polarity(hba1c, source_bullets):
    bullet = source_bullets[2]
    validate_source(hba1c, "inclusion-3", "inclusion", bullet["text"])
    for change in (
        {"source_quote": "invented"},
        {"criterion_type": "exclusion"},
        {"source_bullet_id": "inclusion-4"},
    ):
        with pytest.raises(ValueError):
            validate_source(
                hba1c.model_copy(update=change),
                "inclusion-3",
                "inclusion",
                bullet["text"],
            )


def test_temporal_logic_and_duration(source_bullets):
    duration = Rule.model_validate(offline_extract(source_bullets[1])["rules"][0])
    assert duration.kind == "duration" and duration.uninterrupted
    assert duration.operator == "ge" and duration.value == 3
    assert duration.window is None
    group = Rule.model_validate(offline_extract(source_bullets[5])["rules"][0])
    assert group.logic == "any"
    assert group.criterion_type == "exclusion"
    assert all(child.window.months == 6 for child in group.children)
    assert all(child.source_bullet_id == "exclusion-2" for child in group.children)
    assert screening_result(combine([True, None], "any"), "exclusion") == "not_met"
    assert screening_result(combine([False, None], "any"), "exclusion") == "unknown"
    assert screening_result(combine([False, False], "any"), "exclusion") == "met"
    assert combine([False, None], "all") is False
    assert combine([True, None], "all") is None


def test_calendar_boundaries():
    screening = date(2026, 8, 31)
    assert months_before(screening, 6) == date(2026, 2, 28)
    window = Window(months=6, boundary="inclusive")
    assert in_window(date(2026, 2, 28), screening, window) is True
    assert in_window(date(2026, 2, 27), screening, window) is False
    assert in_window(screening, screening, window) is True
    assert in_window(date(2026, 9, 1), screening, window) is False
    assert in_window(None, screening, window) is None
    assert in_window(screening, screening, Window(months=6)) is None


def test_one_repair_and_retained_failure(source_bullets):
    calls = []

    def broken(bullet, feedback):
        calls.append(feedback)
        return {"rules": [], "review_findings": []}

    result = build_workflow(broken).invoke({"bullet": source_bullets[2]})
    assert len(calls) == 2 and calls[1]
    assert result["extraction"] is None and result["findings"]


def test_repair_success(source_bullets):
    def extractor(bullet, feedback):
        if not feedback:
            raise ValueError("truncated response")
        return offline_extract(bullet)

    result = build_workflow(extractor).invoke({"bullet": source_bullets[2]})
    assert len(result["attempts"]) == 2
    assert result["findings"] == [] and result["extraction"]


@pytest.mark.parametrize(
    "response",
    [
        {"parsed": None, "provider_error": "refusal", "raw": {}},
        {"parsed": None, "provider_error": "truncated provider response", "raw": {}},
    ],
)
def test_provider_failures_retained(source_bullets, response):
    result = build_workflow(lambda bullet, feedback: response).invoke(
        {"bullet": source_bullets[2]}
    )
    assert len(result["attempts"]) == 2
    assert result["extraction"] is None
    assert response["provider_error"] in result["findings"][0]


def test_coverage_provenance_and_cache(tmp_path, source_bullets):
    result = run_snapshot(
        SOURCE, tmp_path, lambda b, f: offline_extract(b), "offline", "test-model"
    )
    assert len(result["records"]) == len(source_bullets) == 11
    assert len(result["source_hash"]) == 64
    assert result["source_version"] and result["timestamp"]
    for record in result["records"]:
        assert record["rules"] or record["status"] == "requires_review"
        for rule in record["rules"]:
            validate_source(
                Rule.model_validate(rule),
                record["source_bullet_id"],
                record["criterion_type"],
                record["text"],
            )
    cached = run_snapshot(
        SOURCE,
        tmp_path,
        lambda b, f: pytest.fail("cache missed"),
        "offline",
        "test-model",
    )
    assert result == cached
    changed = run_snapshot(
        SOURCE, tmp_path, lambda b, f: offline_extract(b), "offline", "different-model"
    )
    assert changed["cache_key"] != result["cache_key"]


def test_unassigned_and_continuation():
    records = bullets(
        "Unassigned requirement\nInclusion Criteria:\n* first\n  continued"
    )
    assert records[0]["criterion_type"] is None
    assert records[1]["text"] == "first\n  continued"


def test_empty_requires_review():
    parsed = Extraction(rules=[], review_findings=["Unsupported wording"])
    assert parsed.review_findings


def test_review_table_handles_multiline_errors(tmp_path):
    write_review(
        [
            {
                "nct_id": "TEST",
                "synthetic": True,
                "records": [
                    {
                        "source_bullet_id": "inclusion-1",
                        "status": "requires_review",
                        "validation_findings": ["Invalid rule\nDetails | here"],
                    }
                ],
            }
        ],
        tmp_path,
    )
    assert "Invalid rule Details / here" in (tmp_path / "review.md").read_text()
