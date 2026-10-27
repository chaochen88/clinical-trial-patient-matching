"""Class 5 hybrid patient retrieval. Scores rank evidence, not eligibility."""

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TypedDict

import numpy as np
from langgraph.graph import END, START, StateGraph
from pydantic import Field

from clinical_trial_patient_matching.criteria import Rule, evaluate_numeric
from clinical_trial_patient_matching.extraction import digest
from clinical_trial_patient_matching.patients import StrictModel
from clinical_trial_patient_matching.search_documents import SearchDocument

ENCODER = "sentence-transformers/all-MiniLM-L6-v2"
RERANKER = "cross-encoder/ms-marco-MiniLM-L6-v2"
SYNONYMS = {"t2dm": "type 2 diabetes", "body mass index": "BMI", "hba1c": "A1c"}


class TrialQuery(StrictModel):
    trial_id: str
    source_hash: str
    mode: str = "retrospective"
    condition_terms: list[str]
    inclusion_quotes: list[str]
    exclusion_checklist: list[str]
    verified_filters: list[Rule] = Field(default_factory=list)
    eligibility_metadata: dict = Field(default_factory=dict)
    original_text: str = ""
    expanded_text: str = ""
    query_version: str = ""


def build_query(query: TrialQuery) -> TrialQuery:
    if query.mode != "retrospective":
        raise ValueError("This lesson supports retrospective mode only")
    for rule in query.verified_filters:
        if rule.criterion_type != "inclusion" or not any(
            rule.source_quote in text for text in query.inclusion_quotes
        ):
            raise ValueError("Hard filters require a reviewed inclusion source quote")
        if rule.kind not in {"range", "threshold"} or rule.window:
            raise ValueError("Only reviewed non-window numeric filters are supported")
    age = [
        f"{key}: {value}"
        for key, value in query.eligibility_metadata.items()
        if key in {"minimumAge", "maximumAge"}
    ]
    original = "\n".join(query.condition_terms + age + query.inclusion_quotes)
    expansions = [
        value for key, value in SYNONYMS.items() if key in original.casefold()
    ]
    payload = query.model_dump(
        mode="json", exclude={"original_text", "expanded_text", "query_version"}
    )
    return query.model_copy(
        update={
            "original_text": original,
            "expanded_text": original + "\n" + " ".join(expansions),
            "query_version": digest(payload),
        }
    )


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.casefold())


def filter_documents(documents: list[SearchDocument], query: TrialQuery):
    accepted, rejections, questions = [], [], {}
    for doc in documents:
        gaps, failed = [], []
        for rule in query.verified_filters:
            candidates = [
                f
                for f in doc.facts
                if f.concept.casefold() == rule.concept.casefold()
                and f.assertion == "affirmed"
                and f.experiencer == "patient"
                and f.clinical_time is not None
                and f.clinical_time <= doc.assessment_time
                and (
                    f.available_time is None or f.available_time <= doc.assessment_time
                )
                and f.source_status not in {"cancelled", "entered-in-error"}
            ]
            if candidates:
                latest = max(f.clinical_time for f in candidates)
                candidates = [f for f in candidates if f.clinical_time == latest]
            conflict_ids = {item for pair in doc.conflicts for item in pair}
            if (
                not candidates
                or any(f.fact_id in conflict_ids for f in candidates)
                or len({(f.value, f.unit) for f in candidates}) != 1
            ):
                gaps.append(f"Missing or conflicting {rule.concept}; retain for review")
                continue
            fact = candidates[0]
            # bool is a Python int but is not a numeric clinical measurement.
            if isinstance(fact.value, bool) or not isinstance(fact.value, (float, int)):
                gaps.append(f"Non-numeric {rule.concept}; retain for review")
                continue
            outcome = evaluate_numeric(rule, fact.value, fact.unit)
            if outcome == "not_met":
                failed.append(
                    {
                        "criterion_id": rule.source_bullet_id,
                        "fact_id": fact.fact_id,
                        "value": fact.value,
                        "unit": fact.unit,
                        "reason": "verified numeric contradiction",
                    }
                )
            elif outcome != "met":
                gaps.append(f"Unit review needed for {rule.concept}")
        if failed:
            rejections.append({"patient_id": doc.patient_id, "reasons": failed})
        else:
            accepted.append(doc)
            questions[doc.patient_id] = gaps
    return accepted, rejections, questions


def ordered_scores(ids, scores, limit):
    values = np.asarray(scores, dtype=float).reshape(-1)
    if len(ids) != len(values) or not np.isfinite(values).all():
        raise ValueError("Model scores must be finite and aligned with patient IDs")
    return [
        {"patient_id": pid, "score": float(score), "rank": rank}
        for rank, (pid, score) in enumerate(
            sorted(zip(ids, values, strict=True), key=lambda pair: (-pair[1], pair[0]))[
                :limit
            ],
            1,
        )
    ]


def fuse(rankings, c=60):
    if c <= 0:
        raise ValueError("RRF constant must be positive")
    scores = {}
    for ranking in rankings:
        for rank, pid in enumerate(dict.fromkeys(ranking), 1):
            scores[pid] = scores.get(pid, 0) + 1 / (c + rank)
    return [
        {"patient_id": pid, "score": score, "rank": rank}
        for rank, (pid, score) in enumerate(
            sorted(scores.items(), key=lambda p: (-p[1], p[0])), 1
        )
    ]


class Models:
    """Local inference with pinned revisions, token limits, and embedding cache."""

    def __init__(self, encoder_revision: str, reranker_revision: str):
        if not all(
            re.fullmatch(r"[0-9a-f]{40}", rev)
            for rev in (encoder_revision, reranker_revision)
        ):
            raise ValueError("Use immutable 40-character model commit revisions")
        from sentence_transformers import CrossEncoder, SentenceTransformer

        self.encoder = SentenceTransformer(
            ENCODER, revision=encoder_revision, device="cpu"
        )
        self.reranker = CrossEncoder(RERANKER, revision=reranker_revision, device="cpu")
        self.configuration = {
            "encoder": ENCODER,
            "encoder_revision": encoder_revision,
            "reranker": RERANKER,
            "reranker_revision": reranker_revision,
            "encoder_tokens": self.encoder.max_seq_length,
            "pair_tokens": (
                getattr(self.reranker, "max_seq_length", None)
                or getattr(self.reranker, "max_length", None)
                or 512
            ),
            "device": "cpu",
            "normalized_embeddings": True,
        }

    def bound_documents(self, documents):
        bounded = []
        tokenizer = self.encoder.tokenizer
        limit = self.encoder.max_seq_length - tokenizer.num_special_tokens_to_add(
            pair=False
        )
        for doc in documents:
            ids = tokenizer.encode(doc.text, add_special_tokens=False, verbose=False)
            text = doc.text if len(ids) <= limit else tokenizer.decode(ids[:limit])
            payload = {
                "source": doc.document_version,
                "text": text,
                "model": self.configuration,
            }
            bounded.append(
                doc.model_copy(
                    update={
                        "text": text,
                        "encoder_tokens": min(len(ids), limit),
                        "original_encoder_tokens": len(ids),
                        "truncated": len(ids) > limit,
                        "document_version": digest(payload),
                        "questions": doc.questions
                        + (
                            ["Search text truncated; full structured facts retained"]
                            if len(ids) > limit
                            else []
                        ),
                    }
                )
            )
        return bounded

    def embeddings(self, documents, cache: Path):
        cache.mkdir(parents=True, exist_ok=True)
        key = digest(
            {
                "documents": [(d.patient_id, d.document_version) for d in documents],
                "model": self.configuration,
            }
        )
        path = cache / f"{key}.npy"
        if path.exists():
            matrix = np.load(path, allow_pickle=False)
            if matrix.shape[0] == len(documents) and np.isfinite(matrix).all():
                return matrix, True
        matrix = self.encoder.encode(
            [d.text for d in documents],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        np.save(path, matrix, allow_pickle=False)
        return matrix, False

    def encode_query(self, text):
        tokens = self.encoder.tokenizer.encode(
            text, add_special_tokens=True, verbose=False
        )
        return self.encoder.encode(
            [text], normalize_embeddings=True, convert_to_numpy=True
        )[0], len(tokens) > self.encoder.max_seq_length

    def rerank(self, query, texts):
        limit = self.configuration["pair_tokens"]
        truncated = [
            len(self.reranker.tokenizer.encode(query, text, verbose=False)) > limit
            for text in texts
        ]
        scores = self.reranker.predict(
            [(query, text) for text in texts], show_progress_bar=False
        )
        return scores, truncated


class RetrievalState(TypedDict, total=False):
    query: TrialQuery
    documents: list[SearchDocument]
    query_result: TrialQuery
    lexical: list[dict]
    vector: list[dict]
    lexical_seconds: float
    vector_seconds: float
    vector_query_truncated: bool
    fused: list[dict]
    shortlist: list[dict]
    reranker_seconds: float
    saved_to: str


def build_workflow(
    models,
    embeddings,
    output: Path,
    questions: dict,
    retrieval_k=50,
    shortlist_k=20,
    c=60,
):
    if min(retrieval_k, shortlist_k) <= 0:
        raise ValueError("Candidate limits must be positive")

    def query_node(state):
        return {"query_result": build_query(state["query"])}

    def lexical(state):
        from rank_bm25 import BM25Okapi

        start = perf_counter()
        docs = state["documents"]
        corpus = [tokenize(d.text) or ["empty"] for d in docs]
        scores = BM25Okapi(corpus).get_scores(
            tokenize(state["query_result"].expanded_text)
        )
        return {
            "lexical": ordered_scores(
                [d.patient_id for d in docs], scores, retrieval_k
            ),
            "lexical_seconds": perf_counter() - start,
        }

    def vector(state):
        start = perf_counter()
        query_vector, truncated = models.encode_query(
            state["query_result"].expanded_text
        )
        scores = embeddings @ query_vector
        return {
            "vector": ordered_scores(
                [d.patient_id for d in state["documents"]], scores, retrieval_k
            ),
            "vector_query_truncated": truncated,
            "vector_seconds": perf_counter() - start,
        }

    def fusion(state):
        return {
            "fused": fuse(
                [
                    [r["patient_id"] for r in state[key]]
                    for key in ("lexical", "vector")
                ],
                c,
            )[:retrieval_k]
        }

    def rerank(state):
        start = perf_counter()
        by_id = {d.patient_id: d for d in state["documents"]}
        candidates = [row["patient_id"] for row in state["fused"]]
        scores, truncated = models.rerank(
            state["query_result"].expanded_text, [by_id[pid].text for pid in candidates]
        )
        limits = dict(zip(candidates, truncated, strict=True))
        ranks = {
            key: {r["patient_id"]: r for r in state[key]}
            for key in ("lexical", "vector", "fused")
        }
        shortlist = []
        # Keep all reranked candidates for Recall@k evaluation, plus selection flags.
        for row in ordered_scores(candidates, scores, len(candidates)):
            pid = row["patient_id"]
            doc = by_id[pid]
            shortlist.append(
                {
                    **row,
                    "selected": row["rank"] <= shortlist_k,
                    "profile_version": doc.profile_version,
                    "document_version": doc.document_version,
                    "retrieval": {key: table.get(pid) for key, table in ranks.items()},
                    "evidence_references": [f.fact_id for f in doc.facts],
                    "questions": doc.questions + questions.get(pid, []),
                    "document_truncated": doc.truncated,
                    "pair_truncated": limits[pid],
                }
            )
        return {"shortlist": shortlist, "reranker_seconds": perf_counter() - start}

    def save(state):
        output.mkdir(parents=True, exist_ok=True)
        result = {
            key: state[key]
            for key in (
                "lexical",
                "vector",
                "fused",
                "shortlist",
                "lexical_seconds",
                "vector_seconds",
                "reranker_seconds",
                "vector_query_truncated",
            )
        }
        result.update(
            query=state["query_result"].model_dump(mode="json"),
            created_at=datetime.now(UTC).isoformat(),
            models=models.configuration,
            corpus_count=len(state["documents"]),
            settings={
                "retrieval_k": retrieval_k,
                "shortlist_k": shortlist_k,
                "rrf_c": c,
            },
            score_meaning="Retrieval relevance only; not eligibility probabilities",
            candidate_counts={
                key: len(state[key])
                for key in ("lexical", "vector", "fused", "shortlist")
            },
        )
        destination = output / "shortlist.json"
        destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return {"saved_to": str(destination)}

    graph = StateGraph(RetrievalState)
    for name, action in (
        ("build_query", query_node),
        ("retrieve_lexical", lexical),
        ("retrieve_vector", vector),
        ("fuse", fusion),
        ("rerank", rerank),
        ("save_shortlist", save),
    ):
        graph.add_node(name, action)
    graph.add_edge(START, "build_query")
    graph.add_edge("build_query", "retrieve_lexical")
    graph.add_edge("build_query", "retrieve_vector")
    graph.add_edge(["retrieve_lexical", "retrieve_vector"], "fuse")
    graph.add_edge("fuse", "rerank")
    graph.add_edge("rerank", "save_shortlist")
    graph.add_edge("save_shortlist", END)
    return graph.compile()


def run(
    documents,
    query,
    models,
    output: Path,
    cache: Path,
    retrieval_k=50,
    shortlist_k=20,
    patient_specific_assessments=False,
):
    ids = [d.patient_id for d in documents]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Corpus must be nonempty with unique patient IDs")
    if len({d.source_metadata.get("time_basis", "calendar") for d in documents}) != 1:
        raise ValueError("Cannot mix different time bases in one corpus")
    if (
        len({d.assessment_time for d in documents}) != 1
        and not patient_specific_assessments
    ):
        raise ValueError(
            "Use patient_specific_assessments for patient-specific cutoffs"
        )
    start = perf_counter()
    query = build_query(query)
    accepted, rejections, questions = filter_documents(documents, query)
    output.mkdir(parents=True, exist_ok=True)
    (output / "filter_audit.json").write_text(
        json.dumps(
            {
                "input_count": len(documents),
                "retained_count": len(accepted),
                "rejections": rejections,
                "questions": questions,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if not accepted:
        raise ValueError("No candidates remain; see filter_audit.json")
    bounded = models.bound_documents(accepted)
    with (output / "documents.jsonl").open("w", encoding="utf-8") as handle:
        for doc in bounded:
            handle.write(doc.model_dump_json() + "\n")
    matrix, hit = models.embeddings(bounded, cache)
    indexing = perf_counter() - start
    state = build_workflow(
        models, matrix, output, questions, retrieval_k, shortlist_k
    ).invoke({"query": query, "documents": bounded})
    result_path = Path(state["saved_to"])
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.update(
        indexing_seconds=indexing,
        rejected_patient_ids=[row["patient_id"] for row in rejections],
        embedding_cache_hit=hit,
        assessment_time=None
        if patient_specific_assessments
        else bounded[0].assessment_time.isoformat(),
        assessment_times={d.patient_id: d.assessment_time.isoformat() for d in bounded}
        if patient_specific_assessments
        else None,
        time_basis=bounded[0].source_metadata.get("time_basis", "calendar"),
        corpus_version=digest([(d.patient_id, d.document_version) for d in bounded]),
        inference_cost={
            "external_api_calls": 0,
            "api_cost_usd": 0,
            "local_compute_cost_usd": None,
        },
        search_coverage={
            "input": len(documents),
            "after_filters": len(bounded),
            "retrieved": len(state["fused"]),
            "shortlisted": sum(r["selected"] for r in state["shortlist"]),
        },
    )
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--documents", type=Path, default=Path("data/class5/documents.jsonl")
    )
    parser.add_argument("--query", type=Path, default=Path("data/class5/query.json"))
    parser.add_argument("--models", type=Path, default=Path("data/class5/models.json"))
    parser.add_argument("--output", type=Path, default=Path("data/class5/run"))
    parser.add_argument("--cache", type=Path, default=Path("data/class5/embeddings"))
    parser.add_argument("--patient-specific-assessments", action="store_true")
    args = parser.parse_args()
    docs = [
        SearchDocument.model_validate_json(line)
        for line in args.documents.read_text(encoding="utf-8").splitlines()
    ]
    query = TrialQuery.model_validate_json(args.query.read_text(encoding="utf-8"))
    config = json.loads(args.models.read_text(encoding="utf-8"))
    start = perf_counter()
    models = Models(config["encoder_revision"], config["reranker_revision"])
    startup = perf_counter() - start
    result = run(
        docs,
        query,
        models,
        args.output,
        args.cache,
        patient_specific_assessments=args.patient_specific_assessments,
    )
    result["model_startup_seconds"] = startup
    (args.output / "shortlist.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(
        f"Saved {result['search_coverage']['shortlisted']} candidates to {args.output}"
    )


if __name__ == "__main__":
    main()
