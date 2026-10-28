"""Synthetic checks for the BigQuery fetcher; no cloud or MIMIC records required."""

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/fetch_mimic_cohort.py"
spec = importlib.util.spec_from_file_location("fetch_mimic_cohort", SCRIPT)
fetcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetcher)


def test_cohort_parameter_uses_typed_patient_and_datetime():
    parameter = fetcher.cohort_parameter(
        [{"subject_id": 123, "assessment_time": datetime(2100, 1, 2)}]
    ).to_api_repr()
    assert parameter["parameterType"]["type"] == "ARRAY"
    struct = parameter["parameterType"]["arrayType"]["structTypes"]
    assert [field["type"]["type"] for field in struct] == ["INT64", "DATETIME"]


def test_oversized_dry_run_does_not_execute(monkeypatch, tmp_path):
    client = Mock()
    client.get_dataset.return_value = SimpleNamespace(location="US")
    client.get_table.return_value = SimpleNamespace(schema=[])
    client.query.return_value = SimpleNamespace(total_bytes_processed=6 * 1024**3)
    monkeypatch.setattr(fetcher.bigquery, "Client", lambda **kwargs: client)
    monkeypatch.setattr(fetcher, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "fetch",
            "--dataset",
            "synthetic.hosp",
            "--output",
            str(tmp_path),
            "--execute",
        ],
    )
    with pytest.raises(RuntimeError, match="byte budget"):
        fetcher.main()
    assert client.query.call_count == 1
    assert client.query.call_args.kwargs["job_config"].dry_run
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["status"] == "partial_or_failed"
    assert not (tmp_path / "cohort.jsonl").exists()


def test_audit_rejects_incomplete_export(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"status": "partial_or_failed"}))
    with pytest.raises(ValueError, match="incomplete"):
        fetcher.audit_export(tmp_path)
