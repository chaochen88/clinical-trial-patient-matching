"""Class 3 prototype: extract -> validate -> one repair -> save for review."""

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph

from clinical_trial_patient_matching.criteria import (
    SCHEMA_VERSION,
    Extraction,
    Rule,
    validate_source,
)

PROMPT_VERSION = "0.1"
PROMPT = Path(__file__).with_name("prompts").joinpath("extract_v0.1.txt").read_text()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def bullets(criteria: str) -> list[dict]:
    """Preserve every nonblank fragment, including unsupported unbulleted text."""
    result = []
    polarity = None
    counts = {"inclusion": 0, "exclusion": 0, "unassigned": 0}
    for line in criteria.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = re.fullmatch(r"(Inclusion|Exclusion) Criteria:\s*", stripped, re.I)
        if heading:
            polarity = heading[1].lower()
            continue
        if stripped.startswith(("* ", "- ")):
            text = stripped[2:]
        elif result and result[-1]["criterion_type"] == polarity:
            result[-1]["text"] += "\n" + line
            continue
        else:
            text = line
        kind = polarity or "unassigned"
        counts[kind] += 1
        result.append(
            {
                "source_bullet_id": f"{kind}-{counts[kind]}",
                "criterion_type": polarity,
                "text": text,
            }
        )
    if not result:
        raise ValueError("snapshot has no eligibility criteria")
    return result


def offline_extract(bullet: dict, feedback=None) -> dict:
    """Reviewed examples only; all other requirements remain visible for review."""
    text = bullet["text"]
    common = {k: bullet[k] for k in ("source_bullet_id", "criterion_type")}
    common["source_quote"] = text
    if text == "Participants must have HbA1c values of 5.6% to 9.5%, inclusive":
        rule = Rule(
            **common,
            kind="range",
            concept="HbA1c",
            lower=5.6,
            upper=9.5,
            lower_inclusive=True,
            upper_inclusive=True,
            unit="%",
        )
    elif text == (
        "Have any episodes of severe hypoglycemia and/or hypoglycemia "
        "unawareness within the 6 months prior to screening"
    ):
        children = [
            Rule(**common, kind="condition", concept=concept, window={"months": 6})
            for concept in ("severe hypoglycemia episode", "hypoglycemia unawareness")
        ]
        rule = Rule(
            **common,
            kind="group",
            concept="hypoglycemia",
            logic="any",
            children=children,
        )
        return Extraction(
            rules=[rule],
            review_findings=[
                "Time-window boundary wording requires reviewer confirmation."
            ],
        ).model_dump()
    elif text == (
        "Participants must have been using multiple daily injections "
        "without interruption for at least 3 months"
    ):
        rule = Rule(
            **common,
            kind="duration",
            concept="multiple daily injections",
            operator="ge",
            value=3,
            unit="calendar_months",
            uninterrupted=True,
        )
    else:
        return {
            "rules": [],
            "review_findings": [
                "Offline demo has no reviewed extraction for this bullet."
            ],
        }
    return Extraction(rules=[rule], review_findings=[]).model_dump()


class State(TypedDict, total=False):
    bullet: dict
    attempts: list[dict]
    findings: list[str]
    extraction: dict | None


def build_workflow(extractor):
    def extract(state):
        attempts = list(state.get("attempts", []))
        try:
            response = extractor(state["bullet"], state.get("findings", []))
            attempts.append({"response": response, "error": None})
        except Exception as error:
            # Keep the bullet for review on provider/parse errors; never drop it.
            attempts.append(
                {"response": None, "error": f"{type(error).__name__}: {error}"}
            )
        return {"attempts": attempts}

    def validate(state):
        attempt = state["attempts"][-1]
        findings = []
        parsed = None
        try:
            if attempt["error"]:
                raise ValueError(attempt["error"])
            response = attempt["response"]
            if response.get("provider_error"):
                raise ValueError(response["provider_error"])
            parsed = Extraction.model_validate(response.get("parsed", response))
            if not parsed.rules and not parsed.review_findings:
                raise ValueError("empty extraction must include a review finding")
            for rule in parsed.rules:
                validate_source(
                    rule,
                    state["bullet"]["source_bullet_id"],
                    state["bullet"]["criterion_type"],
                    state["bullet"]["text"],
                )
        except Exception as error:
            findings.append(str(error))
        return {
            "findings": findings,
            "extraction": parsed.model_dump() if parsed and not findings else None,
        }

    def route(state):
        return "extract" if state["findings"] and len(state["attempts"]) < 2 else END

    graph = StateGraph(State)
    graph.add_node("extract", extract)
    graph.add_node("validate", validate)
    graph.add_edge(START, "extract")
    graph.add_edge("extract", "validate")
    graph.add_conditional_edges("validate", route)
    return graph.compile()


def anthropic_extractor(model: str):
    from langchain_anthropic import ChatAnthropic

    llm = ChatAnthropic(model=model, temperature=0, max_tokens=4096, max_retries=0)
    structured = llm.with_structured_output(
        Extraction, method="function_calling", include_raw=True
    )

    def invoke(bullet, feedback):
        response = structured.invoke(
            [
                ("system", PROMPT),
                (
                    "human",
                    json.dumps({"bullet": bullet, "validation_feedback": feedback}),
                ),
            ]
        )
        raw = response["raw"]
        metadata = raw.response_metadata
        parsed = response["parsed"]
        error = response.get("parsing_error")
        if metadata.get("stop_reason") == "max_tokens":
            error = "truncated provider response"
        if parsed is None and not error:
            error = "refusal or missing structured tool response"
        return {
            "parsed": parsed.model_dump() if parsed else None,
            "raw": raw.model_dump(mode="json"),
            "provider_error": str(error) if error else None,
        }

    return invoke


def run_snapshot(
    path: Path,
    output: Path,
    extractor,
    provider: str,
    model: str,
    use_cache: bool = True,
) -> dict:
    source_bytes = path.read_bytes()
    snapshot = json.loads(source_bytes)
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    settings = {"temperature": 0, "max_tokens": 4096, "max_retries": 0}
    cache_key = digest(
        {
            "source": source_hash,
            "schema": SCHEMA_VERSION,
            "schema_hash": digest(Extraction.model_json_schema()),
            "prompt": PROMPT_VERSION,
            "prompt_hash": digest(PROMPT),
            "provider": provider,
            "model": model,
            "settings": settings,
        }
    )
    destination = output / f"{snapshot['nct_id']}-{cache_key[:12]}.json"
    if use_cache and destination.exists():
        cached = json.loads(destination.read_text())
        if cached["cache_key"] == cache_key:
            return cached
    workflow = build_workflow(extractor)
    records = []
    for bullet in bullets(snapshot["criteria"]):
        if bullet["criterion_type"] is None:
            state = {
                "extraction": None,
                "attempts": [],
                "findings": ["Missing inclusion/exclusion heading."],
            }
        else:
            state = workflow.invoke({"bullet": bullet, "attempts": []})
        parsed = state["extraction"]
        findings = state["findings"] + (parsed["review_findings"] if parsed else [])
        records.append(
            {
                **bullet,
                "status": "requires_review" if findings else "validated",
                "rules": parsed["rules"] if parsed else [],
                "validation_findings": findings,
                "attempts": state["attempts"],
            }
        )
    result = {
        "nct_id": snapshot["nct_id"],
        "source_path": str(path),
        "source_version": snapshot.get("updated_at"),
        "source_metadata": {
            key: snapshot.get(key)
            for key in ("source", "created_at", "last_update_posted_date")
        },
        "source_hash": source_hash,
        "original_criteria": snapshot["criteria"],
        "synthetic": snapshot.get("synthetic", False),
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "provider": provider,
        "model": model,
        "inference_settings": settings,
        "timestamp": datetime.now(UTC).isoformat(),
        "cache_key": cache_key,
        "records": records,
    }
    output.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


def write_review(results: list[dict], output: Path):
    lines = [
        "# Extraction review",
        "",
        "Validated means schema/source checks passed; clinical meaning needs review.",
        "Provider/model provenance is recorded in each JSON output.",
        "",
        "| Trial | Bullet | Status | Findings |",
        "|---|---|---|---|",
    ]
    for result in results:
        label = result["nct_id"] + (" (synthetic)" if result["synthetic"] else "")
        for record in result["records"]:
            finding = (
                "; ".join(record["validation_findings"]) or "Check semantic fidelity"
            )
            finding = " ".join(finding.split()).replace("|", "/")
            lines.append(
                f"| {label} | {record['source_bullet_id']} | "
                f"{record['status']} | {finding} |"
            )
    (output / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshots", nargs="*", type=Path)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("data/class3"))
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    paths = args.snapshots or [Path("class 1/trial_acct.json")]
    if args.demo:
        if not args.snapshots:
            if not paths[0].exists():
                paths = [Path("tests/fixtures/class3_trial.json")]
            paths += sorted(Path("tests/fixtures/class3").glob("*.json"))
        extractor, provider, model = (
            offline_extract,
            "offline",
            "reviewed-examples-v0.1",
        )
    else:
        model = os.environ.get("ANTHROPIC_MODEL")
        if not model or not os.environ.get("ANTHROPIC_API_KEY"):
            parser.error("set ANTHROPIC_MODEL and ANTHROPIC_API_KEY, or use --demo")
        extractor, provider = anthropic_extractor(model), "anthropic"
    results = [
        run_snapshot(
            path, args.output, extractor, provider, model, use_cache=not args.no_cache
        )
        for path in paths
    ]
    write_review(results, args.output)
    for result in results:
        counts = {}
        for record in result["records"]:
            counts[record["status"]] = counts.get(record["status"], 0) + 1
        print(json.dumps({"nct_id": result["nct_id"], "counts": counts}))
    print(f"Saved source-linked outputs and review table to {args.output}")


if __name__ == "__main__":
    main()
