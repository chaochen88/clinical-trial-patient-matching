"""Fresh public-data screening demonstration with live Claude and Langfuse."""

import asyncio
import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from langchain_core.runnables.config import var_child_runnable_config
from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler

from clinical_trial_patient_matching import assessment, extraction, patients, retrieval
from clinical_trial_patient_matching.review_cli import load_environment
from clinical_trial_patient_matching.review_observability import (
    ReviewTelemetry,
    redact_export,
    redact_payload,
)
from clinical_trial_patient_matching.review_store import ReviewStore
from clinical_trial_patient_matching.review_workflow import (
    execute_run,
    initial_review_state,
)
from clinical_trial_patient_matching.search_documents import SearchDocument


def main():
    load_environment()
    model = os.environ["ANTHROPIC_MODEL"]
    session = "live-screening-" + str(uuid4())
    output = Path("data/live-traced") / session
    output.mkdir(parents=True)
    client = Langfuse(mask=redact_payload, mask_otel_spans=redact_export, timeout=20)
    if not client.auth_check():
        raise RuntimeError("Langfuse authentication failed")
    url = client.get_trace_url(trace_id=client.create_trace_id())
    project_url = url.split("/traces/")[0]
    manifest = {
        "session_id": session,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "model": model,
        "output": str(output),
        "session_url": project_url + "/sessions/" + session,
        "stages": [],
        "trace_ids": [],
        "scope": "Frozen public trial; prepared public Synthea corpus; one patient",
    }

    def save():
        (output / "run.json").write_text(json.dumps(manifest, indent=2))

    @contextmanager
    def stage(name):
        trace_id = client.create_trace_id()
        manifest["trace_ids"].append(trace_id)
        manifest["current_stage"] = name
        save()
        print(f"START {name}: {project_url}/traces/{trace_id}", flush=True)
        started = time.monotonic()
        handler = CallbackHandler(trace_context={"trace_id": trace_id})
        with propagate_attributes(
            session_id=session, metadata={"stage": name, "model": model}
        ):
            with client.start_as_current_observation(
                name=name, trace_context={"trace_id": trace_id}
            ):
                token = var_child_runnable_config.set({"callbacks": [handler]})
                try:
                    yield
                finally:
                    var_child_runnable_config.reset(token)
        client.flush()
        manifest["stages"].append(
            {"name": name, "trace_id": trace_id, "seconds": time.monotonic() - started}
        )
        save()
        print(f"DONE {name}", flush=True)

    save()
    print("FOLLOW IN LANGFUSE: " + manifest["session_url"], flush=True)
    try:
        import psycopg
        from langchain_anthropic import ChatAnthropic

        with stage("class01-api-smoke-test"):
            response = ChatAnthropic(
                model=model, max_tokens=32, max_retries=0, timeout=45
            ).invoke("Reply with: Anthropic connection OK")
            manifest["api_smoke_test"] = bool(response.content)
        with stage("class02-database-and-source-check"):
            with psycopg.connect(ReviewStore().dsn, dbname="aact") as conn:
                table = conn.execute("SELECT to_regclass('ctgov.studies')").fetchone()[
                    0
                ]
                manifest["aact_studies_table_present"] = table is not None
                if table:
                    row = conn.execute(
                        "SELECT nct_id FROM ctgov.studies WHERE nct_id=%s",
                        ("NCT03926806",),
                    ).fetchone()
                    manifest["aact_trial_present"] = row is not None
            manifest["data_mode"] = (
                "retrospective frozen source; not current recruitment"
            )
        source = Path("data/class5/sources/NCT03926806.json")
        original = json.loads(source.read_text())
        protocol = original["protocolSection"]
        adapted = {
            "nct_id": "NCT03926806",
            "criteria": protocol["eligibilityModule"]["eligibilityCriteria"],
            "source": str(source),
            "synthetic": False,
        }
        snapshot = output / "trial-source.json"
        snapshot.write_text(json.dumps(adapted))
        with stage("class03-live-trial-extraction"):
            trial_extraction = extraction.run_snapshot(
                snapshot,
                output / "trial-extraction",
                extraction.anthropic_extractor(model),
                "anthropic",
                model,
                use_cache=False,
            )
            manifest["extracted_bullets"] = len(trial_extraction["records"])
        # Newly generated mappings are artifacts awaiting review. Assessment uses
        # the existing reviewed rules for exactly the same frozen source trial.
        trial = assessment.Trial.model_validate_json(
            Path("data/class6/NCT03926806-trial.json").read_text()
        )
        query = retrieval.TrialQuery.model_validate_json(
            Path("data/class5/query.json").read_text()
        )
        if trial.trial_version != query.source_hash:
            raise ValueError("Frozen trial and retrieval query versions differ")
        with stage("class05-hybrid-retrieval-and-reranking"):
            documents = [
                SearchDocument.model_validate_json(line)
                for line in Path("data/class5/documents.jsonl").read_text().splitlines()
            ]
            config = json.loads(Path("data/class5/models.json").read_text())
            models = retrieval.Models(
                config["encoder_revision"], config["reranker_revision"]
            )
            result = retrieval.run(
                documents,
                query,
                models,
                output / "retrieval",
                Path("data/class5/embeddings"),
            )
            selected = next(r for r in result["shortlist"] if r["selected"])
            patient_id = selected["patient_id"]
            manifest["patient_id"] = patient_id
            manifest["retrieval_coverage"] = result["search_coverage"]
        profile = patients.Profile.model_validate_json(
            (Path("data/class5/profiles") / f"{patient_id}.json").read_text()
        )
        cutoff = next(
            d.assessment_time for d in documents if d.patient_id == patient_id
        )
        with stage("class04-live-patient-evidence-extraction"):
            usable = [
                f
                for f in profile.facts
                if not any(
                    t and t > cutoff for t in (f.clinical_time, f.available_time)
                )
            ]
            # A source-linked view of existing public quotes, not a fabricated note.
            text = "\n".join(dict.fromkeys(f.quote for f in usable))[:12000]
            record = patients.Record(
                patient_id=patient_id,
                dataset="public-synthea-quote-view",
                record_id=session + ":quote-view",
                text=text,
                available_time=cutoff,
            )
            profile = patients.build_profile(
                [record],
                patients.anthropic_patient_extractor(model),
                {
                    "provider": "anthropic",
                    "model": model,
                    "derived_view": (
                        "Quotes from dated public source facts; "
                        "bounded to 12000 characters"
                    ),
                },
                structured_facts=profile.facts,
            )
            (output / "profile.json").write_text(profile.model_dump_json(indent=2))
        with stage("class06-live-criterion-assessment"):
            assessed = assessment.build_workflow(
                output / "assessment",
                assessment.anthropic_interpreter(model),
                {"provider": "anthropic", "model": model},
            ).invoke(
                {
                    "profile": profile,
                    "trial": trial,
                    "assessment_time": cutoff,
                    "time_basis": "calendar",
                }
            )
            manifest["live_report"] = assessed["saved_to"]
            manifest["live_overall"] = assessed["report"].overall
        with stage("class07-live-agent-and-MCP-lookup"):
            from clinical_trial_patient_matching.evidence_agent import (
                ToolExecutor,
                anthropic_planner,
                build_investigation,
                initial_state,
            )
            from clinical_trial_patient_matching.patient_mcp_client import MCPTransport
            from clinical_trial_patient_matching.patient_tools import (
                EvidenceBackend,
                create_snapshot,
            )

            config_path = create_snapshot(
                output / "evidence.sqlite", [profile], [trial], cutoff
            )
            initial = profile.model_copy(
                update={
                    "facts": [f for f in profile.facts if f.concept != "BMI"],
                    "conflicts": [],
                }
            )
            manifest["class7_test_variant"] = (
                "BMI intentionally withheld from initial bundle; "
                "full evidence snapshot retained"
            )
            with MCPTransport(config_path) as transport:
                graph = build_investigation(
                    ToolExecutor(EvidenceBackend(config_path), transport),
                    anthropic_planner(model),
                )
                investigated = asyncio.run(
                    graph.ainvoke(
                        initial_state(initial, trial, cutoff),
                        {"max_concurrency": 4, "recursion_limit": 64},
                    )
                )
            (output / "investigation.json").write_text(
                json.dumps(
                    {
                        "events": investigated["events"],
                        "usage": investigated["usage"],
                        "report": investigated["report"].model_dump(mode="json"),
                        "model_calls": investigated["model_calls"],
                    },
                    indent=2,
                    default=str,
                )
            )
            manifest["agent_model_calls"] = investigated["model_calls"]
        store = ReviewStore()
        store.setup()
        manifest["current_stage"] = "class08-persistent-human-review"
        save()
        store.create(initial_review_state(profile, trial, cutoff), run_id=session)
        execute_run(store, session, telemetry=ReviewTelemetry(store))
        row = store.get(session)
        with store.connection() as conn:
            review_traces = conn.execute(
                "SELECT trace_id FROM screening.traces WHERE run_id=%s",
                (session,),
            ).fetchall()
        manifest["trace_ids"].extend(r["trace_id"] for r in review_traces)
        manifest["status"] = row["status"]
        manifest["review_request_id"] = row["pending_review"]["review_request_id"]
        manifest["review_note"] = (
            "Persistent review uses deterministic reassessment; the preceding live "
            "LLM report is saved separately. No human decision was fabricated."
        )
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        save()
        print(json.dumps(manifest, indent=2), flush=True)
    except Exception as error:
        manifest.update(status="failed", error_type=type(error).__name__)
        save()
        client.flush()
        raise


if __name__ == "__main__":
    main()
