# Class 3 first test version

Run these commands from the repository root in WSL, using the existing Linux
Pixi environment:

```bash
pixi install
pixi run extract-demo
pixi run test
pixi run check
```

The demo processes the saved AACT snapshot at `class 1/trial_acct.json` and two
explicitly synthetic fixtures. Only one real snapshot is present in this repo.
The raw snapshot is kept locally and excluded from Git. On a fresh checkout,
the demo and tests use a synthetic coverage fixture in its place. To run live
extraction, provide a local snapshot explicitly or restore it at the default path.
The demo uses manually reviewed examples, without API calls. It extracts the
inclusive HbA1c range, uninterrupted injection duration, and hypoglycemia OR
group. Every other bullet remains in the output with a review finding.

Generated JSON and a review table are written to `data/class3/`, which is ignored
by Git. Records retain the original criteria, bullet text and IDs, exact quotes,
source version and SHA-256, schema/prompt versions, provider/model, inference
settings, UTC timestamp, all response attempts, and validation findings.
`validated` means schema and source checks passed; it does not establish clinical
semantic correctness or patient eligibility. Compare source and rules before use.

For live extraction, use the existing `.env` configuration:

```text
ANTHROPIC_API_KEY=<your existing key>
ANTHROPIC_MODEL=<your configured model>
```

```bash
pixi run extract-trial
# Supply three real snapshots when available:
pixi run python -m clinical_trial_patient_matching.extraction trial1.json trial2.json trial3.json
# Force a fresh run:
pixi run extract-trial --no-cache
```

The live adapter uses Claude through LangChain with explicit function calling.
LangGraph extracts, validates, and allows one repair attempt with feedback.
Invalid quotes, changed polarity, schema errors, refusals, truncations, and
provider failures are retained for review after at most two attempts. Unresolved
requirements never silently disappear. Cache reuse requires matching source,
schema definition/version, prompt contents/version, provider/model, and settings.

The rule representation supports ranges, thresholds, durations, conditions,
and recursive AND/OR groups. Numerical checks require matching units; missing
values are unknown. Exclusion applicability means screening failure. Event
windows use calendar months, clamping month-end dates. If a reviewer explicitly
confirms inclusive boundaries, the interval is closed at both ends; ambiguous
source boundaries remain unknown. Duration rules are represented but treatment
history assessment is a later step.

Before completing the full class deliverable, obtain two more real snapshots,
run live extraction, and manually review omitted requirements, polarity,
operators, boundaries, and ambiguity. A repository issue and independently
reviewed PR also remain to be created. Synthetic demo outputs do not fulfill
the requirement to review three real trials.

## First live run review

The configured Claude adapter processed all 11 bullets of `NCT04450407`.
Five passed schema/source checks and six were retained for review. This is
an engineering review of extraction fidelity, not a clinical sign-off.

| Example | Review outcome |
|---|---|
| HbA1c, inclusion-3 | Correct 5.6–9.5 percent, both inclusive; no invented recency window. |
| Hypoglycemia, exclusion-2 | Correct OR group and exclusion polarity; six-month windows on both children; boundary remains for review. |
| BMI, inclusion-4 | Model produced an incomplete range; rejected after the repair attempt. |
| eGFR and glucocorticoids | Quotes changed escaped source characters; rejected rather than silently normalized. |
| Other unresolved bullets | Causal and subjective wording and temporal boundary conventions require manual review. |

Full raw responses and findings are in `data/class3/live/`, excluded from Git.

Implementation references:
[LangGraph StateGraph](https://reference.langchain.com/python/langgraph/graph/state/StateGraph)
and [Claude structured output](https://docs.langchain.com/oss/python/integrations/chat/anthropic).
