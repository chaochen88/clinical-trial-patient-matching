# Class 4 patient profiles

`patients.py` adds a shared Pydantic fact representation, deterministic FHIR
Observation parsing, structured Anthropic narrative extraction, exact quote
validation, one repair attempt in LangGraph, reconciliation, and bounded evidence
bundles. This stage prepares evidence; it does not decide eligibility.

Run from the repository root in the existing Pixi environment:

```bash
pixi run python scripts/prepare_class4.py
pixi run python -m clinical_trial_patient_matching.patients \
  --queries data/class4/sources/queries.jsonl \
  --fhir data/class4/sources/Maxwell782_Koepp521_6343cfd0-d47f-4a20-b561-b33d6441f372.json
```

The preparation command downloads the pinned TrialGPT revision and the exact
September 2019 Synthea archive from `class_material/class4.md`. It writes the
selected patient files and URL/content-hash manifest under `data/class4/sources`.
The Synthea archive is downloaded into memory; allow sufficient memory for it.
The local `data/synthea/json` sample is a different dataset and is not substituted.

Without preparation, the default command processes the existing local TREC case:

```bash
pixi run python -m clinical_trial_patient_matching.patients
```

For broader narrative extraction, append `--model YOUR_ANTHROPIC_MODEL` with
`ANTHROPIC_API_KEY` configured in the environment or `.env`. This sends the
selected narrative text to Anthropic. Offline mode makes no model calls and
extracts only four selected TREC claims plus the fictional addendum. Its explicit
coverage finding makes the profiles `partial`. Live results still require review
of meaning beyond schema and quote checks. No annotation targets enter prompts.

To use `ANTHROPIC_MODEL` and `ANTHROPIC_API_KEY` from the existing `.env` and save
each live run in a separate timestamped directory:

```bash
pixi run python scripts/run_class4_live.py
```

This launcher uses the prepared public source examples above and writes a run
manifest, profiles, and bundles under `data/class4/live/<UTC timestamp>/`.

Outputs under `data/class4` include profiles and three criterion bundles for:

- Original TREC patient `trec-202129`.
- Separately identified `conflict` variant with a fictional 8.2% HbA1c addendum
  labeled `synthetic_test_variant` in provenance.
- Synthea patient `338e0f93-2fb1-4152-8703-518686c567a8` when `--fhir` is supplied.

The bundles cover injections, HbA1c, and BMI. The frozen live trial extraction
under `data/class3/live` supplies trial identity, version, and source hash. It left
BMI for review, so the patient command constructs the BMI rule only when the
original frozen bullet exactly matches the documented wording and threshold.
It does not change the existing trial output. Use `--trial` for another frozen
extraction with these concepts. `--output` controls the destination.
Use `--assessment-time 2026-10-15T12:00:00-04:00` when a dated assessment is
appropriate; the undated TREC example should normally keep its relative basis.

Each fact retains patient/variant, dataset, record ID, version, content hash,
source status, original content, assertion, experiencer, clinical time, and
separate availability time. Repeated ingestion retains one set of facts. Distinct
observations remain separate even when values match. Changes to source content
or extraction configuration change profile and bundle versions; rerunning the
command rebuilds bundles. The current output filenames represent the latest run,
so use a separate output directory to retain earlier runs.

Reconciliation conservatively retains same-context differing claims as conflicts.
Different clinical dates remain historical observations. It does not establish
source precedence or resolve correction history automatically. A schema-valid
profile may contain unresolved conflicts. `validated` describes extraction checks,
not clinical correctness or complete patient history.

Bundle selection retains supporting and challenging evidence, excludes family
facts, invalidated/cancelled source observations, future dated/available facts,
and facts outside reviewed criterion windows when assessment time is supplied.
Unknown times and unreviewed window boundaries remain explicit gaps. No calendar
date is invented for the TREC note. Units are preserved; incompatible units
require review rather than an automatic conversion. Context limits record all
omitted fact IDs and reasons, with explicit truncation and coverage gaps.

The FHIR path currently supports quantity and string Observation values. Other
Observation forms are retained as processing findings and make the profile
partial; Conditions and Medication resources are outside this parser's coverage.
The full Synthea example produced 213 supported facts. This is a deliberately
bounded parser, not a complete FHIR clinical record importer.

## Source-versus-extraction review

These checks use the source records, independently of schema validity.

| Source evidence | Stored extraction | Review |
|---|---|---|
| Synthea Observation `78bccfed-bfd9-4bb6-9623-64f744a694ac`, LOINC `4548-4` | HbA1c 6.938225162130798%, effective `2018-11-17T06:52:03-05:00` | Value, system/code, patient reference and date agree with source |
| TREC: “concern for possible diabetic ketoacidosis” | Possible, patient experiencer, current presentation, unknown calendar date | Uncertainty preserved; no confirmed current diagnosis inferred |
| TREC: “after not taking his insulin for 3 days” | Affirmed interruption, relative three-day timing | Included with recent regimen in injection bundle |
| TREC recent glargine/aspart sentence | Multiple daily injections, exact contiguous quote | Regimen alone does not establish uninterrupted use |
| TREC: “A1c: 11.3%” | HbA1c 11.3%, current presentation | Original profile has no synthetic claim |
| Fictional addendum: “HbA1c for this screening encounter is 8.2%.” | HbA1c 8.2%, synthetic provenance, conflict variant only | Both claims retained, conflict flagged, no invented precedence |
| No BMI measurement in original TREC note | Empty BMI evidence and request for measured BMI or dated height/weight | Missing evidence remains unknown |

## Verification and remaining review

Run `pixi run test` and `pixi run check`. Additional lint targets:

```bash
pixi run ruff check tests/test_class4.py scripts/prepare_class4.py
```

The tests exercise original source examples, duplicate ingestion, conflict/trend
handling, negation, family experiencer, quote fidelity, repair/provider failures,
future records, incompatible units, missing BMI, context truncation, and mixed
patient rejection. Synthetic fixtures are labeled separately from published data.
`tests/fixtures/class4` contains the pinned single TREC row and an exact subset of
the Synthea Bundle (Patient plus the example Observation), with the download
manifest. Generated full profiles live in ignored `data/`, following repository
policy.

The class's issue link, PR creation, and independent human review remain repository
collaboration steps; this implementation does not claim that review occurred.
