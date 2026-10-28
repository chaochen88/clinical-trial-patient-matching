"""Synthetic engineering tests; no enrollment labels are inferred from retrieval."""

import json
from datetime import UTC, datetime

import numpy as np
import pytest

from clinical_trial_patient_matching.criteria import Rule
from clinical_trial_patient_matching.patients import (
    Claim,
    Record,
    build_profile,
    linked_fact,
)
from clinical_trial_patient_matching.retrieval import (
    TrialQuery,
    build_query,
    filter_documents,
    fuse,
    run,
)
from clinical_trial_patient_matching.retrieval_evaluation import metrics
from clinical_trial_patient_matching.search_documents import (
    build_document,
    profiles_from_fhir,
)

ASSESSMENT = datetime(2021, 1, 13, 23, 59, 59, tzinfo=UTC)


def document(patient_id="p", concept="BMI", value=30, unit="kg/m2", **updates):
    source = Record(
        patient_id=patient_id,
        dataset="synthetic_test_variant",
        record_id=f"{patient_id}-record",
        text="synthetic measurement",
    )
    claim = Claim.model_validate(
        {
            "concept": concept,
            "value": value,
            "unit": unit,
            "clinical_time": "2021-01-13T00:00:00Z",
            "assertion": "affirmed",
            "experiencer": "patient",
            "time_context": "measurement",
            "quote": source.text,
            **updates,
        }
    )
    profile = build_profile([], None, {}, [linked_fact(source, claim, {})])
    return build_document(profile, ASSESSMENT)


def query(filters=()):
    return TrialQuery(
        trial_id="SYNTHETIC",
        source_hash="synthetic",
        condition_terms=["T2DM"],
        inclusion_quotes=["BMI 27 to 36 kg/m2"],
        exclusion_checklist=["Exclude insulin users"],
        verified_filters=list(filters),
    )


def bmi_rule():
    return Rule(
        kind="range",
        concept="BMI",
        lower=27,
        upper=36,
        lower_inclusive=True,
        upper_inclusive=True,
        unit="kg/m2",
        criterion_type="inclusion",
        source_bullet_id="inclusion-1",
        source_quote="BMI 27 to 36 kg/m2",
    )


def test_rrf_duplicates_and_one_route():
    fused = fuse([["A", "A", "B"], ["B", "C"]])
    assert [r["patient_id"] for r in fused] == ["B", "A", "C"]
    assert fused[0]["score"] == pytest.approx(1 / 62 + 1 / 61)
    with pytest.raises(ValueError):
        fuse([], c=0)


def test_query_excludes_exclusions_and_expands_synonyms():
    built = build_query(query())
    assert "type 2 diabetes" in built.expanded_text
    assert "insulin" not in built.expanded_text
    assert built.exclusion_checklist == ["Exclude insulin users"]
    age_query = query().model_copy(
        update={
            "eligibility_metadata": {"minimumAge": "45 Years", "maximumAge": "75 Years"}
        }
    )
    assert "minimumAge: 45 Years" in build_query(age_query).original_text
    assert build_query(age_query).query_version != built.query_version
    bad = bmi_rule().model_copy(update={"criterion_type": "exclusion"})
    with pytest.raises(ValueError, match="inclusion"):
        build_query(query([bad]))


def test_missing_conflicting_units_and_verified_rejections():
    docs = [
        document("valid"),
        document("absent", concept="age"),
        document("unknown-date", clinical_time=None),
        document("wrong-unit", unit="kg"),
        document("failed", value=40),
        document("conflict", value=40),
    ]
    docs[-1].conflicts = [[docs[-1].facts[0].fact_id, "another-claim"]]
    accepted, rejected, questions = filter_documents(docs, query([bmi_rule()]))
    assert {d.patient_id for d in accepted} == {
        "valid",
        "absent",
        "unknown-date",
        "wrong-unit",
        "conflict",
    }
    assert rejected[0]["patient_id"] == "failed"
    assert rejected[0]["reasons"][0]["fact_id"]
    assert all(
        questions[pid] for pid in ("absent", "unknown-date", "wrong-unit", "conflict")
    )


def test_external_document_future_values_cannot_hard_reject():
    doc = document(value=40)
    doc.facts[0] = doc.facts[0].model_copy(
        update={"clinical_time": datetime(2022, 1, 1, tzinfo=UTC)}
    )
    accepted, rejected, questions = filter_documents([doc], query([bmi_rule()]))
    assert accepted and not rejected and questions[doc.patient_id]


def test_cutoffs_assertions_and_versions():
    assert not document(clinical_time="2022-01-01T00:00:00Z").facts
    source = Record(
        patient_id="late",
        dataset="synthetic_test_variant",
        record_id="late",
        text="BMI 30",
        available_time=datetime(2022, 1, 1, tzinfo=UTC),
    )
    late = linked_fact(
        source,
        Claim(
            concept="BMI",
            value=30,
            unit="kg/m2",
            assertion="affirmed",
            experiencer="patient",
            quote="BMI 30",
            time_context="synthetic",
            clinical_time=ASSESSMENT,
        ),
        {},
    )
    profile = build_profile([], None, {}, [late])
    assert not build_document(profile, ASSESSMENT).facts
    negated = document(assertion="negated")
    assert "negated" in negated.text
    family = document(experiencer="family")
    assert not family.facts
    assert document(value=28).document_version != document(value=29).document_version


class FakeModels:
    """Test double only: never reported as a semantic model baseline."""

    configuration = {"test_double": True}

    def bound_documents(self, documents):
        return documents

    def embeddings(self, documents, cache):
        return np.array([[1.0, 0.0] for _ in documents]), False

    def encode_query(self, text):
        return np.array([1.0, 0.0]), False

    def rerank(self, query, texts):
        return np.arange(len(texts), dtype=float), [False] * len(texts)


def test_graph_fanout_join_and_saved_provenance(tmp_path):
    docs = [document("a"), document("b")]
    result = run(
        docs, query(), FakeModels(), tmp_path, tmp_path / "cache", shortlist_k=1
    )
    saved = json.loads((tmp_path / "shortlist.json").read_text())
    assert saved == result
    assert len(result["shortlist"]) == 2
    assert sum(r["selected"] for r in result["shortlist"]) == 1
    assert all(r["evidence_references"] for r in result["shortlist"])
    assert result["search_coverage"]["input"] == 2
    with pytest.raises(ValueError, match="unique"):
        run([docs[0], docs[0]], query(), FakeModels(), tmp_path, tmp_path)


def test_metrics_incomplete_no_positives_and_dedup():
    labels = {"a": "relevant", "b": "irrelevant", "c": "relevant"}
    result = metrics(["a", "a", "b"], labels, k=2)
    assert result["recall@2"] == 0.5
    assert result["ndcg@2"] == pytest.approx(1 / (1 + 1 / np.log2(3)))
    partial = metrics(["a"], {"a": "relevant", "b": "unresolved"})
    assert partial["recall@10"] is None and partial["coverage"] == 0.5
    assert metrics([], {"a": "irrelevant"})["no_positive_pool"]


def test_patient_specific_cutoffs_are_explicit_and_time_bases_cannot_mix(tmp_path):
    docs = [document("a"), document("b")]
    docs[1].assessment_time = datetime(2022, 1, 1, tzinfo=UTC)
    for doc in docs:
        doc.source_metadata["time_basis"] = "mimic_shifted_clock_utc_tag_not_real_utc"
    with pytest.raises(ValueError, match="patient_specific"):
        run(docs, query(), FakeModels(), tmp_path, tmp_path)
    result = run(
        docs,
        query(),
        FakeModels(),
        tmp_path,
        tmp_path,
        patient_specific_assessments=True,
    )
    assert result["assessment_time"] is None and len(result["assessment_times"]) == 2
    docs[1].source_metadata["time_basis"] = "calendar"
    with pytest.raises(ValueError, match="time bases"):
        run(
            docs,
            query(),
            FakeModels(),
            tmp_path,
            tmp_path,
            patient_specific_assessments=True,
        )


def test_fhir_age_conditions_and_time_cutoff():
    bundle = {
        "entry": [
            {
                "fullUrl": "urn:uuid:p",
                "resource": {
                    "resourceType": "Patient",
                    "id": "p",
                    "birthDate": "1959-01-13",
                },
            },
            {
                "resource": {
                    "resourceType": "Condition",
                    "id": "c",
                    "subject": {"reference": "urn:uuid:p"},
                    "onsetDateTime": "2000-01-01T00:00:00Z",
                    "abatementDateTime": "2022-01-01T00:00:00Z",
                    "code": {
                        "coding": [
                            {
                                "system": "http://snomed.info/sct",
                                "code": "44054006",
                                "display": "Diabetes",
                            }
                        ]
                    },
                }
            },
        ]
    }
    profile = profiles_from_fhir(bundle, ASSESSMENT, "synthetic_test_variant")[0]
    doc = build_document(profile, ASSESSMENT)
    assert next(f.value for f in doc.facts if f.concept == "age") == 62
    assert "2022" not in doc.text and "Diabetes" in doc.text
    assert (
        next(f for f in doc.facts if f.concept == "Diabetes").time_context
        == "Condition history"
    )
