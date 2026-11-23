"""Bounded live-pilot selection checks using synthetic profiles only."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_runner():
    path = Path(__file__).parents[1] / "scripts/run_mimic_pipeline.py"
    spec = importlib.util.spec_from_file_location("mimic_pipeline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_pilot_uses_ranking_selected_flags_and_matching_profile_versions():
    module = load_runner()
    profiles = {
        pid: SimpleNamespace(
            profile_version="v1",
            facts=[
                SimpleNamespace(
                    original={
                        "source_format": "mimic_note"
                        if pid != "a"
                        else "mimic_bigquery"
                    }
                )
            ],
        )
        for pid in ("a", "b", "c", "d", "e")
    }
    rows = [
        dict(patient_id=pid, rank=i, selected=pid != "d", profile_version="v1")
        for i, pid in enumerate(profiles, 1)
    ]
    shortlist = {"shortlist": list(reversed(rows))}
    assert module.select_pilot(shortlist, profiles, 3) == ["b", "c", "e"]
    assert module.select_pilot(shortlist, profiles, 1) == ["b"]
    assert module.select_pilot(shortlist, profiles, 40, True) == ["a", "b", "c", "e"]
    for limit in (0, 4):
        with pytest.raises(ValueError):
            module.select_pilot(shortlist, profiles, limit)
    rows[0]["profile_version"] = "wrong-version"
    with pytest.raises(ValueError, match="version"):
        module.select_pilot(shortlist, profiles, 3)


def test_shared_pool_loader_rejects_modified_source_profile(tmp_path):
    import hashlib
    import json

    module = load_runner()
    path = tmp_path / "profile.json"
    path.write_text("{}")
    manifest = {
        "profiles": [
            {
                "profile_path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        ]
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    path.write_text('{"modified":true}')
    with pytest.raises(ValueError, match="hash changed"):
        module.load_profiles(tmp_path)


def test_continuation_reuses_only_matching_patient_context_and_feedback():
    module = load_runner()
    calls = []

    def live(context, feedback):
        calls.append((context, feedback))
        return {"fresh": True}

    saved = [
        {
            "patient_id": "a",
            "context": {"criterion": "one"},
            "feedback": None,
            "raw": {},
            "parsed": {"saved": True},
        }
    ]
    invoke = module.replay_then_live("a", saved, live)
    assert invoke({"criterion": "one"}, None) == {"saved": True}
    assert not calls
    assert invoke({"criterion": "one"}, "changed feedback") == {"fresh": True}
    other = module.replay_then_live("b", saved, live)
    assert other({"criterion": "one"}, None) == {"fresh": True}
