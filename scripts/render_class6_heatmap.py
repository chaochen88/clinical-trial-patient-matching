"""Build a standalone interactive heatmap from saved Class 6 reports; no API calls."""

import argparse
import json
from pathlib import Path

from clinical_trial_patient_matching.assessment import ScreeningReport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/class6"))
    parser.add_argument("--output", type=Path, default=Path("data/class6/heatmap.html"))
    args = parser.parse_args()
    manifest = json.loads((args.input / "manifest.json").read_text(encoding="utf-8"))
    entries = [
        (entry["path"], "Shared examples", entry["mode"])
        for entry in manifest["reports"]
    ]
    index = args.input / "shortlist/index.json"
    if index.exists():
        entries.extend(
            (entry["report"], "Class 5 shortlist", "rules-only")
            for entry in json.loads(index.read_text(encoding="utf-8"))
        )
    reports = []
    for path, cohort, mode in entries:
        report = ScreeningReport.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )
        reports.append(
            {
                "cohort": cohort,
                "mode": mode,
                **report.model_dump(mode="json", exclude={"configuration"}),
            }
        )
    if not reports:
        raise ValueError("No saved reports found")
    # Escape markup delimiters so untrusted source quotes cannot terminate the
    # JSON script element. The UI writes record content only through textContent.
    payload = json.dumps(reports, ensure_ascii=True).replace("<", "\\u003c")
    template = Path(__file__).with_name("templates") / "class6_heatmap.html"
    html = template.read_text(encoding="utf-8").replace("__REPORT_DATA__", payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"Saved {len(reports)} report views to {args.output}")


if __name__ == "__main__":
    main()
