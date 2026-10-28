"""Download the pinned Class 4 sources and preserve their content hashes."""

import hashlib
import io
import json
import urllib.request
import zipfile
from pathlib import Path

DESTINATION = Path("data/class4/sources")
REVISION = "1b3242cd50c153a9c324661d4a0bd94fc68a4226"
TREC = f"https://raw.githubusercontent.com/ncbi-nlp/TrialGPT/{REVISION}/dataset/trec_2021/queries.jsonl"
SYNTHEA = (
    "https://synthetichealth.github.io/synthea-sample-data/downloads/"
    "synthea_sample_data_fhir_r4_sep2019.zip"
)
MEMBER = "Maxwell782_Koepp521_6343cfd0-d47f-4a20-b561-b33d6441f372.json"


def main():
    DESTINATION.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(TREC, timeout=120) as response:
        queries = response.read()
    row = next(
        json.loads(line)
        for line in queries.splitlines()
        if json.loads(line)["_id"] == "trec-202129"
    )
    (DESTINATION / "queries.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    print("Downloaded pinned TREC patient", flush=True)
    with urllib.request.urlopen(SYNTHEA, timeout=120) as response:
        archive_bytes = response.read()
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        member = next(name for name in archive.namelist() if name.endswith(MEMBER))
        patient_bytes = archive.read(member)
    (DESTINATION / MEMBER).write_bytes(patient_bytes)
    metadata = {
        "trec_url": TREC,
        "trec_revision": REVISION,
        "trec_full_file_sha256": hashlib.sha256(queries).hexdigest(),
        "synthea_url": SYNTHEA,
        "archive_member": member,
        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "patient_sha256": hashlib.sha256(patient_bytes).hexdigest(),
    }
    (DESTINATION / "manifest.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    print("Downloaded exact Synthea sample and saved source manifest", flush=True)


if __name__ == "__main__":
    main()
