"""Local validation checks only; no patient data or API calls."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "mimic_claude_runner", Path(__file__).parents[1] / "scripts/run_mimic_claude.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_citations_and_complete_criteria():
    criteria = [{"criterion_id": "inclusion-1"}]
    facts = [{"fact_id": "source-1"}]
    result = runner.Assessment(
        criteria=[
            runner.CriterionAssessment(
                criterion_id="inclusion-1",
                status="met",
                evidence_fact_ids=["source-1"],
                rationale="Synthetic evidence",
            )
        ],
        limitations=[],
    )
    assert runner.validate_result(result, criteria, facts) == []
    result.criteria[0].evidence_fact_ids = ["invented"]
    assert runner.validate_result(result, criteria, facts)
    result.criteria[0].evidence_fact_ids = []
    assert runner.validate_result(result, criteria, facts)
    result.criteria[0].status = "unknown"
    assert runner.validate_result(result, criteria, facts) == []
    result.criteria.append(result.criteria[0])
    assert runner.validate_result(result, criteria, facts)
    result.criteria = []
    assert runner.validate_result(result, criteria, facts)
