"""Compare fixed, direct-agent and MCP-agent workflows on frozen public data."""

import argparse
import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import messages_to_dict

from clinical_trial_patient_matching.assessment import Trial
from clinical_trial_patient_matching.evidence_agent import (
    RunBudget,
    ToolExecutor,
    anthropic_planner,
    build_investigation,
    fixture_planner,
    initial_state,
)
from clinical_trial_patient_matching.extraction import digest
from clinical_trial_patient_matching.patient_mcp_client import MCPTransport
from clinical_trial_patient_matching.patient_tools import (
    EvidenceBackend,
    create_snapshot,
)
from clinical_trial_patient_matching.patients import Profile, reconcile

CUTOFF = datetime.fromisoformat("2021-01-13T23:59:59-05:00")


def save(path, value):
    path.write_text(json.dumps(value, indent=2, default=str))


def run_case(initial, trial, config, transport, output, planner, fixed, budget):
    executor = ToolExecutor(EvidenceBackend(config), transport)
    graph = build_investigation(executor, planner, budget, fixed=fixed)
    started = time.perf_counter()
    state = asyncio.run(
        graph.ainvoke(
            initial_state(initial, trial, CUTOFF),
            {"max_concurrency": 4, "recursion_limit": 64},
        )
    )
    report = state["report"]
    save(
        output.with_suffix(".json"),
        {
            "report": report.model_dump(mode="json"),
            "events": state["events"],
            "stop_reason": state["stopped"],
            "messages": messages_to_dict(state["messages"]),
            "tool_attempts": state["attempts"],
            "backend_reads": executor.backend_reads,
            "model_calls": state["model_calls"],
            "usage": state["usage"],
            "rounds": state["round"],
            "latency_seconds": time.perf_counter() - started,
            "profile": state["profile"].model_dump(mode="json"),
        },
    )
    bmi = next(r for r in report.results if r.criterion_id == "inclusion-1")
    return {
        "mode": output.name,
        "BMI": bmi.status,
        "tool_attempts": state["attempts"],
        "backend_reads": executor.backend_reads,
        "model_calls": state["model_calls"],
        "stop_reason": state["stopped"],
        "latency_seconds": round(time.perf_counter() - started, 3),
        "input_tokens": sum(u.get("input_tokens", 0) for u in state["usage"]),
        "output_tokens": sum(u.get("output_tokens", 0) for u in state["usage"]),
        "cost_usd": 0 if planner is fixture_planner else None,
        "cost_basis": "offline fixture"
        if planner is fixture_planner
        else "Not estimated; provider invoice is authoritative",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/class7"))
    parser.add_argument(
        "--live",
        action="store_true",
        help="Small live planner test using the public synthetic patient",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    profile = Profile.model_validate_json(
        Path("data/class6/synthea-shared-profile.json").read_text()
    )
    trial = Trial.model_validate_json(
        Path("data/class6/NCT03926806-trial.json").read_text()
    )
    missing = profile.model_copy(
        update={
            "facts": [f for f in profile.facts if f.concept != "BMI"],
            "profile_version": digest(
                [profile.profile_version, "BMI deliberately withheld"]
            ),
        }
    )
    rows, equivalence, live_rows = [], [], []
    budget = RunBudget(40)
    load_dotenv()
    model = os.environ.get("ANTHROPIC_MODEL")
    if args.live and (not model or not os.environ.get("ANTHROPIC_API_KEY")):
        parser.error("Configure ANTHROPIC_MODEL and ANTHROPIC_API_KEY")
    for name in ("sufficient", "recoverable", "absent", "conflicting"):
        source = missing if name == "absent" else profile
        if name == "conflicting":
            latest = max(
                (f for f in profile.facts if f.concept == "BMI"),
                key=lambda f: f.clinical_time,
            )
            # Clearly labelled counterfactual fixture; never alter original data.
            other = latest.model_copy(
                update={
                    "value": 42.0,
                    "fact_id": digest([latest.fact_id, "fictional-conflict"]),
                    "record_id": "fictional-conflict",
                    "quote": "Fictional BMI 42 kg/m2",
                    "original": {"text": "Fictional BMI 42 kg/m2"},
                    "source_hash": digest("Fictional BMI 42 kg/m2"),
                }
            )
            facts, conflicts = reconcile(profile.facts + [other])
            source = profile.model_copy(
                update={
                    "facts": facts,
                    "conflicts": conflicts,
                    "profile_version": digest("conflict"),
                }
            )
        root = args.output / name
        root.mkdir()
        config = create_snapshot(root / "evidence.sqlite", [source], [trial], CUTOFF)
        backend = EvidenceBackend(config)
        initial = profile if name == "sufficient" else missing
        rows.append(
            run_case(
                initial,
                trial,
                config,
                backend,
                root / "fixed",
                fixture_planner,
                True,
                budget,
            )
        )
        rows.append(
            run_case(
                initial,
                trial,
                config,
                backend,
                root / "direct",
                fixture_planner,
                False,
                budget,
            )
        )
        with MCPTransport(config) as mcp:
            arguments = {
                "patient_id": profile.patient_id,
                "loinc_code": "39156-5",
                "as_of": CUTOFF.isoformat(),
            }
            direct = backend.call("get_patient_observations", arguments)
            remote = mcp.call("get_patient_observations", arguments)
            same = direct.model_dump(mode="json") == remote.model_dump(mode="json")
            if not same:
                raise ValueError(f"Direct/MCP evidence differs in {name}")
            equivalence.append(
                {
                    "case": name,
                    "equal": same,
                    "discovered": mcp.tools,
                    "status": remote.status,
                    "source_version": remote.source_version,
                }
            )
            rows.append(
                run_case(
                    initial,
                    trial,
                    config,
                    mcp,
                    root / "mcp",
                    fixture_planner,
                    False,
                    budget,
                )
            )
        if args.live and name in {"sufficient", "recoverable", "absent"}:
            live_rows.append(
                run_case(
                    initial,
                    trial,
                    config,
                    backend,
                    root / "claude",
                    anthropic_planner(model),
                    False,
                    budget,
                )
            )
        print(f"Completed Class 7 case: {name}", flush=True)
    save(
        args.output / "summary.json",
        {
            "offline": rows,
            "live": live_rows,
            "direct_mcp_equivalence": equivalence,
            "run_tool_budget_used": budget.used,
            "live_model": model if args.live else None,
            "limitations": "Public synthetic demo; not clinical accuracy. "
            "Conflict is fictional.",
        },
    )
    lines = [
        "# Class 7 evidence investigation comparison",
        "",
        "Frozen public Synthea patient and NCT03926806. Original sources were "
        "not changed. Missing-BMI cases deliberately remove it from the initial "
        "profile. The conflicting case contains a labelled fictional BMI.",
        "",
        "Offline planner rows are deterministic test fixtures, not live agent claims.",
        "",
        "| Case / mode | BMI | Tool attempts | Backend reads | Planner calls "
        "| Seconds | Input / output tokens | Stop |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    for case in ("sufficient", "recoverable", "absent", "conflicting"):
        for mode in ("fixed", "direct", "mcp", "claude"):
            path = args.output / case / f"{mode}.json"
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            bmi = next(
                r
                for r in data["report"]["results"]
                if r["criterion_id"] == "inclusion-1"
            )
            usage = data["usage"]
            lines.append(
                f"| {case} / {mode} | {bmi['status']} | "
                f"{data['tool_attempts']} | {data['backend_reads']} | "
                f"{data['model_calls']} | {data['latency_seconds']:.3f} | "
                f"{sum(u.get('input_tokens', 0) for u in usage)} / "
                f"{sum(u.get('output_tokens', 0) for u in usage)} | "
                f"{data['stop_reason']} |"
            )
    lines += [
        "",
        "Direct/MCP evidence equivalence: all four cases passed. "
        "MCP latency includes process startup. Results retain source IDs, "
        "dates, units and coverage; errors and truncation are not promoted "
        "to sufficient evidence.",
        "",
        "Offline token cost is zero. Live token "
        "usage is recorded; no dollar estimate is asserted without verified "
        "pricing. Overall trial screening still requires the other criteria "
        "and human review.",
        "",
    ]
    (args.output / "comparison.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
