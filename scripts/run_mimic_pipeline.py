"""Run local MIMIC retrieval/reranking and a bounded live Claude assessment pilot."""

import argparse
import csv
import hashlib
import json
import os
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from clinical_trial_patient_matching.assessment import (
    PROMPT,
    Trial,
    anthropic_interpreter,
    build_workflow,
)
from clinical_trial_patient_matching.extraction import digest
from clinical_trial_patient_matching.mimic_adapter import TIME_BASIS
from clinical_trial_patient_matching.patients import Profile
from clinical_trial_patient_matching.retrieval import Models, TrialQuery, run
from clinical_trial_patient_matching.search_documents import SearchDocument


def save(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def select_pilot(shortlist, profiles, limit, all_patients=False):
    if not 1 <= limit <= (40 if all_patients else 3):
        raise ValueError("The live pilot is limited to one to three patients")
    selected = []
    seen = set()
    for row in sorted(shortlist["shortlist"], key=lambda row: row["rank"]):
        if row["patient_id"] in seen:
            raise ValueError("Duplicate ranked patient")
        seen.add(row["patient_id"])
        profile = profiles[row["patient_id"]]
        if row["profile_version"] != profile.profile_version:
            raise ValueError("Ranked profile version changed")
        has_note = any(
            f.original.get("source_format") == "mimic_note" for f in profile.facts
        )
        if row["selected"] and (has_note or all_patients) and len(selected) < limit:
            selected.append(row["patient_id"])
    return selected


def markdown_report(report):
    score = report.eligibility_score
    lines = [
        f"# {report.patient_id}",
        "",
        f"Overall: **{report.overall}**. "
        f"Verified evidence score: **{score.value}/100**.",
        f"Verified passes: {score.passed}; failures: {score.failed}; "
        f"unresolved: {score.unresolved}. Coverage: {score.coverage_percent}%.",
        "",
        "Claude results remain unverified. Passing schema and citation checks is "
        "not clinical validation. No enrollment decision is approved.",
        "",
        "| Criterion | Predicate | Status | Evidence | Verified "
        "| Processing | Reason | Follow-up |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for result in report.results:
        values = [
            result.criterion_id,
            result.predicate,
            result.status,
            result.evidence_state,
            result.verified,
            result.processing_status,
            result.reason,
            result.follow_up or "",
        ]
        lines.append(
            "| "
            + " | ".join(
                " ".join(str(v).split()).replace("|", "&#124;") for v in values
            )
            + " |"
        )
    lines += [
        "",
        "Exact citations, source versions, input bundles and attempts are "
        "saved in the corresponding report JSON.",
        "",
    ]
    return "\n".join(lines)


def documents(root):
    return [
        SearchDocument.model_validate_json(line)
        for line in (root / "documents.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def load_profiles(root):
    """Read either an enrichment folder or a hash-bound shared pool index."""
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    entries = manifest.get("profiles")
    if entries is not None:
        paths = []
        for entry in entries:
            path = Path(entry["profile_path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("Indexed profile hash changed")
            paths.append(path)
    else:
        enrichment = json.loads((root / "enrichment_report.json").read_text())
        if enrichment["status"] != "completed":
            raise ValueError("Combined profile enrichment is incomplete")
        paths = list((root / "profiles").glob("*.json"))
    profiles = {}
    for path in paths:
        profile = Profile.model_validate_json(path.read_text())
        if profile.patient_id in profiles:
            raise ValueError("Duplicate combined patient")
        profiles[profile.patient_id] = profile
    return profiles


def replay_then_live(patient, saved_calls, live):
    """Reuse only identical patient/context/feedback responses on continuation."""
    cache = defaultdict(deque)
    for call in saved_calls:
        if call["patient_id"] == patient:
            cache[digest([call["context"], call["feedback"]])].append(call)

    def invoke(context, feedback):
        queue = cache[digest([context, feedback])]
        if not queue:
            return live(context, feedback)
        call = queue.popleft()
        if call["raw"].get("response_metadata", {}).get("stop_reason") == "max_tokens":
            raise ValueError("Truncated narrative interpretation")
        if call["parsed"] is None:
            raise ValueError(call["parsing_error"])
        return call["parsed"]

    return invoke


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--combined",
        type=Path,
        default=Path("data/mimic/NCT03926806/with-notes/20261029T012542Z"),
    )
    parser.add_argument(
        "--hospital", type=Path, default=Path("data/mimic/NCT03926806/processed-20")
    )
    parser.add_argument(
        "--trial", type=Path, default=Path("data/class6/NCT03926806-trial.json")
    )
    parser.add_argument("--query", type=Path, default=Path("data/class5/query.json"))
    parser.add_argument("--models", type=Path, default=Path("data/class5/models.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--patients", type=int, default=3, choices=range(1, 41))
    parser.add_argument(
        "--all-patients",
        action="store_true",
        help="Assess the entire pool, including patients without notes",
    )
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 5))
    parser.add_argument(
        "--top-ranked",
        action="store_true",
        help="Assess the top N ranked patients, with or without notes",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--transport-retries", type=int, default=0, choices=range(0, 3))
    args = parser.parse_args()
    load_dotenv()
    model = os.environ.get("ANTHROPIC_MODEL")
    if not model or not os.environ.get("ANTHROPIC_API_KEY"):
        parser.error("Configure ANTHROPIC_MODEL and ANTHROPIC_API_KEY")
    # Retrieval uses existing cached local models, not cloud embedding services.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    output = args.output or Path("data/mimic/NCT03926806/pipeline-live") / datetime.now(
        UTC
    ).strftime("%Y%m%dT%H%M%SZ")
    previous = None
    if args.resume:
        if not args.output:
            parser.error("--resume requires --output")
        previous = json.loads((output / "manifest.json").read_text())
    else:
        output.mkdir(parents=True, exist_ok=False)
    trial = Trial.model_validate_json(args.trial.read_text())
    query = TrialQuery.model_validate_json(args.query.read_text(encoding="utf-8-sig"))
    if (query.trial_id, query.source_hash) != (trial.trial_id, trial.trial_version):
        raise ValueError(
            "Retrieval query and assessment trial have different snapshots"
        )
    profiles = load_profiles(args.combined)
    expanded = args.all_patients or args.top_ranked
    if args.all_patients and args.top_ranked:
        parser.error("Choose --all-patients or --top-ranked")
    if not expanded and args.patients > 3:
        parser.error("Use --all-patients or --top-ranked for an expanded assessment")
    if args.patients > len(profiles):
        parser.error("Patient limit exceeds pool size")
    if args.all_patients and args.patients != len(profiles):
        parser.error("--all-patients requires --patients equal to the pool size")
    combined, baseline = documents(args.combined), documents(args.hospital)
    if set(profiles) != {d.patient_id for d in combined} or set(profiles) != {
        d.patient_id for d in baseline
    }:
        raise ValueError("Hospital and combined pools differ")
    for doc in combined:
        p = profiles[doc.patient_id]
        if doc.profile_version != p.profile_version:
            raise ValueError("Search document and profile versions differ")
        if p.configuration["time_basis"] != TIME_BASIS:
            raise ValueError("Unexpected source time basis")
        if doc.assessment_time.isoformat() != p.configuration["assessment_time"]:
            raise ValueError("Search and assessment cutoffs differ")
    manifest = {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "trial_id": trial.trial_id,
        "trial_version": trial.trial_version,
        "source": str(args.combined),
        "model": model,
        "reports": [],
        "calls": [],
        "live_patient_limit": args.patients,
        "prompt": PROMPT,
        "prompt_hash": digest(PROMPT),
        "live_selection_policy": (
            "All patients, including those without notes, in retrieval rank order"
            if args.all_patients
            else f"Top {args.patients} by retrieval rank, with or without notes"
            if args.top_ranked
            else "Highest-ranked shortlisted patients with note evidence"
        ),
        "workers": args.workers,
        "transport_retries": args.transport_retries,
        "data_handling": "User previously confirmed approved MIMIC Claude processing",
        "source_documents_sha256": hashlib.sha256(
            (args.combined / "documents.jsonl").read_bytes()
        ).hexdigest(),
        "model_revisions": json.loads(args.models.read_text()),
        "metric_limits": "No adjudicated MIMIC relevance/eligibility labels; "
        "recall, nDCG, F1 and clinical accuracy are not measured",
    }
    if previous:
        for key in (
            "trial_version",
            "source",
            "model",
            "source_documents_sha256",
            "model_revisions",
            "prompt_hash",
        ):
            if previous[key] != manifest[key]:
                raise ValueError(f"Cannot resume changed input: {key}")
        save(output / "manifest-before-continuation.json", previous)
        manifest["reports"] = previous["reports"]
        manifest["calls"] = previous["calls"]
        manifest["started_at"] = previous["started_at"]
        manifest["continued_at"] = datetime.now(UTC).isoformat()
        manifest["previous_live_patient_limit"] = previous["live_patient_limit"]
    save(output / "manifest.json", manifest)
    save(output / "trial.json", trial.model_dump(mode="json"))
    try:
        config = manifest["model_revisions"]
        local = (
            None
            if args.resume
            else Models(config["encoder_revision"], config["reranker_revision"])
        )
        rankings = {}
        for name, docs in [("hospital", baseline), ("combined", combined)]:
            if args.resume:
                rankings[name] = json.loads(
                    (output / f"retrieval-{name}/shortlist.json").read_text()
                )
                if rankings[name]["query"]["source_hash"] != trial.trial_version:
                    raise ValueError("Saved ranking uses a different trial")
                continue
            rankings[name] = run(
                docs,
                query,
                local,
                output / f"retrieval-{name}",
                output / "embeddings",
                retrieval_k=len(profiles),
                shortlist_k=len(profiles),
                patient_specific_assessments=True,
            )
            print(f"Ranked {len(docs)} {name} profiles", flush=True)
        selected = select_pilot(rankings["combined"], profiles, args.patients, expanded)
        if args.all_patients and len(selected) != len(profiles):
            raise ValueError("Retrieval did not select the complete patient pool")
        if not selected:
            raise ValueError(
                "No shortlisted patient has note evidence for live testing"
            )
        if any(
            e["case"] not in selected
            for e in manifest["reports"]
            if e["mode"] == "rules-plus-claude"
        ):
            raise ValueError("Saved live reports fall outside the requested top ranks")
        manifest["live_patients"] = selected
        manifest["actual_live_patients"] = len(selected)
        save(output / "manifest.json", manifest)
        for row in rankings["combined"]["shortlist"]:
            if any(
                e["case"] == row["patient_id"] and e["mode"] == "rules-only"
                for e in manifest["reports"]
            ):
                continue
            profile = profiles[row["patient_id"]]
            cutoff = datetime.fromisoformat(profile.configuration["assessment_time"])
            state = build_workflow(
                output / "reports",
                configuration={
                    "provider": "none",
                    "model": None,
                    "purpose": "MIMIC pipeline baseline",
                },
            ).invoke(
                dict(
                    profile=profile,
                    trial=trial,
                    assessment_time=cutoff,
                    time_basis=TIME_BASIS,
                )
            )
            manifest["reports"].append(
                {
                    "case": profile.patient_id,
                    "mode": "rules-only",
                    "cohort": "MIMIC hospital + notes",
                    "path": state["saved_to"],
                    "report_key": state["report"].report_key,
                    "overall": state["report"].overall,
                }
            )
        save(output / "manifest.json", manifest)
        (output / "calls").mkdir(exist_ok=args.resume)
        audit_lock = threading.Lock()
        saved_calls = (
            [json.loads((output / e["file"]).read_text()) for e in manifest["calls"]]
            if args.resume
            else []
        )

        def assess_patient(item):
            index, patient = item
            if any(
                e["case"] == patient and e["mode"] == "rules-plus-claude"
                for e in manifest["reports"]
            ):
                return
            profile = profiles[patient]
            cutoff = datetime.fromisoformat(profile.configuration["assessment_time"])

            def audit(call, patient=patient, index=index):
                with audit_lock:
                    call_id = len(manifest["calls"]) + 1
                    filename = f"calls/{call_id:03d}.json"
                    save(output / filename, {"patient_id": patient, **call})
                    manifest["calls"].append(
                        {
                            "patient_id": patient,
                            "file": filename,
                            "criterion_id": call["context"]["criterion"][
                                "criterion_id"
                            ],
                        }
                    )
                    save(output / "manifest.json", manifest)
                print(
                    f"Saved Claude response {call_id} "
                    f"(pilot patient {index}/{len(selected)})",
                    flush=True,
                )

            interpreter = anthropic_interpreter(
                model, audit=audit, transport_retries=args.transport_retries
            )
            if args.resume:
                interpreter = replay_then_live(patient, saved_calls, interpreter)
            state = build_workflow(
                output / "reports",
                interpreter,
                {
                    "provider": "anthropic",
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 2048,
                    "max_retries": args.transport_retries,
                    "timeout_seconds": 120,
                    "repair_limit": 1,
                    "prompt_version": "0.1",
                },
            ).invoke(
                dict(
                    profile=profile,
                    trial=trial,
                    assessment_time=cutoff,
                    time_basis=TIME_BASIS,
                )
            )
            report = state["report"]
            md = f"patient-{index}-claude.md"
            (output / md).write_text(markdown_report(report))
            entry = {
                "case": patient,
                "mode": "rules-plus-claude",
                "cohort": "MIMIC hospital + notes",
                "path": state["saved_to"],
                "report_key": report.report_key,
                "overall": report.overall,
                "markdown": md,
            }
            with audit_lock:
                manifest["reports"].append(entry)
                save(output / "manifest.json", manifest)
            print(
                f"Completed live pilot patient {index}/{len(selected)}: "
                f"{report.overall}",
                flush=True,
            )

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            list(executor.map(assess_patient, enumerate(selected, 1)))
        manifest["status"] = "completed"
        baseline_ranks = {
            r["patient_id"]: r["rank"] for r in rankings["hospital"]["shortlist"]
        }
        ranking_rows = []
        for row in rankings["combined"]["shortlist"]:
            pid = row["patient_id"]
            ranking_rows.append(
                {
                    "patient_id": pid,
                    "hospital_rank": baseline_ranks.get(pid),
                    "combined_rank": row["rank"],
                    "rank_change": baseline_ranks.get(pid, 0) - row["rank"],
                    "reranker_score": row["score"],
                    "note_facts": sum(
                        f.original.get("source_format") == "mimic_note"
                        for f in profiles[pid].facts
                    ),
                    "live_assessed": pid in selected,
                    "text_truncated": row["document_truncated"],
                }
            )
        with (output / "ranking.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(ranking_rows[0]))
            writer.writeheader()
            writer.writerows(ranking_rows)
        save(output / "ranking.json", ranking_rows)
        print(f"Saved pipeline run: {output}", flush=True)
    except Exception as error:
        manifest.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        save(output / "manifest.json", manifest)


if __name__ == "__main__":
    main()
