"""Pin public TrialGPT expert annotations; separate inputs from answer fields."""

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

import duckdb

DATASET = "ncbi/TrialGPT-Criterion-Annotations"


def separate(row):
    polarity = row["criterion_type"]
    mapping = {
        ("inclusion", "included"): "met",
        ("inclusion", "not included"): "not_met",
        ("exclusion", "excluded"): "met",
        ("exclusion", "not excluded"): "not_met",
    }
    label = row["expert_eligibility"]
    if label in {"not enough information", "not applicable"}:
        status = "unknown" if label == "not enough information" else "not_applicable"
    else:
        status = mapping[(polarity, label)]
    inputs = {
        key: row[key]
        for key in (
            "annotation_id",
            "patient_id",
            "note",
            "trial_id",
            "trial_title",
            "criterion_type",
            "criterion_text",
            "training",
        )
    }
    gold = {
        "annotation_id": row["annotation_id"],
        "expert_eligibility": label,
        "predicate_status": status,
        "expert_sentences": row["expert_sentences"],
    }
    return inputs, gold


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/class6/annotations"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    lock = args.output / "source.json"
    if lock.exists():
        source = json.loads(lock.read_text())
    else:
        with urllib.request.urlopen(
            f"https://huggingface.co/api/datasets/{DATASET}", timeout=60
        ) as response:
            metadata = json.load(response)
        files = [
            f["rfilename"]
            for f in metadata["siblings"]
            if f["rfilename"].endswith(".parquet")
        ]
        if not files:
            raise ValueError("Dataset format changed; inspect source metadata")
        source = {"dataset": DATASET, "revision": metadata["sha"], "files": files}
    all_rows, hashes = [], {}
    for name in source["files"]:
        path = args.output / Path(name).name
        url = f"https://huggingface.co/datasets/{DATASET}/resolve/{source['revision']}/{name}"
        if not path.exists():
            with urllib.request.urlopen(url, timeout=60) as response:
                path.write_bytes(response.read())
        value = hashlib.sha256(path.read_bytes()).hexdigest()
        if source.get("sha256", {}).get(name, value) != value:
            raise ValueError("Pinned annotation file hash changed")
        hashes[name] = value
        with duckdb.connect() as connection:
            cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(path)])
            columns = [d[0] for d in cursor.description]
            all_rows.extend(
                dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
            )
    outputs = {
        "development-inputs": [],
        "development-gold": [],
        "evaluation-inputs": [],
        "evaluation-gold": [],
    }
    identities = set()
    for row in all_rows:
        if row["annotation_id"] in identities:
            raise ValueError("Duplicate annotation identity")
        identities.add(row["annotation_id"])
        inputs, gold = separate(row)
        prefix = "development" if row["training"] else "evaluation"
        outputs[f"{prefix}-inputs"].append(inputs)
        outputs[f"{prefix}-gold"].append(gold)
    for name, rows in outputs.items():
        (args.output / f"{name}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows)
        )
    source.update(
        sha256=hashes,
        row_count=len(all_rows),
        split_counts={key: len(rows) for key, rows in outputs.items()},
        split_policy="Original training flag; answer fields only in gold files",
    )
    lock.write_text(json.dumps(source, indent=2))
    print(
        json.dumps(
            {
                "revision": source["revision"],
                "rows": len(all_rows),
                "counts": source["split_counts"],
            }
        )
    )


if __name__ == "__main__":
    main()
