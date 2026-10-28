"""Run Class 4 with the configured Claude model, keeping each live run separate."""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from clinical_trial_patient_matching.patients import main as patient_main


def main():
    load_dotenv()
    model = os.environ.get("ANTHROPIC_MODEL")
    if not model or not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_MODEL and ANTHROPIC_API_KEY in .env")
    started = datetime.now(UTC)
    output = Path("data/class4/live") / started.strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "started_at": started.isoformat(),
        "provider": "anthropic",
        "model": model,
        "output": str(output),
        "status": "running",
    }
    manifest_path = output / "run.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    sys.argv = [
        "patients",
        "--model",
        model,
        "--output",
        str(output),
        "--queries",
        "data/class4/sources/queries.jsonl",
        "--fhir",
        "data/class4/sources/Maxwell782_Koepp521_6343cfd0-d47f-4a20-b561-b33d6441f372.json",
    ]
    print(f"Claude model: {model}; saving to {output}", flush=True)
    try:
        patient_main()
    except Exception as error:
        manifest.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    else:
        manifest["status"] = "completed"
    finally:
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
