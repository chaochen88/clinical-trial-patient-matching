"""Three synthetic trials x 20 synthetic patients with real retrieval models.

Labels are authored fixture expectations, not independent clinical judgments.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from clinical_trial_patient_matching.extraction import digest
from clinical_trial_patient_matching.patients import (
    Claim,
    Record,
    build_profile,
    linked_fact,
)
from clinical_trial_patient_matching.retrieval import Models, TrialQuery, run
from clinical_trial_patient_matching.retrieval_evaluation import evaluate
from clinical_trial_patient_matching.search_documents import Judgment, build_document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=Path("data/class5/models.json"))
    parser.add_argument("--output", type=Path, default=Path("data/class5/engineering"))
    args = parser.parse_args()
    fixture = json.loads(Path("tests/fixtures/class5/synthetic_pool.json").read_text())
    assessment = datetime.fromisoformat(fixture["assessment_time"])
    documents = []
    for row in fixture["patients"]:
        record = Record(
            patient_id=row["id"],
            dataset=fixture["label"],
            record_id=row["id"],
            label="synthetic_test_variant",
            text=json.dumps(row),
        )
        facts = []
        for concept, value, unit in (
            (row["condition"], None, None),
            ("age", row["age"], "years"),
            ("BMI", row["bmi"], "kg/m2"),
            ("HbA1c", row["hba1c"], "%"),
        ):
            if value is None and concept in {"BMI", "HbA1c"}:
                continue
            claim = Claim(
                concept=concept,
                value=value,
                unit=unit,
                assertion="affirmed",
                experiencer="patient",
                clinical_time=assessment,
                time_context="synthetic screening encounter",
                quote=json.dumps(row),
            )
            facts.append(linked_fact(record, claim, row))
        documents.append(
            build_document(
                build_profile([], None, {"synthetic": True}, facts), assessment
            )
        )
    config = json.loads(args.models.read_text())
    models = Models(config["encoder_revision"], config["reranker_revision"])
    reports = []
    args.output.mkdir(parents=True, exist_ok=True)
    pool = [d.patient_id for d in documents]
    (args.output / "pool.json").write_text(json.dumps(pool, indent=2))
    judgments = []
    for trial in fixture["trials"]:
        query = TrialQuery(
            trial_id=trial["id"],
            source_hash=digest(trial),
            condition_terms=[trial["condition"]],
            inclusion_quotes=[trial["inclusion"]],
            exclusion_checklist=[],
        )
        rows = [
            Judgment(
                trial_id=trial["id"],
                patient_id=pid,
                label="relevant" if pid in trial["relevant"] else "irrelevant",
                reviewer="authored-synthetic-fixture-not-independent-human-review",
                rationale="Synthetic screening relevance; missing labs retained",
            )
            for pid in pool
        ]
        judgments.extend(rows)
        result = run(
            documents,
            query,
            models,
            args.output / trial["id"],
            args.output / "embeddings",
        )
        reports.append(evaluate(result, rows, pool))
    with (args.output / "judgments.jsonl").open("w") as handle:
        for row in judgments:
            handle.write(row.model_dump_json() + "\n")
    (args.output / "comparison.json").write_text(
        json.dumps(
            {
                "label": fixture["label"],
                "reports": reports,
                "limitation": "Engineering only; no test split or clinical review",
            },
            indent=2,
        )
    )
    lines = [
        "# Synthetic engineering comparison",
        "",
        "Authored synthetic labels; no clinical validation or independent review.",
        "",
        "| Trial | Method | Recall@10 | nDCG@10 | Stage seconds |",
        "|---|---|---:|---:|---:|",
    ]
    for report in reports:
        for method, values in report["comparisons"].items():
            lines.append(
                f"| {report['trial_id']} | {method} | {values['recall@10']:.3f} | "
                f"{values['ndcg@10']:.3f} | {values['stage_seconds']:.3f} |"
            )
    (args.output / "comparison.md").write_text("\n".join(lines) + "\n")
    print(f"Saved three-trial/20-patient engineering comparison to {args.output}")


if __name__ == "__main__":
    main()
