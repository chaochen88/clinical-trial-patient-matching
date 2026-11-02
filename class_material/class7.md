# Class 7 — Agentic Workflow, Tool Calling, and MCP

Extend the screening workflow so it can choose an evidence lookup, execute a validated tool call, and reassess the affected criterion. LangGraph controls state and routing; tools provide access to trial and patient records.

## 1. Choose the Next Action from Missing Evidence

**Example — intentionally omit BMI from the initial evidence bundle** for the November 2021 [Synthea patient][fhir] `1cfa5a70-7f3c-4227-5cf1-e182fcff4cd4`. Trial [NCT03926806][trial] requires BMI 27–36.

```text
Assessment: BMI unknown
Next action: look up BMI observations for this patient before screening
Tool result: BMI 29.38 kg/m², measured 2021-01-13
Reassessment: BMI criterion met; continue the remaining checks
```

- Let the model select from explicit actions: request evidence or finish with the current findings. Application code validates and executes the proposed action.
- Prompt it to identify the unresolved criterion, the evidence needed, and the appropriate tool. Existing sufficient evidence should proceed directly to assessment.
- A lookup that finds no usable record leaves the criterion unresolved and produces a specific follow-up question.

## 2. Define a Small Read-Only Tool Interface

**Example — arguments to `get_patient_observations`:**

```json
{
  "patient_id": "1cfa5a70-7f3c-4227-5cf1-e182fcff4cd4",
  "loinc_code": "39156-5",
  "as_of": "2021-01-13T23:59:59-05:00"
}
```

The matching source includes Observation `e735114d-c4f1-af47-ca8f-6450697f5110`, value `29.38`, unit `kg/m2`, and clinical time `2021-01-13T23:22:51-05:00`.

- Start with `get_trial`, `search_patients`, `get_patient_record`, and `get_patient_observations`. Give each a narrow input schema and a description of what it returns.
- Enforce the authorized patient scope, code validation, and fixed assessment time in the backend. Use parameterized database queries; credentials stay in application configuration.
- Return source IDs, dates, values/units, and coverage metadata. Distinguish `ok`, `not_found`, `truncated`, and `error`; the caller must recognize incomplete results.

## 3. Execute Tool Calls and Bound the Loop

**Example — route after verifying a criterion assessment:**

```python
def next_step(state):
    if not state["has_unresolved_criteria"]:
        return "aggregate"
    if state["tool_calls_used"] >= state["tool_call_limit"]:
        return "aggregate"
    if not state["has_new_lookup"]:
        return "aggregate"
    return "choose_tool"
```

- Connect `assess → verify → route`. The evidence branch runs `choose_tool → validate_call → execute_tool → assess`. Aggregation preserves unknowns and processing failures.
- Bind tool schemas to the model. For each proposed call, validate arguments, reserve budget, execute the function, and return a tool-result message with the matching call ID. Give rejected calls an explicit error result. [Tool-calling pattern][quickstart]
- Begin with at most two additional evidence rounds and four tool attempts per patient, including retries, plus a run-wide limit. Count every call in a multi-call response before dispatch; cache identical calls by arguments and source version. Set model-call limits and a graph recursion limit as additional bounds.

## 4. Run Independent Checks in Parallel

**Example — BMI and HbA1c checks** can read the same frozen profile independently and return results keyed by `(patient_id, criterion_id)`.

- Fan out independent checks with LangGraph's `Send` or parallel branches, then merge results through an explicit reducer. Preserve identifiers and source versions in every branch.
- Start with four concurrent checks. Reserve shared call budget before scheduling work; keep identical evidence requests from producing duplicate backend reads.
- Aggregate only after each expected check has a result or explicit failure status. A timed-out branch remains visible as incomplete processing. [Graph state and parallel execution][graph]

## 5. Expose the Same Backend Through MCP

**MCP—Model Context Protocol—** standardizes how a client discovers and calls tools provided by a server.

**Example — a server wrapper around your validated `read_observations` function:**

```python
from mcp.server import MCPServer
from patient_tools import read_observations

mcp = MCPServer("Patient evidence")


@mcp.tool()
def get_patient_observations(
    patient_id: str, loinc_code: str, as_of: str
) -> dict:
    """Read sourced observations for an authorized patient through as_of."""
    return read_observations(patient_id, loinc_code, as_of)
```

Create `patient_tools.py` with the validated read-only backend from section 2 and save the wrapper as `patient_mcp_server.py`. This example uses the official Python SDK **2.x**: `pixi add --pypi "mcp[cli]>=2,<3"`. Record the resolved version in the lockfile. [SDK documentation][sdk]

- Test discovery and the BMI call with `pixi run mcp dev patient_mcp_server.py`. Connect the graph's tool executor through an SDK client using local stdio; keep the same validation and result contract.
- Direct path: `graph → Python function`. MCP path: `graph → MCP client → server → same function`. Compare the returned evidence after normalizing transport envelopes.
- Enforce read-only access in the backend and database permissions. Tool annotations describe behavior; access restrictions require implementation. Preserve server errors and truncation when returning results to the graph. [MCP tools][mcp-tools]

## 6. Compare Behavior, Cost, and Failure Handling

**Example — an initially missing BMI is also absent from the underlying test record:** after the permitted lookup, the workflow returns `unknown` with a request for a dated measurement. It must finish within its configured budget.

- Run the same cases through a fixed evidence workflow and the agentic workflow. Compare criterion results, recovered evidence, unnecessary calls, latency, token usage, and cost.
- Separately compare direct and MCP execution using identical tool arguments and source versions. Both should return equivalent evidence and preserve errors.
- Test sufficient evidence, recoverable missing data, genuinely absent data, conflicting records, repeated requests, and invalid/out-of-scope arguments. Log actions, call IDs, source references, and stop reasons.

## Deliverables

- A LangGraph screening workflow with validated tool execution, conditional routing, and enforced budgets.
- Read-only tool contracts and one working MCP server/client connection.
- Parallel criterion checks with deterministic result merging and explicit failure coverage.
- A comparison report covering fixed versus agentic behavior and direct versus MCP execution.
- A reviewed PR linked to an issue in your repository; Ruff and pytest passing.

## Sources

- Patient: [November 2021 FHIR][fhir] and [November 2021 CSV][csv]. Shared example: `Alaine226_Willms744_1cfa5a70-7f3c-4227-5cf1-e182fcff4cd4.json`; preserve each export's precision and identifiers.
- Trial: [NCT03926806][trial]; use the frozen criteria for retrospective screening.
- Implementation: [LangGraph tool-calling quickstart][quickstart], [Graph API][graph], [MCP Python SDK][sdk], and [MCP tool specification][mcp-tools].

[fhir]: https://synthetichealth.github.io/synthea-sample-data/downloads/synthea_sample_data_fhir_r4_nov2021.zip
[csv]: https://synthetichealth.github.io/synthea-sample-data/downloads/synthea_sample_data_csv_nov2021.zip
[trial]: https://clinicaltrials.gov/study/NCT03926806
[quickstart]: https://docs.langchain.com/oss/python/langgraph/quickstart
[graph]: https://docs.langchain.com/oss/python/langgraph/graph-api
[sdk]: https://py.sdk.modelcontextprotocol.io/
[mcp-tools]: https://modelcontextprotocol.io/specification/2025-11-25/server/tools
