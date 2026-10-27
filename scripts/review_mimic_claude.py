"""Summarize a saved experiment locally without further API calls."""

import json
from collections import Counter
from pathlib import Path


def table_row(*values):
    cells = [" ".join(str(value).split()).replace("|", "&#124;") for value in values]
    return "| " + " | ".join(cells) + " |"


def patient_review(output, manifest):
    criteria = {item["criterion_id"]: item for item in manifest["criteria"]}
    lines = [
        "# Patient-by-patient experimental review",
        "",
        "Passing validation means the response structure and fact IDs were valid. "
        "It does not mean the patient qualifies. No patient has confirmed eligibility. "
        "The reasons below are Claude's saved judgments, not adjudicated findings.",
        "",
        "Approximate age cannot establish exact age. Billing codes do not establish "
        "current disease or onset; inpatient insulin does not automatically establish "
        "current outpatient insulin use. Missing evidence remains unresolved.",
        "",
        "For inclusion criteria, met means the requirement is satisfied. "
        "For exclusion criteria, met means the exclusion condition is present.",
        "",
        "| Patient | Response validation | Trial eligibility | Saved response |",
        "|---|---|---|---|",
    ]
    for entry in manifest["results"]:
        record = json.loads((output / entry["file"]).read_text())
        valid = record["status"] == "validated_structure_and_citations"
        lines.append(
            table_row(
                record["patient_id"],
                "Passed" if valid else "Failed",
                "Undetermined; human review required",
                f"[Response]({entry['file']})",
            )
        )
    lines.append("")
    for entry in manifest["results"]:
        record = json.loads((output / entry["file"]).read_text())
        valid = record["status"] == "validated_structure_and_citations"
        lines += [
            f"## {record['patient_id']}",
            "",
            f"Response validation: **{'passed' if valid else 'failed'}**. "
            "Trial eligibility: **undetermined; human review required**.",
            "",
            f"[Saved response and attempts]({entry['file']})",
            "",
        ]
        if not valid:
            lines += [
                "No reliable patient pass/fail conclusion is available. "
                "The following errors describe the model response, "
                "not patient ineligibility.",
                "",
                "| Attempt | Validation failure |",
                "|---|---|",
            ]
            for number, attempt in enumerate(record["attempts"], 1):
                errors = attempt.get("validation_errors", [])
                if attempt.get("error"):
                    errors = [*errors, attempt["error"]]
                for error in errors:
                    brief = " ".join(str(error).split())[:650]
                    lines.append(table_row(number, brief))
            lines += [""]
            continue
        assessment = record["attempts"][-1]["assessment"]
        concerns = [
            item
            for item in assessment["criteria"]
            if (
                item["criterion_id"].startswith("inclusion")
                and item["status"] == "not_met"
            )
            or (
                item["criterion_id"].startswith("exclusion") and item["status"] == "met"
            )
        ]
        lines += [
            "**Potential screening barriers reported by Claude (unverified):**",
            "",
            "| Criterion | Reason flagged |",
            "|---|---|",
        ]
        if not concerns:
            lines += [
                "| None flagged | This does not establish eligibility. |",
                "",
            ]
        for item in concerns:
            lines.append(table_row(item["criterion_id"], item["rationale"]))
        lines += [
            "",
            "**All criterion explanations:**",
            "",
            "| Criterion | Trial requirement | Claude status | Explanation "
            "| Evidence fact IDs |",
            "|---|---|---|---|---|",
        ]
        for item in assessment["criteria"]:
            criterion = criteria[item["criterion_id"]]
            text = criterion["text"]
            if isinstance(text, dict):
                text = json.dumps(text)
            evidence = (
                ", ".join(f"`{value}`" for value in item["evidence_fact_ids"])
                or "None cited"
            )
            lines.append(
                table_row(
                    item["criterion_id"],
                    text,
                    item["status"],
                    item["rationale"],
                    evidence,
                )
            )
        lines.append("")
        if assessment["limitations"]:
            lines += [
                "**Claude's limitations:**",
                "",
                "| # | Limitation |",
                "|---|---|",
            ]
            lines += [
                table_row(i, value)
                for i, value in enumerate(assessment["limitations"], 1)
            ]
            lines += [""]
    (output / "patient_summary.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    root = Path("data/mimic/NCT03926806/claude-live")
    output = sorted(root.iterdir())[-1]
    manifest = json.loads((output / "run.json").read_text(encoding="utf-8-sig"))
    patient_review(output, manifest)
    counts = {}
    usage = Counter()
    attempts = 0
    for entry in manifest["results"]:
        path = output / entry["file"]
        record = json.loads(path.read_text())
        for attempt in record["attempts"]:
            attempts += 1
            metadata = attempt.get("raw", {}).get("usage_metadata") or {}
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                usage[key] += metadata.get(key, 0)
        if record["status"] != "validated_structure_and_citations":
            continue
        for item in record["attempts"][-1]["assessment"]["criteria"]:
            counts.setdefault(item["criterion_id"], Counter())[item["status"]] += 1
    summary = {
        "model": manifest["model"],
        "run_status": manifest["status"],
        "patients_saved": len(manifest["results"]),
        "additional_saved_patients": len(manifest.get("additional_results", [])),
        "validated_count": manifest.get("validated_count"),
        "attempt_count": attempts,
        "token_usage": dict(usage),
        "criterion_status_counts": counts,
        "accuracy": "Not measured: no adjudicated criterion-level gold labels",
        "clinical_disposition": "Human review required for all assessments",
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    lines = [
        "# Experimental Claude MIMIC assessment",
        "",
        "[Patient-by-patient reasons and validation failures](patient_summary.md)",
        "",
        f"Model: `{manifest['model']}`. Run status: {manifest['status']}.",
        "",
        f"Saved patients: {len(manifest['results'])}; "
        "validated structure and citations: "
        f"{manifest.get('validated_count', 'pending')}; API attempts: {attempts}.",
        "",
        "All source facts were supplied, with no fact selection or truncation. "
        "Inputs and raw responses are saved alongside each assessment. "
        "Fact IDs resolve to original source rows in the processed profiles.",
        "",
        "Validation checks criterion completeness, schema, citation membership and "
        "the presence of citations for met/not_met conclusions. It does not check "
        "whether evidence clinically entails a conclusion. No gold labels were "
        "available, so extraction or screening accuracy was not measured.",
        "",
        "For exclusions, met means the exclusion condition is present. "
        "All results require human review; no eligibility decisions are approved.",
        "",
        "| Criterion | Met | Not met | Unknown | Requires review |",
        "|---|---:|---:|---:|---:|",
    ]
    for criterion, values in sorted(counts.items()):
        cells = [
            str(values.get(k, 0))
            for k in ("met", "not_met", "unknown", "requires_review")
        ]
        lines.append(f"| {criterion} | " + " | ".join(cells) + " |")
    lines += ["", "Token usage (reported by provider): " + json.dumps(dict(usage)), ""]
    if manifest.get("interruption_note"):
        lines += ["", manifest["interruption_note"]]
    (output / "review.md").write_text("\n".join(lines))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
