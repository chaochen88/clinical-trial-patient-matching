"""Compare saved reports against explicit, separately stored engineering judgments."""

import argparse
import json
from pathlib import Path

from clinical_trial_patient_matching.assessment import (
    ScreeningReport,
    validate_citations,
)
from clinical_trial_patient_matching.extraction import digest
from clinical_trial_patient_matching.patients import Profile


def classification_metrics(pairs):
    scores = {}
    for label in ("met", "not_met", "unknown", "not_applicable"):
        tp = sum(gold == label and pred == label for gold, pred in pairs)
        fp = sum(gold != label and pred == label for gold, pred in pairs)
        fn = sum(gold == label and pred != label for gold, pred in pairs)
        scores[label] = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
    present = [value for value in scores.values() if value is not None]
    return {
        "macro_f1": sum(present) / len(present) if present else None,
        "per_label_f1": scores,
        "evaluated_pairs": len(pairs),
        "incorrect_met": sum(gold != "met" and pred == "met" for gold, pred in pairs),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/class6"))
    parser.add_argument(
        "--judgments",
        type=Path,
        default=Path("tests/fixtures/class6/expected_judgments.json"),
    )
    args = parser.parse_args()
    manifest = json.loads((args.input / "manifest.json").read_text())
    gold = json.loads(args.judgments.read_text())
    if gold["trial_versions"] != manifest["trial_versions"]:
        raise ValueError("Judgments belong to different trial snapshots")
    identities = [(row["case"], row["criterion_id"]) for row in gold["criteria"]]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate judgments")
    modes, comparisons = {}, []
    for entry in manifest["reports"]:
        report = ScreeningReport.model_validate_json(Path(entry["path"]).read_text())
        profile = Profile.model_validate_json(
            (args.input / f"{entry['case']}-profile.json").read_text()
        )
        if (report.profile_version, report.patient_id, report.variant) != (
            profile.profile_version,
            profile.patient_id,
            profile.variant,
        ):
            raise ValueError("Report/profile identity mismatch")
        results = {r.criterion_id: r for r in report.results}
        mode = modes.setdefault(
            entry["mode"],
            {
                "pairs": [],
                "valid_evidence": 0,
                "cited_results": 0,
                "aggregate_errors": 0,
                "aggregate_cases": 0,
                "processing_failures": 0,
            },
        )
        for row in gold["criteria"]:
            if row["case"] == entry["case"]:
                if row["criterion_id"] not in results:
                    raise ValueError("Required judged result missing")
                mode["pairs"].append(
                    (row["status"], results[row["criterion_id"]].status)
                )
        for result in report.results:
            mode["processing_failures"] += result.processing_status == "failed"
            if result.citations:
                mode["cited_results"] += 1
                try:
                    validate_citations(result, profile, profile.facts)
                except ValueError:
                    pass
                else:
                    mode["valid_evidence"] += 1
        expected = gold["overall"].get(entry["case"])
        if expected:
            mode["aggregate_cases"] += 1
            mode["aggregate_errors"] += report.overall != expected
        comparisons.append(
            dict(
                case=entry["case"],
                mode=entry["mode"],
                expected_overall=expected,
                actual_overall=report.overall,
                report_key=report.report_key,
                eligibility_score=report.eligibility_score.model_dump(),
            )
        )
    results = {}
    for name, mode in modes.items():
        pairs = mode.pop("pairs")
        results[name] = {
            **classification_metrics(pairs),
            **mode,
            "evidence_validity": mode["valid_evidence"] / mode["cited_results"]
            if mode["cited_results"]
            else None,
        }
    evaluation = {
        "review_status": gold["review_status"],
        "notes": gold["notes"],
        "judgments_hash": digest(gold),
        "trial_versions": manifest["trial_versions"],
        "engineering_metrics": results,
        "clinical_accuracy": None,
        "clinical_accuracy_reason": "Independent adjudication has not been completed",
        "cases": comparisons,
    }
    (args.input / "evaluation.json").write_text(json.dumps(evaluation, indent=2))
    lines = [
        "# Class 6 engineering comparison",
        "",
        gold["notes"],
        "",
        "Clinical accuracy is not measured. Metrics below are checks against "
        "author-checked teaching expectations, not independent clinical labels.",
        "",
        "| Mode | Criterion macro-F1 | Incorrect met | Valid citations "
        "| Aggregate errors |",
        "|---|---:|---:|---:|---:|",
    ]
    for mode, values in results.items():
        lines.append(
            f"| {mode} | {values['macro_f1']:.4f} | "
            f"{values['incorrect_met']} | {values['evidence_validity']} | "
            f"{values['aggregate_errors']}/{values['aggregate_cases']} |"
        )
    lines += [
        "",
        "| Case | Mode | Overall screening result | Evidence score | Report |",
        "|---|---|---|---|---|",
    ]
    for entry in manifest["reports"]:
        name = f"{entry['case']}-{entry['mode']}.md"
        score = next(
            c["eligibility_score"]
            for c in comparisons
            if c["report_key"] == entry["report_key"]
        )
        display = f"{score['value']}/100" if score["value"] is not None else "N/A"
        lines.append(
            f"| {entry['case']} | {entry['mode']} | {entry['overall']} | "
            f"{display} | [Details]({name}) |"
        )
    (args.input / "comparison.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
