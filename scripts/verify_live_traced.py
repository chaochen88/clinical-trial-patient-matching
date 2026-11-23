"""Read back generation spans for the latest live test without exposing payloads."""

import json
from pathlib import Path

from langfuse import Langfuse

from clinical_trial_patient_matching.review_cli import load_environment


def main():
    load_environment()
    paths = list(Path("data/live-traced").glob("*/run.json"))
    records = [(p, json.loads(p.read_text(encoding="utf-8-sig"))) for p in paths]
    path, manifest = max(
        ((p, r) for p, r in records if r["status"] != "interrupted"),
        key=lambda pair: pair[1]["started_at"],
    )
    client = Langfuse(timeout=20)
    spans = client.api.observations.get_many(
        session_id=manifest["session_id"],
        fields="basic,usage",
        limit=1000,
    ).data
    generations = [s for s in spans if s.type == "GENERATION"]
    report = {
        "session_url": manifest["session_url"],
        "status": manifest["status"],
        "observations": len(spans),
        "live_generations": len(generations),
        "generation_usage": [
            {"id": s.id, "usage": s.usage_details} for s in generations
        ],
    }
    assert generations, "No generation observations visible yet"
    (path.parent / "langfuse-verification.json").write_text(
        json.dumps(report, indent=2)
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "generation_usage"}, indent=2
        )
    )


if __name__ == "__main__":
    main()
