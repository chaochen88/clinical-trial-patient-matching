"""Synthetic scope, tool, budget, MCP and graph failure cases; no paid calls."""

import asyncio
from datetime import datetime

import pytest
from langchain_core.messages import AIMessage

from clinical_trial_patient_matching.assessment import Criterion, Trial
from clinical_trial_patient_matching.criteria import Rule
from clinical_trial_patient_matching.evidence_agent import (
    RunBudget,
    ToolExecutor,
    build_investigation,
    fixture_planner,
    initial_state,
    merge_checks,
)
from clinical_trial_patient_matching.extraction import digest
from clinical_trial_patient_matching.patient_mcp_client import MCPTransport
from clinical_trial_patient_matching.patient_tools import (
    EvidenceBackend,
    ToolResult,
    create_snapshot,
)
from clinical_trial_patient_matching.patients import Claim, Profile, Record, linked_fact

CUTOFF = datetime.fromisoformat("2021-01-13T23:59:59+00:00")


@pytest.fixture
def evidence(tmp_path):
    text = "BMI 29.38 kg/m2"
    record = Record(
        patient_id="synthetic",
        record_id="observation-1",
        dataset="synthetic",
        text=text,
        available_time=CUTOFF,
    )
    claim = Claim(
        concept="BMI",
        concept_system="http://loinc.org",
        concept_code="39156-5",
        value=29.38,
        unit="kg/m2",
        assertion="affirmed",
        experiencer="patient",
        clinical_time=CUTOFF,
        time_context="dated observation",
        quote=text,
    )
    fact = linked_fact(record, claim, {"text": text}, "final")
    profile = Profile(
        patient_id="synthetic",
        variant="original",
        profile_version="v1",
        configuration={},
        facts=[fact],
        conflicts=[],
        findings=[],
        attempts=[],
        status="validated",
    )
    rule = Rule(
        kind="range",
        source_bullet_id="bmi",
        criterion_type="inclusion",
        source_quote="BMI 27-36",
        concept="BMI",
        lower=27,
        upper=36,
        lower_inclusive=True,
        upper_inclusive=True,
        unit="kg/m2",
    )
    trial = Trial(
        trial_id="NCT03926806",
        trial_version="t1",
        source_reference="fixture",
        criteria=[
            Criterion(
                criterion_id="bmi",
                criterion_type="inclusion",
                predicate="BMI 27-36",
                rule=rule,
                reviewed_mapping=True,
            )
        ],
    )
    config = create_snapshot(tmp_path / "evidence.sqlite", [profile], [trial], CUTOFF)
    return profile, trial, EvidenceBackend(config), config


def invoke(profile, trial, backend, **kwargs):
    executor = ToolExecutor(backend)
    graph = build_investigation(executor, **kwargs)
    result = asyncio.run(
        graph.ainvoke(
            initial_state(profile, trial, CUTOFF),
            {"max_concurrency": 4, "recursion_limit": 64},
        )
    )
    return result, executor


def test_missing_bmi_recovers_and_sufficient_evidence_skips_planner(evidence):
    profile, trial, backend, _ = evidence
    initial = profile.model_copy(update={"facts": [], "profile_version": "missing"})
    fixed, _ = invoke(initial, trial, backend, fixed=True)
    assert fixed["report"].results[0].status == "unknown"
    result, executor = invoke(initial, trial, backend)
    assert result["report"].results[0].status == "met"
    assert executor.backend_reads == result["attempts"] == 1
    assert result["events"][0]["rows"][0]["record_id"] == "observation-1"
    assert result["messages"][-1].tool_call_id == "lookup-bmi"
    sufficient, _ = invoke(profile, trial, backend)
    assert sufficient["model_calls"] == sufficient["attempts"] == 0


def test_backend_scope_cutoff_codes_and_parameterized_queries(evidence):
    profile, _, backend, _ = evidence
    args = {
        "patient_id": profile.patient_id,
        "loinc_code": "39156-5",
        "as_of": CUTOFF.isoformat(),
    }
    assert backend.call("get_patient_observations", args).status == "ok"
    for changed in (
        {"patient_id": "outsider"},
        {"loinc_code": "39156-5 OR 1=1"},
        {"as_of": "2022-01-01T00:00:00Z"},
        {"extra": True},
    ):
        result = backend.call("get_patient_observations", {**args, **changed})
        assert result.status == "error" and not result.rows
    assert (
        backend.call("search_patients", {"query": "' OR 1=1 --"}).status == "not_found"
    )
    assert backend.call("get_trial", {"trial_id": "NCT00000000"}).status == "error"


def test_cache_and_run_budget_are_shared_and_source_bound(evidence):
    p, t, backend, _ = evidence
    executor = ToolExecutor(backend)
    args = {
        "patient_id": p.patient_id,
        "loinc_code": "39156-5",
        "as_of": CUTOFF.isoformat(),
    }
    assert executor.execute("get_patient_observations", args)[1] is False
    assert executor.execute("get_patient_observations", args)[1] is True
    assert executor.backend_reads == 1
    missing = p.model_copy(update={"facts": []})
    result, executor = invoke(missing, t, backend, budget=RunBudget(0))
    assert result["report"].results[0].status == "unknown"
    assert not executor.backend_reads


def test_multicall_batch_reserved_before_any_backend_dispatch(evidence):
    p, t, backend, _ = evidence

    def too_many(context, messages):
        proposal = fixture_planner(context, messages)
        proposal["calls"] = [{**proposal["calls"][0], "id": str(i)} for i in range(5)]
        proposal["message"] = AIMessage(content="", tool_calls=proposal["calls"])
        return proposal

    result, executor = invoke(
        p.model_copy(update={"facts": []}), t, backend, planner=too_many
    )
    assert result["proposed_calls"] == 5
    assert (
        result["attempts"] == 0
    )  # Whole over-budget batch is rejected before dispatch.
    assert executor.backend_reads == 0
    assert len(result["events"]) == 5
    assert all(e["status"] == "error" for e in result["events"])


def test_absent_and_truncated_data_remain_unknown(evidence):
    p, t, backend, _ = evidence

    class Incomplete:
        def call(self, name, args):
            return ToolResult(
                status="truncated",
                rows=[p.facts[0].model_dump(mode="json")],
                source_version=backend.source_version,
                coverage={"complete": False},
            )

    graph = build_investigation(ToolExecutor(backend, Incomplete()))
    result = asyncio.run(
        graph.ainvoke(initial_state(p.model_copy(update={"facts": []}), t, CUTOFF))
    )
    assert result["report"].results[0].status == "unknown"
    assert "dated BMI" in result["report"].results[0].follow_up
    assert result["stopped"] == "no_new_usable_evidence"


def test_parallel_reducer_rejects_identity_collision_and_timeout_is_visible(
    evidence,
    monkeypatch,
):
    import clinical_trial_patient_matching.evidence_agent as agent

    assert merge_checks({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    with pytest.raises(ValueError):
        merge_checks({"a": 1}, {"a": 2})

    def timeout(*args):
        raise TimeoutError("fixture branch timeout")

    monkeypatch.setattr(agent, "assess", timeout)
    p, t, backend, _ = evidence
    result, _ = invoke(p, t, backend, fixed=True)
    r = result["report"].results[0]
    assert r.processing_status == "failed" and r.status == "unknown"
    assert "TimeoutError" in r.findings[0]


def test_real_stdio_mcp_discovery_and_equivalent_errors(evidence):
    p, _, backend, config = evidence
    args = {
        "patient_id": p.patient_id,
        "loinc_code": "39156-5",
        "as_of": CUTOFF.isoformat(),
    }
    with MCPTransport(config) as mcp:
        assert set(mcp.tools) == {
            "get_trial",
            "search_patients",
            "get_patient_record",
            "get_patient_observations",
        }
        for arguments in (
            args,
            {**args, "patient_id": "outside"},
            {**args, "loinc_code": "99999-9"},
        ):
            direct = backend.call("get_patient_observations", arguments)
            remote = mcp.call("get_patient_observations", arguments)
            assert direct.model_dump() == remote.model_dump()


def test_changed_database_fails_closed(evidence):
    _, _, backend, config = evidence
    with backend.database.open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(ValueError, match="changed"):
        EvidenceBackend(config)


def test_conflicting_lookup_repeated_requests_and_real_truncation(evidence, tmp_path):
    p, t, _, _ = evidence
    original = p.facts[0]
    other = original.model_copy(
        update={
            "fact_id": digest("second"),
            "record_id": "second",
            "value": 42.0,
            "quote": "BMI 42 kg/m2",
            "original": {"text": "BMI 42 kg/m2"},
            "source_hash": digest("BMI 42 kg/m2"),
        }
    )
    config = create_snapshot(
        tmp_path / "conflict.sqlite",
        [p.model_copy(update={"facts": [original, other]})],
        [t],
        CUTOFF,
    )
    backend = EvidenceBackend(config)
    result, executor = invoke(p.model_copy(update={"facts": []}), t, backend)
    assert result["report"].results[0].evidence_state == "conflicting"
    assert result["report"].results[0].status == "unknown"
    assert executor.backend_reads == 1
    assert result["attempts"] == 2
    assert result["stopped"] == "rejected_or_repeated_call"
    truncated = backend.call(
        "get_patient_observations",
        {
            "patient_id": p.patient_id,
            "loinc_code": "39156-5",
            "as_of": CUTOFF.isoformat(),
            "limit": 1,
        },
    )
    assert truncated.status == "truncated" and not truncated.coverage["complete"]


def test_absent_and_unauthorized_graph_scope(evidence, tmp_path):
    p, t, backend, _ = evidence
    missing = p.model_copy(update={"facts": []})
    config = create_snapshot(tmp_path / "absent.sqlite", [missing], [t], CUTOFF)
    result, executor = invoke(missing, t, EvidenceBackend(config))
    assert result["events"][0]["status"] == "not_found"
    assert result["report"].results[0].status == "unknown"
    assert result["attempts"] == executor.backend_reads == 1
    with pytest.raises(ValueError, match="scope"):
        invoke(p.model_copy(update={"patient_id": "outside"}), t, backend)
