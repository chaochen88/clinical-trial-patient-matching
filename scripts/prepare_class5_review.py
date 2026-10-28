"""Freeze a 20-patient review pool before seeing rankings; create 60 unlabeled pairs."""

import json
from pathlib import Path

from prepare_class5 import get_json

from clinical_trial_patient_matching.extraction import bullets, digest
from clinical_trial_patient_matching.retrieval import TrialQuery
from clinical_trial_patient_matching.search_documents import Judgment, SearchDocument


def main():
    root = Path("data/class5")
    output = root / "review"
    output.mkdir(parents=True, exist_ok=True)
    if (output / "judgments.jsonl").exists():
        raise SystemExit(
            "Review pool already exists; preserving judgments and frozen sources"
        )
    docs = [
        SearchDocument.model_validate_json(line)
        for line in (root / "documents.jsonl").read_text().splitlines()
    ]
    # Stable hash ordering independent of trial relevance or model rankings.
    chosen = sorted(
        docs, key=lambda doc: digest({"seed": "class5-review-v1", "id": doc.patient_id})
    )[:20]
    pool = [doc.patient_id for doc in chosen]
    (output / "pool.json").write_text(json.dumps(pool, indent=2))
    with (output / "documents.jsonl").open("w") as handle:
        for doc in chosen:
            handle.write(doc.model_dump_json() + "\n")
    judgments = []
    for trial_id in ("NCT03926806", "NCT04450407", "NCT03548935"):
        snapshot = root / "sources" / f"{trial_id}.json"
        if not snapshot.exists():
            snapshot.write_text(
                json.dumps(
                    get_json(f"https://clinicaltrials.gov/api/v2/studies/{trial_id}"),
                    indent=2,
                )
            )
        raw = json.loads(snapshot.read_text())
        protocol = raw["protocolSection"]
        parts = bullets(protocol["eligibilityModule"]["eligibilityCriteria"])
        query = TrialQuery(
            trial_id=trial_id,
            source_hash=digest(raw),
            condition_terms=protocol.get("conditionsModule", {}).get("conditions", []),
            eligibility_metadata={
                key: protocol["eligibilityModule"][key]
                for key in ("minimumAge", "maximumAge")
                if key in protocol["eligibilityModule"]
            },
            inclusion_quotes=[
                b["text"] for b in parts if b["criterion_type"] == "inclusion"
            ],
            exclusion_checklist=[
                b["text"] for b in parts if b["criterion_type"] == "exclusion"
            ],
        )
        (output / f"{trial_id}-query.json").write_text(query.model_dump_json(indent=2))
        judgments.extend(
            Judgment(
                trial_id=trial_id,
                patient_id=pid,
                label="unresolved",
                reviewer="pending-independent-review",
                rationale="Review complete frozen evidence without model rankings",
            )
            for pid in pool
        )
    with (output / "judgments.jsonl").open("w") as handle:
        for judgment in judgments:
            handle.write(judgment.model_dump_json() + "\n")
    print(f"Frozen {len(pool)} patients and {len(judgments)} unresolved review pairs")


if __name__ == "__main__":
    main()
