"""Two real prompt versions on versioned authored synthetic reference cases."""

import argparse
import json
import os
import time
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field

from clinical_trial_patient_matching.extraction import digest
from clinical_trial_patient_matching.patients import StrictModel
from clinical_trial_patient_matching.review_cli import load_environment
from clinical_trial_patient_matching.review_workflow import PROMPT_VERSION

DATASET_NAME = "class8-hba1c-synthetic-v1"
PROMPTS = Path(__file__).with_name("prompts")
REVIEW_FILE = (
    Path(__file__).resolve().parents[2]
    / "docs/new_syllabus/class8_reference_review.json"
)


class EvaluationAnswer(StrictModel):
    status: str = Field(pattern="^(met|not_met|unknown)$")
    evidence_ids: list[str]
    reason: str


def reference_cases():
    """Explicit authored labels; independent human/clinical review is not claimed."""
    specs = [
        ("inside", ["Patient HbA1c result for this encounter: 8.2%."], "met", ["e1"]),
        (
            "below",
            ["Patient HbA1c result for this encounter: 5.5%."],
            "not_met",
            ["e1"],
        ),
        (
            "above",
            ["Patient HbA1c result for this encounter: 11.3%."],
            "not_met",
            ["e1"],
        ),
        (
            "lower-bound",
            ["Patient HbA1c result for this encounter: 5.6%."],
            "met",
            ["e1"],
        ),
        (
            "upper-bound",
            ["Patient HbA1c result for this encounter: 9.5%."],
            "met",
            ["e1"],
        ),
        ("missing", [], "unknown", []),
        (
            "conflict-v1",
            [
                "Original encounter result: HbA1c 11.3%.",
                "Fictional addendum for the same encounter: HbA1c 8.2%; unres"
                "olved conflict.",
            ],
            "unknown",
            ["e1", "e2"],
        ),
        (
            "family",
            ["The patient's mother has HbA1c 8.2%. No patient result is recorded."],
            "unknown",
            [],
        ),
        (
            "target-only",
            ["HbA1c was not measured. 8.2% is a target, not an observed result."],
            "unknown",
            [],
        ),
        (
            "conflict-resolved-v2",
            [
                "Original encounter result: HbA1c 11.3%; confirmed by fiction"
                "al review.",
                "Fictional 8.2% addendum marked entered-in-error by that review.",
            ],
            "not_met",
            ["e1"],
        ),
        (
            "unsupported-unit",
            ["HbA1c 60 mmol/mol. No reviewed unit conversion is available."],
            "unknown",
            [],
        ),
        (
            "future-availability",
            [
                "HbA1c 8.2% measured 2021-01-12; first available 2021-01-15, "
                "after the assessment cutoff."
            ],
            "unknown",
            [],
        ),
    ]
    cases = []
    for name, texts, status, support in specs:
        input_value = {
            "case_id": name,
            "criterion": (
                "Patient HbA1c must be 5.6–9.5%, inclusive, for this encounter."
            ),
            "assessment_cutoff": "2021-01-13T23:59:59+00:00",
            "evidence": [
                {"id": f"e{i + 1}", "text": text} for i, text in enumerate(texts)
            ],
        }
        cases.append(
            {
                "id": digest([DATASET_NAME, input_value]),
                "input": input_value,
                "expected_output": {"status": status, "evidence_ids": support},
                "metadata": {
                    "case_version": 2 if name.endswith("v2") else 1,
                    "reference_status": "authored_requires_independent_review",
                    "synthetic": True,
                },
            }
        )
    return cases


def run_task(model, prompt, input_value):
    """Only input_value enters the model; expected labels never reach this function."""
    started = time.monotonic()
    response = model.invoke(
        [
            SystemMessage(content=prompt),
            HumanMessage(content=json.dumps(input_value)),
        ]
    )
    raw = response["raw"]
    parsed = response["parsed"]
    return {
        "status": parsed.status if parsed else "unknown",
        "evidence_ids": parsed.evidence_ids if parsed else [],
        "processing_error": type(response["parsing_error"]).__name__
        if response["parsing_error"]
        else None,
        "latency_seconds": time.monotonic() - started,
        "usage": raw.usage_metadata or {},
        "cost_usd": None,
    }


def reference_review(cases):
    if not REVIEW_FILE.exists():
        return {"reference_status": "authored_requires_independent_review"}
    review = json.loads(REVIEW_FILE.read_text())
    reference_hash = digest(
        [
            {"input": case["input"], "expected_output": case["expected_output"]}
            for case in cases
        ]
    )
    if review["reference_hash"] != reference_hash:
        raise ValueError("Reference cases changed; obtain a new review")
    return review


def main():
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run 24 paid Anthropic calls and publish Langfuse experiments",
    )
    parser.add_argument("--output", type=Path, default=Path("data/class8/evaluation"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cases = reference_cases()
    review = reference_review(cases)
    for case in cases:
        case["metadata"].update(review)
    (args.output / "reference-cases.json").write_text(json.dumps(cases, indent=2))
    if not args.live:
        print(
            "Prepared 12 authored synthetic cases. Use --live for two act"
            "ual prompt experiments."
        )
        return
    from langchain_anthropic import ChatAnthropic
    from langfuse import Evaluation, Langfuse

    model_name = os.environ["ANTHROPIC_MODEL"]
    client = Langfuse(timeout=20)
    if not client.auth_check():
        raise RuntimeError("Langfuse authentication failed")
    client.create_dataset(
        name=DATASET_NAME,
        metadata={
            "version": 1,
            **review,
            "engine_prompt_version": PROMPT_VERSION,
        },
    )
    for case in cases:
        client.create_dataset_item(dataset_name=DATASET_NAME, **case)
    dataset = client.get_dataset(DATASET_NAME)
    if len(dataset.items) != len(cases):
        raise ValueError("Dataset membership changed; create a new dataset version")
    model = ChatAnthropic(
        model=model_name, temperature=0, max_tokens=500, max_retries=0, timeout=45
    ).with_structured_output(EvaluationAnswer, include_raw=True)
    summaries = []
    local_results = []

    def label_evaluator(*, output, expected_output, **kwargs):
        return Evaluation(
            name="criterion_correct",
            value=int(
                not output["processing_error"]
                and output["status"] == expected_output["status"]
            ),
            data_type="BOOLEAN",
        )

    def support_evaluator(*, output, expected_output, **kwargs):
        return Evaluation(
            name="evidence_supported",
            value=int(
                not output["processing_error"]
                and set(output["evidence_ids"]) == set(expected_output["evidence_ids"])
            ),
            data_type="BOOLEAN",
        )

    for version in ("v1", "v2"):
        prompt = (PROMPTS / f"review_eval_{version}.txt").read_text()

        def task(*, item, _prompt=prompt, _version=version, **kwargs):
            # Deliberately pass item.input only, never the DatasetItem object.
            output = run_task(model, _prompt, item.input)
            local_results.append(
                {"prompt_version": _version, "case_id": item.input["case_id"], **output}
            )
            return output

        result = dataset.run_experiment(
            name=f"class8-review-{version}",
            task=task,
            evaluators=[label_evaluator, support_evaluator],
            max_concurrency=2,
            metadata={
                "prompt_version": version,
                "prompt_hash": digest(prompt),
                "model": model_name,
                **review,
            },
        )
        summaries.append(
            {
                "prompt_version": version,
                "experiment_url": result.experiment_url,
                "experiment_id": result.experiment_id,
            }
        )
        # Save after each version so a later failure does not erase completed work.
        (args.output / "raw-results.json").write_text(
            json.dumps(local_results, indent=2)
        )
        (args.output / "experiments.json").write_text(json.dumps(summaries, indent=2))
    client.flush()
    expected = {case["input"]["case_id"]: case["expected_output"] for case in cases}
    comparison = []
    for version in ("v1", "v2"):
        rows = [r for r in local_results if r["prompt_version"] == version]
        if len(rows) != len(cases):
            raise RuntimeError(
                "Experiment has failed items; inspect Langfuse before reporting success"
            )
        comparison.append(
            {
                "prompt_version": version,
                "cases": len(rows),
                "label_agreement": sum(
                    not r["processing_error"]
                    and r["status"] == expected[r["case_id"]]["status"]
                    for r in rows
                )
                / len(rows),
                "evidence_support_agreement": sum(
                    not r["processing_error"]
                    and set(r["evidence_ids"])
                    == set(expected[r["case_id"]]["evidence_ids"])
                    for r in rows
                )
                / len(rows),
                "mean_latency_seconds": sum(r["latency_seconds"] for r in rows)
                / len(rows),
                "input_tokens": sum(r["usage"].get("input_tokens", 0) for r in rows),
                "output_tokens": sum(r["usage"].get("output_tokens", 0) for r in rows),
                "cost_usd": None,
                "cost_note": "Token usage measured; monetary pricing not supplied.",
                "report_acceptance": None,
            }
        )
    summary = {
        "dataset": DATASET_NAME,
        **review,
        "comparisons": comparison,
        "experiments": summaries,
    }
    (args.output / "comparison.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
