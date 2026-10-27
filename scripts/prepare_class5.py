"""Prepare public November 2021 Synthea corpus and frozen retrospective trial query."""

import argparse
import csv
import hashlib
import io
import json
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from clinical_trial_patient_matching.extraction import bullets, digest
from clinical_trial_patient_matching.retrieval import ENCODER, RERANKER, TrialQuery
from clinical_trial_patient_matching.search_documents import (
    build_document,
    profiles_from_fhir,
)

BASE = "https://synthetichealth.github.io/synthea-sample-data/downloads/"


def download(url, destination):
    if not destination.exists():
        temporary = destination.with_suffix(".part")
        with (
            urllib.request.urlopen(url, timeout=120) as response,
            temporary.open("wb") as handle,
        ):
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
        temporary.replace(destination)
    sha = hashlib.sha256()
    with destination.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    return {"url": url, "sha256": sha.hexdigest(), "bytes": destination.stat().st_size}


def get_json(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/class5"))
    parser.add_argument(
        "--assessment-time",
        type=datetime.fromisoformat,
        default=datetime.fromisoformat("2021-01-13T23:59:59-05:00"),
    )
    args = parser.parse_args()
    if args.assessment_time.utcoffset() is None:
        parser.error("assessment-time requires timezone")
    sources = args.output / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    profile_directory = args.output / "profiles"
    profile_directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "assessment_time": args.assessment_time.isoformat(),
    }
    for name in ("fhir_r4", "csv"):
        filename = f"synthea_sample_data_{name}_nov2021.zip"
        manifest[name] = download(BASE + filename, sources / filename)
        print(f"Downloaded/verified {filename}", flush=True)
    with zipfile.ZipFile(sources / "synthea_sample_data_csv_nov2021.zip") as archive:
        member = next(n for n in archive.namelist() if n.endswith("patients.csv"))
        rows = list(
            csv.DictReader(io.StringIO(archive.read(member).decode("utf-8-sig")))
        )
        csv_ids = {row["Id"] for row in rows}
    temporary_documents = args.output / "documents.jsonl.part"
    with (
        zipfile.ZipFile(sources / "synthea_sample_data_fhir_r4_nov2021.zip") as archive,
        temporary_documents.open("w", encoding="utf-8") as handle,
    ):
        patients = {}
        for member in archive.namelist():
            if not member.endswith(".json"):
                continue
            bundle = json.loads(archive.read(member))
            ids = [
                e["resource"]["id"]
                for e in bundle.get("entry", [])
                if e.get("resource", {}).get("resourceType") == "Patient"
            ]
            for patient_id in ids:
                if patient_id in patients:
                    raise ValueError(f"Duplicate FHIR patient {patient_id}")
                patients[patient_id] = member
        shared = sorted(csv_ids.intersection(patients))
        if len(shared) != 555:
            raise ValueError(
                f"Expected 555 shared patients, found {len(shared)}; inspect sources"
            )
        versions = []
        skipped = []
        for patient_id in shared:
            member = patients[patient_id]
            bundle = json.loads(archive.read(member))
            profiles = profiles_from_fhir(
                bundle, args.assessment_time, "Synthea-FHIR-R4-Nov2021"
            )
            own = [p for p in profiles if p.patient_id == patient_id]
            if len(own) != 1:
                skipped.append(patient_id)
                continue
            profile_path = profile_directory / f"{patient_id}.json"
            profile_path.write_text(own[0].model_dump_json(), encoding="utf-8")
            doc = build_document(
                own[0],
                args.assessment_time,
                {
                    "archive_member": member,
                    "profile_path": str(profile_path),
                    "fhir_sha256": manifest["fhir_r4"]["sha256"],
                    "csv_sha256": manifest["csv"]["sha256"],
                    "precision_policy": "FHIR indexed once; CSV for shared IDs only",
                },
            )
            handle.write(doc.model_dump_json() + "\n")
            versions.append((doc.patient_id, doc.document_version))
            if len(versions) % 50 == 0:
                print(f"Built {len(versions)}/{len(shared)} documents", flush=True)
    if skipped:
        raise ValueError(f"Missing profiles: {skipped}")
    temporary_documents.replace(args.output / "documents.jsonl")
    manifest.update(
        shared_patient_ids=shared,
        document_count=len(versions),
        corpus_version=digest(versions),
    )
    trial_url = "https://clinicaltrials.gov/api/v2/studies/NCT03926806"
    trial_path = sources / "NCT03926806.json"
    if not trial_path.exists():
        trial_path.write_text(
            json.dumps(get_json(trial_url), indent=2), encoding="utf-8"
        )
    trial = json.loads(trial_path.read_text(encoding="utf-8"))
    protocol = trial["protocolSection"]
    criteria = protocol["eligibilityModule"]["eligibilityCriteria"]
    parts = bullets(criteria)
    query = TrialQuery(
        trial_id="NCT03926806",
        source_hash=hashlib.sha256(trial_path.read_bytes()).hexdigest(),
        condition_terms=protocol.get("conditionsModule", {}).get("conditions", []),
        eligibility_metadata={
            key: protocol["eligibilityModule"][key]
            for key in ("minimumAge", "maximumAge")
            if key in protocol["eligibilityModule"]
        },
        inclusion_quotes=[
            p["text"] for p in parts if p["criterion_type"] == "inclusion"
        ],
        exclusion_checklist=[
            p["text"] for p in parts if p["criterion_type"] == "exclusion"
        ],
    )
    # Review numeric rules before enabling hard filters from trial prose.
    (args.output / "query.json").write_text(
        query.model_dump_json(indent=2), encoding="utf-8"
    )
    models = {
        "encoder_revision": get_json(f"https://huggingface.co/api/models/{ENCODER}")[
            "sha"
        ],
        "reranker_revision": get_json(f"https://huggingface.co/api/models/{RERANKER}")[
            "sha"
        ],
    }
    (args.output / "models.json").write_text(
        json.dumps(models, indent=2), encoding="utf-8"
    )
    manifest["trial"] = {
        "url": trial_url,
        "source_hash": query.source_hash,
        "last_update": protocol.get("statusModule", {}).get("lastUpdatePostDateStruct"),
    }
    (sources / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(f"Prepared {len(versions)} unique patient documents", flush=True)


if __name__ == "__main__":
    main()
