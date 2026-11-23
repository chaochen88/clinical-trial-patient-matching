"""Audit a saved MIMIC pipeline run and summarize results without model calls."""

import argparse
import json
from collections import Counter
from pathlib import Path

from run_mimic_pipeline import load_profiles

from clinical_trial_patient_matching.assessment import (
    ScreeningReport,
    validate_citations,
)


def cell(value):
    return " ".join(str(value).split()).replace("|", "&#124;")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    root = args.input
    manifest = json.loads((root / "manifest.json").read_text())
    ranking = json.loads((root / "ranking.json").read_text())
    hospital = json.loads((root / "retrieval-hospital/shortlist.json").read_text())
    combined = json.loads((root / "retrieval-combined/shortlist.json").read_text())
    for result in (hospital, combined):
        if result["query"]["source_hash"] != manifest["trial_version"]:
            raise ValueError("Ranking query uses a different frozen trial")
    source = Path(manifest["source"])
    profiles = load_profiles(source)
    usage, stages, positive_unverified = Counter(), Counter(), 0
    live, baselines = {}, {}
    criteria, failed, citations, attempts = 0, 0, 0, 0
    omitted = 0
    failure_reasons = Counter()
    for entry in manifest["reports"]:
        report = ScreeningReport.model_validate_json(Path(entry["path"]).read_text())
        profile = profiles[report.patient_id]
        if profile.profile_version != report.profile_version:
            raise ValueError("Assessment profile changed")
        if report.trial_version != manifest["trial_version"]:
            raise ValueError("Assessment trial differs from ranking trial")
        for result in report.results:
            validate_citations(result, profile, profile.facts)
        if entry["mode"] == "rules-only":
            baselines[report.patient_id] = report
            continue
        live[report.patient_id] = report
        for result in report.results:
            criteria += 1
            failed += result.processing_status == "failed"
            if result.processing_status == "failed":
                message = " ".join(result.findings)
                category = (
                    "quote_not_exact"
                    if "not an exact source span" in message
                    else "unresolved_conflict"
                    if "conflicting evidence" in message
                    else "unsupported_decisive_result"
                    if "requires supporting citations" in message
                    else "other"
                )
                failure_reasons[category] += 1
            citations += len(result.citations)
            attempts += len(result.attempts)
            stages[result.status] += 1
            positive_unverified += result.status == "met" and not result.verified
            omitted += len(
                (result.evidence_bundle or {}).get("omitted_by_context_limit", [])
            )
    for entry in manifest["calls"]:
        call = json.loads((root / entry["file"]).read_text())
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            usage[key] += (call["raw"].get("usage_metadata") or {}).get(key, 0)
    if set(live) != set(manifest["live_patients"]):
        raise ValueError("Not all selected live patients have a saved report")
    if {row["patient_id"] for row in ranking} != set(profiles):
        raise ValueError("Ranking does not cover the complete source pool")
    if set(baselines) != set(profiles):
        raise ValueError("Deterministic reports do not cover the complete pool")
    if manifest["live_selection_policy"].startswith("All patients"):
        if set(live) != set(profiles):
            raise ValueError("Full-pool live assessment is incomplete")
    if manifest["live_selection_policy"].startswith("Top "):
        top = {
            row["patient_id"]
            for row in sorted(combined["shortlist"], key=lambda row: row["rank"])[
                : manifest["live_patient_limit"]
            ]
        }
        if set(live) != top:
            raise ValueError("Live reports do not match the requested top ranks")
        if any(call["patient_id"] not in top for call in manifest["calls"]):
            raise ValueError("Provider calls include a patient outside the top ranks")
    if len({call["file"] for call in manifest["calls"]}) != len(manifest["calls"]):
        raise ValueError("Provider response files are not unique")
    summary = {
        "status": manifest["status"],
        "patients_ranked": len(ranking),
        "live_patients": len(live),
        "criteria_assessed_in_live_reports": criteria,
        "claude_attempts": attempts,
        "saved_provider_responses": len(manifest["calls"]),
        "narrative_processing_failures": failed,
        "validation_failure_reasons": dict(failure_reasons),
        "validated_citations": citations,
        "criterion_status_counts": dict(stages),
        "unverified_met_decisions": positive_unverified,
        "context_limit_omissions": omitted,
        "token_usage": dict(usage),
        "rank_changes_after_notes": sum(row["rank_change"] != 0 for row in ranking),
        "truncated_search_documents": sum(row["text_truncated"] for row in ranking),
        "live_overall": dict(Counter(r.overall for r in live.values())),
        "clinical_accuracy": None,
        "retrieval_quality_metrics": None,
        "reason_metrics_unavailable": (
            "No adjudicated MIMIC relevance or criterion labels"
        ),
        "model": manifest["model"],
        "source": str(source),
    }
    save_path = root / "summary.json"
    save_path.write_text(json.dumps(summary, indent=2))
    lines = [
        "# MIMIC retrieval, ranking and live Claude assessment",
        "",
        f"Model: `{manifest['model']}`. Run status: **{manifest['status']}**.",
        "",
        f"Ranked **{len(ranking)}** patients using local BM25 + MiniLM "
        "cosine retrieval, "
        "reciprocal-rank fusion, and the pinned cross-encoder reranker. "
        f"Assessed all {len(baselines)} profiles with deterministic rules and "
        f"**{len(live)}** with rules plus live Claude.",
        "",
        "Live selection: " + manifest["live_selection_policy"] + ". "
        "The full patient ranking is retained. This is an integration run, "
        "not a representative evaluation sample.",
        "",
        "Patient-specific discharge cutoffs and frozen trial/profile versions were "
        "preserved. Later-signed notes were excluded. "
        "No eligibility decision is approved.",
        "",
        "## Retrieval and ranking",
        "",
        f"Search documents truncated: {summary['truncated_search_documents']}/"
        f"{len(ranking)}. Patients whose rank changed after adding notes: "
        f"{summary['rank_changes_after_notes']}.",
        "The encoder sees only its bounded prefix, and this pipeline also uses that "
        "bounded text for BM25 and reranking. Full-note evidence may therefore affect "
        "assessment without affecting ranking. Full facts remain in the assessment "
        "profiles. Token truncation is an observed limitation, "
        "not proof notes are irrelevant.",
        "",
        "Reranker scores measure relevance, not eligibility probabilities. "
        "Recall/nDCG and clinical accuracy remain unmeasured without MIMIC judgments.",
        "",
        "| Patient | Hospital rank | With-notes rank | Change "
        "| Reranker score | Note facts | Live |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in ranking:
        values = [
            row["patient_id"],
            row["hospital_rank"],
            row["combined_rank"],
            row["rank_change"],
            f"{row['reranker_score']:.4f}",
            row["note_facts"],
            "Yes" if row["live_assessed"] else "No",
        ]
        lines.append("| " + " | ".join(cell(v) for v in values) + " |")
    lines += [
        "",
        "## Live assessment",
        "",
        f"Saved provider responses: {len(manifest['calls'])}. "
        f"Narrative processing failures: {failed}. Validated citations: {citations}.",
        "",
        "Rejected result categories: " + json.dumps(dict(failure_reasons)) + ".",
        "",
        f"Evidence omitted by criterion-context limits: {omitted} occurrences "
        "(counted separately for each criterion, not unique facts). Full profiles "
        "retain this evidence, but Claude did not see every fact in every request. "
        "Unknown results do not establish that the underlying records lack evidence.",
        "",
        "All Claude interpretations are unverified and require human review. "
        "Their met/not_met statuses are not promoted to verified evidence scores; "
        "a live interpretation can change a criterion explanation while the verified "
        "score stays unchanged. Unknown does not mean a clinical failure.",
        "",
        "| Patient | Verified score | Overall | Met / not met / unknown "
        "| Failed processing | Report |",
        "|---|---:|---|---|---:|---|",
    ]
    live_entries = sorted(
        (e for e in manifest["reports"] if e["mode"] == "rules-plus-claude"),
        key=lambda e: (-(live[e["case"]].eligibility_score.value or 0), e["case"]),
    )
    for entry in live_entries:
        r = live[entry["case"]]
        counts = Counter(result.status for result in r.results)
        values = [
            r.patient_id,
            f"{r.eligibility_score.value}/100",
            r.overall,
            f"{counts['met']} / {counts['not_met']} / {counts['unknown']}",
            sum(v.processing_status == "failed" for v in r.results),
            f"[Details]({entry['markdown']})",
        ]
        lines.append("| " + " | ".join(cell(v) for v in values) + " |")
    lines += [
        "",
        "Token usage (reported by the provider): " + json.dumps(dict(usage)),
        "",
        "Raw model responses, feedback and input contexts are under `calls/`; "
        "structured reports, exact citations and source references "
        "are under `reports/`.",
        "",
    ]
    (root / "review.md").write_text("\n".join(lines))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
