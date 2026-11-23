"""Combine disjoint, completed MIMIC profile pools without changing source profiles."""

import argparse
import hashlib
import json
from pathlib import Path


def combine(sources, output):
    documents, index, identities = [], [], set()
    for source in sources:
        report = source / "enrichment_report.json"
        if report.exists() and json.loads(report.read_text())["status"] != "completed":
            raise ValueError("Cannot combine unfinished note extraction")
        for line in (source / "documents.jsonl").read_text().splitlines():
            document = json.loads(line)
            identity = document["patient_id"]
            if identity in identities:
                raise ValueError("Patient pools overlap; refusing duplicate patients")
            identities.add(identity)
            path = source / "profiles" / f"{identity.split(':')[-1]}.json"
            raw = path.read_bytes()
            profile = json.loads(raw)
            if profile["patient_id"] != identity:
                raise ValueError("Profile identity mismatch")
            if profile["profile_version"] != document["profile_version"]:
                raise ValueError("Document profile version mismatch")
            index.append(
                {
                    "patient_id": identity,
                    "profile_path": str(path),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "profile_version": profile["profile_version"],
                    "hospital_facts": sum(
                        f["original"].get("source_format") != "mimic_note"
                        for f in profile["facts"]
                    ),
                    "note_facts": sum(
                        f["original"].get("source_format") == "mimic_note"
                        for f in profile["facts"]
                    ),
                }
            )
            documents.append(document)
    output.mkdir(parents=True, exist_ok=False)
    (output / "documents.jsonl").write_text(
        "".join(json.dumps(d) + "\n" for d in documents)
    )
    manifest = {
        "patients": len(index),
        "patients_with_note_facts": sum(p["note_facts"] > 0 for p in index),
        "sources": [str(s) for s in sources],
        "profiles": index,
        "meaning": "Reusable evidence pool; not an eligibility decision",
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    lines = [
        "# Reusable MIMIC patient pool",
        "",
        f"Patients: {len(index)}. With note facts: "
        f"{manifest['patients_with_note_facts']}.",
        "",
        "| Patient | Hospital facts | Note facts | Profile |",
        "|---|---:|---:|---|",
    ]
    for p in index:
        lines.append(
            f"| {p['patient_id']} | {p['hospital_facts']} | {p['note_facts']} | "
            f"{p['profile_path']} |"
        )
    (output / "review.md").write_text("\n".join(lines) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = combine(args.source, args.output)
    print(
        json.dumps({k: manifest[k] for k in ("patients", "patients_with_note_facts")})
    )


if __name__ == "__main__":
    main()
