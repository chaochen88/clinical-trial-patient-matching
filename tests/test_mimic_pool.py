"""Pool identity and version checks use synthetic profiles only."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "combine_mimic_profile_pools",
    Path(__file__).resolve().parents[1] / "scripts/combine_mimic_profile_pools.py",
)
pool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool)


def source(root, pid, *, stale=False):
    (root / "profiles").mkdir(parents=True)
    profile = {
        "patient_id": f"mimiciv:{pid}",
        "profile_version": "v1",
        "facts": [{"original": {"source_format": "mimic_note"}}],
    }
    (root / "profiles" / f"{pid}.json").write_text(json.dumps(profile))
    (root / "documents.jsonl").write_text(
        json.dumps(
            {
                "patient_id": profile["patient_id"],
                "profile_version": "stale" if stale else "v1",
            }
        )
        + "\n"
    )
    return root


def test_pool_keeps_disjoint_sources_and_rejects_overlap(tmp_path):
    a = source(tmp_path / "a", 1)
    b = source(tmp_path / "b", 2)
    result = pool.combine([a, b], tmp_path / "combined")
    assert result["patients"] == result["patients_with_note_facts"] == 2
    assert result["profiles"][0]["profile_path"] == str(a / "profiles/1.json")
    with pytest.raises(ValueError, match="overlap"):
        pool.combine([a, a], tmp_path / "duplicate")
    assert not (tmp_path / "duplicate").exists()


def test_pool_rejects_stale_document_and_unfinished_notes(tmp_path):
    a = source(tmp_path / "a", 1, stale=True)
    with pytest.raises(ValueError, match="version"):
        pool.combine([a], tmp_path / "stale")
    (a / "enrichment_report.json").write_text('{"status":"running"}')
    with pytest.raises(ValueError, match="unfinished"):
        pool.combine([a], tmp_path / "unfinished")
