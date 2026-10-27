"""Retrieval evaluation; unresolved judgments never count as negatives."""

import argparse
import json
import math
from pathlib import Path

from clinical_trial_patient_matching.search_documents import Judgment


def metrics(ranking, labels, k=10):
    if k <= 0:
        raise ValueError("k must be positive")
    if any(
        value not in {"relevant", "irrelevant", "unresolved"}
        for value in labels.values()
    ):
        raise ValueError("Invalid judgment label")
    judged = sum(value != "unresolved" for value in labels.values())
    positives = {pid for pid, label in labels.items() if label == "relevant"}
    top = list(dict.fromkeys(ranking))[:k]
    complete = bool(labels) and judged == len(labels)
    hits = len(set(top).intersection(positives))
    dcg = sum(1 / math.log2(i + 2) for i, pid in enumerate(top) if pid in positives)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(positives))))
    return {
        f"recall@{k}": hits / len(positives) if complete and positives else None,
        f"ndcg@{k}": dcg / ideal if complete and positives else None,
        "coverage": judged / len(labels) if labels else 0,
        "positives": len(positives),
        "hits": hits,
        "complete_pool": complete,
        "no_positive_pool": complete and not positives,
    }


def evaluate(result: dict, judgments: list[Judgment], pool: list[str]):
    if len(pool) != len(set(pool)):
        raise ValueError("Duplicate pool patient IDs")
    labels = {}
    for row in judgments:
        if row.trial_id != result["query"]["trial_id"]:
            continue
        if row.patient_id not in pool or row.patient_id in labels:
            raise ValueError("Judgments must be unique and belong to the fixed pool")
        labels[row.patient_id] = row.label
    complete_labels = {pid: labels.get(pid, "unresolved") for pid in pool}
    rankings = {
        key: [row["patient_id"] for row in result[key]]
        for key in ("lexical", "vector", "fused", "shortlist")
    }
    if any(pid not in pool for ranking in rankings.values() for pid in ranking):
        raise ValueError("Run retrieval on the fixed pool, not a larger corpus")
    positives = {pid for pid, label in complete_labels.items() if label == "relevant"}
    timings = {
        "lexical": result["lexical_seconds"],
        "vector": result["vector_seconds"],
        "fused": max(result["lexical_seconds"], result["vector_seconds"]),
        "shortlist": max(result["lexical_seconds"], result["vector_seconds"])
        + result["reranker_seconds"],
    }
    return {
        "trial_id": result["query"]["trial_id"],
        "pool_count": len(pool),
        "comparisons": {
            key: {**metrics(ranking, complete_labels), "stage_seconds": timings[key]}
            for key, ranking in rankings.items()
        },
        "missed_positives": {
            key: sorted(positives.difference(ranking[:10]))
            for key, ranking in rankings.items()
        },
        "absent_from_fused_candidates": sorted(positives.difference(rankings["fused"])),
        "positives_rejected_by_filter": sorted(
            positives.intersection(result.get("rejected_patient_ids", []))
        ),
        "indexing_seconds": result.get("indexing_seconds"),
        "inference_cost": result.get("inference_cost"),
        "note": "Screening relevance, not enrollment eligibility; no TREC labels used.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [
        Judgment.model_validate_json(line)
        for line in args.judgments.read_text(encoding="utf-8").splitlines()
    ]
    report = evaluate(
        json.loads(args.run.read_text(encoding="utf-8")),
        rows,
        json.loads(args.pool.read_text(encoding="utf-8")),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
