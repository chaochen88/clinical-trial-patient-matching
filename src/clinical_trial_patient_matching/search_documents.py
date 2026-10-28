"""Time-frozen patient documents for retrieval, separate from eligibility decisions."""

import json
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field

from clinical_trial_patient_matching.extraction import digest
from clinical_trial_patient_matching.patients import (
    Claim,
    Profile,
    Record,
    StrictModel,
    build_profile,
    linked_fact,
    parse_fhir,
)


class SearchFact(StrictModel):
    """Compact retrieval projection; original quotes/records stay in the profile."""

    fact_id: str
    patient_id: str
    concept: str
    concept_system: str | None
    concept_code: str | None
    value: float | str | bool | None
    unit: str | None
    assertion: Literal["affirmed", "negated", "possible"]
    experiencer: Literal["patient", "family"]
    clinical_time: AwareDatetime | None
    available_time: AwareDatetime | None
    time_context: str
    source_status: str | None
    record_id: str
    source_hash: str


class SearchDocument(StrictModel):
    patient_id: str
    profile_version: str
    assessment_time: AwareDatetime
    text: str
    facts: list[SearchFact]
    conflicts: list[list[str]]
    questions: list[str]
    omitted: list[dict]
    document_version: str
    source_metadata: dict = Field(default_factory=dict)
    encoder_tokens: int = 0
    original_encoder_tokens: int = 0
    truncated: bool = False


def build_document(
    profile: Profile, assessment: datetime, metadata=None
) -> SearchDocument:
    if assessment.utcoffset() is None:
        raise ValueError("Assessment must have a timezone")
    if profile.variant != "original":
        raise ValueError(
            "Index variants separately; the corpus requires one patient once"
        )
    facts, omitted, questions = [], [], list(profile.findings)
    for fact in profile.facts:
        reason = None
        if fact.experiencer != "patient":
            reason = "family experiencer"
        elif fact.source_status in {"cancelled", "entered-in-error"}:
            reason = "invalid source status"
        elif any(
            t and t > assessment for t in (fact.clinical_time, fact.available_time)
        ):
            reason = "later than assessment"
        if reason:
            omitted.append({"fact_id": fact.fact_id, "reason": reason})
        else:
            facts.append(fact)
            if not fact.clinical_time:
                questions.append(f"Unknown clinical time: {fact.fact_id}")
    # Prefer recent facts within each concept, without discarding contradictory history.
    facts.sort(
        key=lambda f: (
            f.clinical_time.timestamp() if f.clinical_time else -1,
            f.fact_id,
        ),
        reverse=True,
    )
    lines = [f"Patient: {profile.patient_id}", f"Assessment: {assessment.isoformat()}"]
    # Put one recent fact per concept first, then retain all history below it.
    first, history, seen = [], [], set()
    for fact in facts:
        key = (fact.concept_system, fact.concept_code, fact.concept.casefold())
        (history if key in seen else first).append(fact)
        seen.add(key)
    first.sort(
        key=lambda f: (
            0
            if f.concept == "age"
            else 1
            if f.original.get("resourceType") == "Condition"
            or f.original.get("category") == "condition"
            else 2
            if f.concept in {"HbA1c", "BMI"}
            else 3
            if f.original.get("resourceType", "").startswith("Medication")
            or f.original.get("category") == "medication"
            else 4
        )
    )
    for fact in first + history:
        time = fact.clinical_time.isoformat() if fact.clinical_time else "unknown date"
        lines.append(
            f"{fact.assertion} | {fact.concept}: {fact.value} {fact.unit or ''} | "
            f"{time}; {fact.time_context} | source={fact.record_id}"
            + (
                ""
                if fact.original.get("resourceType")
                or fact.original.get("source_format") == "mimic_bigquery"
                else f" | {fact.quote}"
            )
        )
    for pair in profile.conflicts:
        questions.append(f"Conflicting evidence: {', '.join(pair)}")
    payload = dict(
        patient_id=profile.patient_id,
        profile_version=profile.profile_version,
        assessment_time=assessment.isoformat(),
        text="\n".join(lines),
        facts=[
            {
                key: value
                for key, value in f.model_dump(mode="json").items()
                if key in SearchFact.model_fields
            }
            for f in facts
        ],
        conflicts=profile.conflicts,
        questions=sorted(set(questions)),
        omitted=omitted,
        source_metadata=metadata or {},
    )
    return SearchDocument(**payload, document_version=digest(payload))


def profiles_from_fhir(
    bundle: dict, assessment: datetime, dataset: str
) -> list[Profile]:
    """Extend Class 4's Observations with source-linked demographics/history.

    Historical Conditions and MedicationRequests remain historical evidence, not
    claims of current disease or verified adherence. Unsupported records are findings.
    """
    observations, findings = parse_fhir(bundle, dataset)
    entries = bundle.get("entry", [])
    patients = {}
    resources = []
    for entry in entries:
        resource = entry.get("resource", {})
        resources.append(resource)
        if resource.get("resourceType") == "Patient":
            patient_id = resource["id"]
            for ref in (
                entry.get("fullUrl"),
                f"Patient/{patient_id}",
                f"urn:uuid:{patient_id}",
            ):
                if ref:
                    patients[ref] = patient_id
    facts = list(observations)
    for resource in resources:
        kind = resource.get("resourceType")
        if kind not in {
            "Patient",
            "Condition",
            "MedicationRequest",
            "MedicationStatement",
        }:
            continue
        patient_id = (
            resource.get("id")
            if kind == "Patient"
            else patients.get(resource.get("subject", {}).get("reference"))
        )
        if not patient_id:
            findings.append(f"{kind} {resource.get('id')}: unresolved patient")
            continue
        raw = json.dumps(resource, sort_keys=True)
        record = Record(
            patient_id=patient_id,
            dataset=dataset,
            record_id=resource["id"],
            text=raw,
            version=resource.get("meta", {}).get("versionId"),
        )
        try:
            if kind == "Patient":
                born = datetime.fromisoformat(resource["birthDate"]).date()
                today = assessment.date()
                age = (
                    today.year
                    - born.year
                    - ((today.month, today.day) < (born.month, born.day))
                )
                if age < 0:
                    raise ValueError("birth after assessment")
                claim = Claim(
                    concept="age",
                    value=age,
                    unit="years",
                    assertion="affirmed",
                    experiencer="patient",
                    clinical_time=assessment,
                    time_context="age computed at assessment from birthDate",
                    quote=json.dumps(resource["birthDate"]),
                )
                facts.append(linked_fact(record, claim, resource))
                continue
            code = (
                resource.get("code", {})
                if kind == "Condition"
                else resource.get("medicationCodeableConcept", {})
            )
            coding = code.get("coding", [])
            if not coding:
                raise ValueError("no coded concept")
            clinical = (
                resource.get("onsetDateTime")
                if kind == "Condition"
                else resource.get("authoredOn", resource.get("effectiveDateTime"))
            )
            context = f"{kind} history"
            # Avoid deriving current status from a future abatement/end date.
            end = resource.get("abatementDateTime") if kind == "Condition" else None
            if end and datetime.fromisoformat(end.replace("Z", "+00:00")) <= assessment:
                context += f"; ended {end}"
            for item in coding:
                verification = resource.get("verificationStatus", {}).get("coding", [])
                status = next((v.get("code") for v in verification), None)
                assertion = (
                    "negated"
                    if status == "refuted"
                    else (
                        "possible"
                        if status in {"unconfirmed", "provisional", "differential"}
                        else "affirmed"
                    )
                )
                claim = Claim(
                    concept=item.get("display", item["code"]),
                    concept_system=item.get("system"),
                    concept_code=item["code"],
                    assertion=assertion,
                    experiencer="patient",
                    clinical_time=clinical,
                    time_context=context,
                    quote=json.dumps(code, sort_keys=True),
                )
                # A dated history quote preserves source content, but future events
                # are excluded by build_document before any model sees them.
                facts.append(
                    linked_fact(
                        record,
                        claim,
                        resource,
                        "entered-in-error"
                        if status == "entered-in-error"
                        else resource.get("status"),
                    )
                )
        except (ValueError, KeyError, TypeError) as error:
            findings.append(f"{kind} {resource.get('id')}: {error}")
    result = []
    for patient_id in sorted(set(patients.values())):
        own = [fact for fact in facts if fact.patient_id == patient_id]
        if own:
            result.append(
                build_profile([], None, {"parser": "class5-fhir-0.1"}, own, findings)
            )
    return result


class Judgment(StrictModel):
    trial_id: str
    patient_id: str
    label: Literal["relevant", "irrelevant", "unresolved"]
    reviewer: str
    rationale: str
