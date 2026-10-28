"""Build reproducible Class 6 cases from existing frozen public source files."""

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from clinical_trial_patient_matching.assessment import (
    Criterion,
    Trial,
    anthropic_interpreter,
    build_workflow,
)
from clinical_trial_patient_matching.criteria import Rule
from clinical_trial_patient_matching.extraction import bullets, digest
from clinical_trial_patient_matching.patients import (
    Claim,
    Profile,
    Record,
    build_profile,
    linked_fact,
)

SHARED = "1cfa5a70-7f3c-4227-5cf1-e182fcff4cd4"
CUTOFF = datetime.fromisoformat("2021-01-13T23:59:59-05:00")


def freeze_trial(path):
    raw = path.read_bytes()
    protocol = json.loads(raw)["protocolSection"]
    trial_id = protocol["identificationModule"]["nctId"]
    module = protocol["eligibilityModule"]
    criteria = []
    # Exact snapshot text is checked before applying these author-checked examples.
    mappings = {
        ("NCT03926806", "inclusion-1"): (
            "BMI 27-36",
            dict(
                kind="range",
                concept="BMI",
                lower=27,
                upper=36,
                lower_inclusive=True,
                upper_inclusive=True,
                unit="kg/m2",
            ),
        ),
        ("NCT03926806", "inclusion-2"): (
            "HbA1C \\< 8.5%",
            dict(kind="threshold", concept="HbA1c", operator="lt", value=8.5, unit="%"),
        ),
        ("NCT04450407", "inclusion-2"): (
            "Participants must have been using multiple daily injections without "
            "interruption for at least 3 months",
            dict(
                kind="duration",
                concept="multiple daily injections",
                operator="ge",
                value=3,
                unit="months",
                uninterrupted=True,
            ),
        ),
        ("NCT04450407", "inclusion-3"): (
            "Participants must have HbA1c values of 5.6% to 9.5%, inclusive",
            dict(
                kind="range",
                concept="HbA1c",
                lower=5.6,
                upper=9.5,
                lower_inclusive=True,
                upper_inclusive=True,
                unit="%",
            ),
        ),
        ("NCT04450407", "inclusion-4"): (
            "Participants must have a body mass index (BMI) of "
            "\u226435 kilograms per meter squared (kg/m\u00b2)",
            dict(
                kind="threshold", concept="BMI", operator="le", value=35, unit="kg/m2"
            ),
        ),
    }
    for bullet in bullets(module["eligibilityCriteria"]):
        key = trial_id, bullet["source_bullet_id"]
        rule = None
        if key in mappings:
            exact, fields = mappings[key]
            if bullet["text"] != exact:
                raise ValueError(f"Frozen wording changed: {key}; review mapping")
            rule = Rule(
                **fields,
                source_bullet_id=key[1],
                criterion_type=bullet["criterion_type"],
                source_quote=exact,
            )
        criteria.append(
            Criterion(
                criterion_id=bullet["source_bullet_id"],
                criterion_type=bullet["criterion_type"],
                predicate=bullet["text"],
                rule=rule,
                reviewed_mapping=rule is not None,
            )
        )
    metadata = {k: module[k] for k in ("minimumAge", "maximumAge") if k in module}
    if metadata:
        predicate = json.dumps(metadata, sort_keys=True)
        lower = metadata.get("minimumAge")
        upper = metadata.get("maximumAge")
        fields = None
        if lower and lower.endswith(" Years") and not upper:
            fields = dict(
                kind="threshold", operator="ge", value=float(lower.split()[0])
            )
        elif lower and upper and all(v.endswith(" Years") for v in metadata.values()):
            fields = dict(
                kind="range",
                lower=float(lower.split()[0]),
                upper=float(upper.split()[0]),
                lower_inclusive=True,
                upper_inclusive=True,
            )
        rule = (
            Rule(
                **fields,
                concept="age",
                unit="years",
                source_bullet_id="metadata-age",
                criterion_type="inclusion",
                source_quote=predicate,
            )
            if fields
            else None
        )
        criteria.append(
            Criterion(
                criterion_id="metadata-age",
                criterion_type="inclusion",
                predicate=predicate,
                rule=rule,
                reviewed_mapping=rule is not None,
            )
        )
    return Trial(
        trial_id=trial_id,
        trial_version=hashlib.sha256(raw).hexdigest(),
        source_reference=str(path),
        criteria=criteria,
    )


def presentation_profile(path):
    old = Profile.model_validate_json(path.read_text(encoding="utf-8"))
    text = next(f.original["text"] for f in old.facts if "text" in f.original)
    age = None
    # This is an exact example mapping, not a general age extractor.
    if "The patient is a 24-year-old man" in text:
        source = Record(
            patient_id=old.patient_id,
            variant=old.variant,
            dataset="pinned-TREC-2021",
            record_id=old.patient_id,
            text=text,
            version=old.facts[0].source_version,
        )
        age = linked_fact(
            source,
            Claim(
                concept="age",
                value=24,
                unit="years",
                assertion="affirmed",
                experiencer="patient",
                time_context="current presentation",
                quote="24-year-old",
            ),
            {"text": text},
        )
    return build_profile(
        [],
        None,
        {**old.configuration, "class6_example_age_mapping": "exact-source-span-v1"},
        old.facts + ([age] if age else []),
        old.findings,
    )


def example_interpreter(context, feedback):
    """Replay one transparent teaching interpretation; NOT an LLM experiment."""
    criterion = context["criterion"]
    quote = "after not taking his insulin for 3 days"
    evidence = next((f for f in context["facts"] if f["quote"] == quote), None)
    if (
        criterion["criterion_id"] == "inclusion-2"
        and evidence
        and "without interruption for at least 3 months" in criterion["predicate"]
    ):
        return dict(
            status="not_met",
            evidence_state="supported",
            citations=[dict(fact_id=evidence["fact_id"], quote=quote)],
            reason="The reported three-day interruption breaks the required "
            "continuous-use period at the described presentation.",
            follow_up="Confirm the dated medication-use history with the patient.",
        )
    return dict(
        status="unknown",
        evidence_state="missing",
        citations=[],
        reason="Teaching replay does not cover this narrative predicate.",
        follow_up="Obtain source evidence addressing the exact trial requirement.",
    )


def report_markdown(report):
    def cell(value):
        return " ".join(str(value).split()).replace("|", "&#124;")

    lines = [
        f"# {report.patient_id} ({report.variant})",
        "",
        f"Trial: {report.trial_id}. Overall: **{report.overall}**.",
        "",
        f"**Eligibility evidence score: {report.eligibility_score.value}/100**; "
        f"{report.eligibility_score.passed}/"
        f"{report.eligibility_score.applicable_total} "
        f"verified passes, {report.eligibility_score.failed} verified failures, "
        f"{report.eligibility_score.unresolved} unresolved. "
        f"Assessment coverage: {report.eligibility_score.coverage_percent}%.",
        "",
        "The score is an equal-weight evidence summary, not an eligibility "
        "probability. Unknown/unverified criteria earn no points; justified "
        "nonapplicable criteria are excluded. Mandatory conflicts override the score.",
        "",
        "This is a screening report, not an enrollment decision. "
        "Unverified narrative interpretations require human review.",
        "",
        "| Criterion | Type | Predicate status | Evidence | Verified "
        "| Reason | Follow-up |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report.results:
        lines.append(
            "| "
            + " | ".join(
                cell(v)
                for v in (
                    r.criterion_id,
                    r.criterion_type,
                    r.status,
                    r.evidence_state,
                    r.verified,
                    r.reason,
                    r.follow_up or "",
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/class6"))
    parser.add_argument(
        "--model", help="Optional live narrative interpretation of public cases"
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    trials = {
        nct: freeze_trial(Path(f"data/class5/sources/{nct}.json"))
        for nct in ("NCT03926806", "NCT04450407")
    }
    for nct, trial in trials.items():
        (args.output / f"{nct}-trial.json").write_text(trial.model_dump_json(indent=2))
    cases = [
        (
            "synthea-shared",
            Profile.model_validate_json(
                Path(f"data/class5/profiles/{SHARED}.json").read_text()
            ),
            trials["NCT03926806"],
            CUTOFF,
            "calendar",
        )
    ]
    for variant in ("original", "conflict"):
        cases.append(
            (
                f"trec-{variant}",
                presentation_profile(
                    Path(f"data/class4/trec-202129-{variant}-profile.json")
                ),
                trials["NCT04450407"],
                None,
                "described_presentation",
            )
        )
    from dotenv import load_dotenv

    load_dotenv()
    interpreter = (
        anthropic_interpreter(args.model) if args.model else example_interpreter
    )
    manifest = {
        "case_count": len(cases),
        "trial_versions": {key: value.trial_version for key, value in trials.items()},
        "reports": [],
    }
    for name, profile, trial, cutoff, basis in cases:
        (args.output / f"{name}-profile.json").write_text(
            profile.model_dump_json(indent=2)
        )
        for mode, provider in [
            ("rules-only", None),
            ("rules-plus-claude" if args.model else "rules-plus-replay", interpreter),
        ]:
            config = {
                "provider": "anthropic"
                if args.model and provider
                else "teaching-replay"
                if provider
                else "none",
                "model": args.model if provider and args.model else None,
                "temperature": 0,
                "max_tokens": 2048,
                "repair_limit": 1,
                "prompt_version": "0.1",
            }
            state = build_workflow(args.output / "reports", provider, config).invoke(
                dict(
                    profile=profile,
                    trial=trial,
                    assessment_time=cutoff,
                    time_basis=basis,
                )
            )
            report = state["report"]
            (args.output / f"{name}-{mode}.md").write_text(report_markdown(report))
            manifest["reports"].append(
                dict(
                    case=name,
                    mode=mode,
                    path=state["saved_to"],
                    report_key=report.report_key,
                    overall=report.overall,
                )
            )
            print(f"{name}/{mode}: {report.overall}", flush=True)
    manifest["manifest_version"] = digest(manifest)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
