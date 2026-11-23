"""MCP 2.x stdio wrapper; scope and permissions live in the shared backend."""

import argparse
import os
from pathlib import Path

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from clinical_trial_patient_matching.patient_tools import EvidenceBackend

mcp = MCPServer("Patient evidence", version="0.1")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)


def backend():
    return EvidenceBackend(Path(os.environ["PATIENT_EVIDENCE_CONFIG"]))


@mcp.tool(annotations=READ_ONLY)
def get_trial(trial_id: str) -> dict:
    """Read a scoped frozen trial."""
    return backend().call("get_trial", {"trial_id": trial_id}).model_dump(mode="json")


@mcp.tool(annotations=READ_ONLY)
def search_patients(query: str, limit: int = 10) -> dict:
    """Search concepts only inside the configured patient scope."""
    return (
        backend()
        .call(
            "search_patients",
            {
                "query": query,
                "limit": limit,
            },
        )
        .model_dump(mode="json")
    )


@mcp.tool(annotations=READ_ONLY)
def get_patient_record(patient_id: str, record_id: str) -> dict:
    """Read a scoped source record by its source ID and fixed cutoff."""
    return (
        backend()
        .call(
            "get_patient_record",
            {
                "patient_id": patient_id,
                "record_id": record_id,
            },
        )
        .model_dump(mode="json")
    )


@mcp.tool(annotations=READ_ONLY)
def get_patient_observations(
    patient_id: str,
    loinc_code: str,
    as_of: str,
    limit: int = 20,
) -> dict:
    """Read dated, sourced LOINC observations through the configured cutoff."""
    return (
        backend()
        .call(
            "get_patient_observations",
            {
                "patient_id": patient_id,
                "loinc_code": loinc_code,
                "as_of": as_of,
                "limit": limit,
            },
        )
        .model_dump(mode="json")
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.config:
        os.environ["PATIENT_EVIDENCE_CONFIG"] = str(args.config.resolve())
    backend()  # Fail closed before starting discovery.
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
