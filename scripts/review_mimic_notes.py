"""Audit merged note provenance and write patient evidence tables locally."""

import argparse
import json
from collections import Counter
from pathlib import Path

from clinical_trial_patient_matching.mimic_adapter import shifted_time
from clinical_trial_patient_matching.mimic_notes import availability
from clinical_trial_patient_matching.patients import Profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads((args.input / "enrichment_report.json").read_text())
    if report["status"] != "completed":
        raise ValueError("Cannot audit an incomplete combined export")
    hospital = Path(report["hospital_profiles"])
    total, alignment, rejected = 0, Counter(), 0
    lines = [
        "# Patient note evidence",
        "",
        "These are extracted candidate facts, not eligibility decisions. Source "
        "spans and timestamps were checked; clinical entailment still requires review. "
        "Clinical event time remains unknown unless independently verified. "
        "Note timestamp does not establish onset or measurement date.",
        "",
    ]
    for row in report["patient_results"]:
        pid = row["patient_id"].split(":", 1)[1]
        combined = Profile.model_validate_json(
            (args.input / f"profiles/{pid}.json").read_text()
        )
        original = Profile.model_validate_json(
            (hospital / f"profiles/{pid}.json").read_text()
        )
        lookup = {f.fact_id: f for f in combined.facts}
        if len(lookup) != len(combined.facts):
            raise ValueError("Duplicate combined fact ID")
        if any(lookup.get(f.fact_id) != f for f in original.facts):
            raise ValueError("Hospital fact was removed or modified")
        cutoff = shifted_time(
            combined.configuration["assessment_time"].replace("+00:00", "")
        )
        added = [
            f for f in combined.facts if f.original.get("source_format") == "mimic_note"
        ]
        for fact in added:
            source = fact.original
            metadata = source["note_metadata"]
            if fact.patient_id != f"mimiciv:{metadata['subject_id']}":
                raise ValueError("Note/patient identity mismatch")
            if availability(metadata, cutoff) != "available_by_cutoff":
                raise ValueError("Late note leaked into merged evidence")
            if source["text"][source["span_start"] : source["span_end"]] != fact.quote:
                raise ValueError("Quote does not match exact source offsets")
            if (
                fact.clinical_time is not None
                or source["extraction_verified"] is not False
            ):
                raise ValueError("Unverified note claim was promoted")
            alignment[source["quote_alignment"]] += 1
        total += len(added)
        for batch in combined.attempts:
            for attempt in batch.get("note_extraction", []):
                rejected += len(attempt.get("rejected_claims", []))
        if not row["notes_used"]:
            continue
        lines += [
            f"## {combined.patient_id}",
            "",
            f"Added {len(added)} candidate facts; original hospital facts retained. "
            f"Cutoff: {combined.configuration['assessment_time']} "
            "(MIMIC shifted clock).",
            "",
            "| Concept | Assertion | Experiencer | Value / unit | Temporal scope "
            "| Exact source quote | Note ID |",
            "|---|---|---|---|---|---|---|",
        ]
        for fact in added:
            values = [
                fact.concept,
                fact.assertion,
                fact.experiencer,
                f"{fact.value if fact.value is not None else 'Not quantified'} "
                f"{fact.unit or ''}",
                fact.original["temporal_scope"],
                fact.quote,
                fact.record_id,
            ]
            cells = [" ".join(str(v).split()).replace("|", "&#124;") for v in values]
            lines.append("| " + " | ".join(cells) + " |")
        lines += [
            "",
            "Follow-up: confirm temporal relevance, missing duration, "
            "current medication use, and any unsupported trial-specific details.",
            "",
        ]
    if total != report["note_facts_added"]:
        raise ValueError("Combined note-fact count mismatch")
    audit = {
        "profiles": len(report["patient_results"]),
        "note_facts": total,
        "quote_alignment": dict(alignment),
        "rejected_claim_occurrences": rejected,
        "hospital_facts_preserved": True,
        "all_note_quotes_verified_against_offsets": True,
        "all_notes_available_by_cutoff": True,
        "clinical_eligibility_assessed": False,
    }
    (args.input / "audit.json").write_text(json.dumps(audit, indent=2))
    (args.input / "patient_note_evidence.md").write_text("\n".join(lines))
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
