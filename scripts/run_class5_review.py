"""Run three frozen real trials against the fixed review pool; retain unknown labels."""

import json
from pathlib import Path

from clinical_trial_patient_matching.retrieval import Models, TrialQuery, run
from clinical_trial_patient_matching.retrieval_evaluation import evaluate
from clinical_trial_patient_matching.search_documents import Judgment, SearchDocument


def main():
    root = Path("data/class5")
    review = root / "review"
    models_config = json.loads((root / "models.json").read_text())
    models = Models(
        models_config["encoder_revision"], models_config["reranker_revision"]
    )
    documents = [
        SearchDocument.model_validate_json(line)
        for line in (review / "documents.jsonl").read_text().splitlines()
    ]
    pool = json.loads((review / "pool.json").read_text())
    rows = [
        Judgment.model_validate_json(line)
        for line in (review / "judgments.jsonl").read_text().splitlines()
    ]
    reports = []
    for query_path in sorted(review.glob("*-query.json")):
        query = TrialQuery.model_validate_json(query_path.read_text())
        output = review / f"{query.trial_id}-run"
        result = run(documents, query, models, output, root / "embeddings")
        report = evaluate(result, rows, pool)
        (review / f"{query.trial_id}-evaluation.json").write_text(
            json.dumps(report, indent=2)
        )
        reports.append(report)
        print(
            f"Saved fixed-pool run for {query.trial_id}; independent review coverage "
            f"{report['comparisons']['fused']['coverage']:.0%}",
            flush=True,
        )
    (review / "comparison.json").write_text(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
