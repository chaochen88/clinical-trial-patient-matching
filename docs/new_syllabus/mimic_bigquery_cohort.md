# Initial MIMIC-IV BigQuery cohort

`scripts/fetch_mimic_cohort.py` fetches a bounded retrospective evidence sample
for the type-2-diabetes trial `NCT03926806`. It is a structured SQL exporter;
it does not invoke Claude or generate patient eligibility decisions.

The initial local export is in `data/mimic/NCT03926806/initial-20`. This directory
is ignored by Git, as are other raw datasets. Source records, cohort IDs, and
derived patient-level coverage stay there rather than in test fixtures or logs.

## Selection and assessment basis

- Select up to 20 distinct adult patients, ordered by `subject_id`, with an
  admission containing a dictionary diagnosis title mentioning diabetes and
  type 2/II. For each patient select the latest completed admission meeting that
  condition. This is a convenience sample, not a representative evaluation pool.
- Some ICD-9 dictionary descriptions include unspecified diabetes type. This
  broad search identifies candidates; review the codes before claiming confirmed
  type 2 diabetes. It is not a clinically validated cohort phenotype.
- Use the selected discharge as the retrospective assessment cutoff. Do not
  select only patients satisfying the trial's age, BMI, HbA1c, or drug criteria.
- Retrieve billing diagnoses from completed admissions up to the cutoff and
  one year of relevant measurements and medication history. This is not complete
  lifetime or outpatient history.
- Preserve MIMIC's shifted `DATETIME` values. They are not real-world UTC times,
  and should not be fed directly into the existing timezone-aware fact schema
  without an explicitly designed MIMIC time representation.

## Files

| File | Contents |
|---|---|
| `cohort.jsonl` | Selected patients, admission IDs, demographics, cutoff, approximate age |
| `lab_dictionary.jsonl` | Blood HbA1c-related and creatinine lab definitions used by the query |
| `diagnoses.jsonl` | Prior/current completed-admission ICD codes and descriptions |
| `labs.jsonl` | Relevant values, units, measurement/store times, and source IDs |
| `measurements.jsonl` | BMI/height/weight OMR records, retaining original result names/values |
| `prescriptions.jsonl` | Dated prescriptions, drug names, doses, forms, and routes |
| `administrations.jsonl` | Medication events, including event descriptions and store times |
| `manifest.json` | Dataset, schemas, SQL hashes, jobs, estimates, billed bytes, hashes, and gaps |
| `coverage.json` | Local patient-level coverage and aggregate audit summary |
| `*.sql` | Exact parameterized SQL used; patient IDs remain parameters |

Lab and administration queries exclude later known store times. OMR has only
date precision, so within-day availability is unresolved. Billing discharge times
are a retrospective grouping basis, not verified coding availability or diagnosis
onset dates. Prescriptions do not verify adherence or stable outpatient doses;
their recorded stop times may incorporate later updates. Diet, supplements,
alcohol and allergy exclusions are not comprehensively covered. Missing evidence
must remain unknown during later assessment.

## Run and audit

BigQuery support is provided by `google-cloud-bigquery`. Use Application Default
Credentials for the Google account with access to the exact versioned dataset.
No service account key is stored in this repository.

Inspect metadata without reading patient records:

```bash
pixi run python scripts/fetch_mimic_cohort.py \
  --project YOUR_PROJECT_ID \
  --dataset physionet-data.mimiciv_3_1_hosp --inspect
```

Without `--execute`, the script only estimates the cohort query. Each additional
query is dry-run before execution once cohort parameters have been obtained.
BigQuery can scan a large table even for a small cohort; `LIMIT` does not serve
as a byte budget. An oversized estimate fails before executing that query and
retains a partial-run manifest. Every executed job has a maximum-bytes-billed cap;
the cumulative estimate also has a configurable limit.

```bash
pixi run python scripts/fetch_mimic_cohort.py \
  --project YOUR_PROJECT_ID \
  --dataset physionet-data.mimiciv_3_1_hosp \
  --patients 20 --max-gib-per-query 10 --max-gib-total 20 \
  --execute
```

Default outputs use a timestamped directory. Use `--output` to choose a directory;
rerunning into an existing directory replaces its exports and manifest. Prefer
a new directory to retain earlier run history.

Audit an existing completed export locally without creating BigQuery jobs:

```bash
pixi run python scripts/fetch_mimic_cohort.py \
  --output data/mimic/NCT03926806/initial-20 --audit-only
```

The audit verifies file hashes/counts, unique cohort membership, and clinical /
known availability cutoffs. It reports whether evidence records exist, not whether
values are clinically valid or the patient qualifies. Synthetic tests verify typed
parameters, budget rejection before execution, and incomplete-export handling.

## Next integration step

The deterministic adapter is now in
`src/clinical_trial_patient_matching/mimic_adapter.py`. It converts the completed
export into the existing `Profile` and `SearchDocument` schemas, without BigQuery,
Claude, embedding-model, or other network calls:

```bash
pixi run mimic-profiles
# Or choose source/output directories:
pixi run python -m clinical_trial_patient_matching.mimic_adapter \
  --source data/mimic/NCT03926806/initial-20 \
  --output data/mimic/NCT03926806/processed-20
```

Outputs under `processed-20` are `profiles/<subject_id>.json`, `documents.jsonl`,
and `processing_report.json`. The report records adapter version, source manifest
hash, aggregate coverage, row failures, and omitted-row pointers. Complete source
rows and exact canonical JSON quotes stay in profiles; search documents contain
compact projections. Repeated identical source rows deduplicate, and conflicting
same-context observations remain separate facts with conflict references. Use a
different output directory to retain earlier conversions.

### Representation limits

- MIMIC shifted timestamps are tagged with a UTC offset solely to satisfy the
  shared `AwareDatetime` schema. This does not recover real dates or establish a
  real-world timezone. `configuration.time_basis` and document metadata explicitly
  identify `mimic_shifted_clock_utc_tag_not_real_utc`. Only within-patient relative
  timing is used; do not mix these timestamps with real calendar records.
- Each document freezes that patient's own discharge cutoff. Retrieval supports
  this through explicit `--patient-specific-assessments`; its default still
  requires one common cutoff. Mixed time bases are rejected. Runs using the flag
  record each patient's assessment time rather than presenting one global date.
- OMR's original date and units remain in provenance and time context.
  `clinical_time` stays null for date-only OMR rather than inventing midnight.
  Existing timestamp-dependent hard filters therefore retain such evidence for
  review. No BMI is automatically calculated from height/weight.
- Demographic age is named `approximate age`, not the canonical exact `age` field.
  Anchor-year age is approximate, and older ages are obscured. Existing exact-age
  filters must not silently treat it as precise screening age.
- Diagnoses are labelled as billing coding, retaining ICD code/version and
  admission provenance. Clinical onset and verified coding availability stay
  unknown; discharge is not invented as a diagnosis timestamp.
- HbA1c and blood creatinine use the exported dictionary definitions. Native item
  identifiers are retained; LOINC/SNOMED mappings are not invented. OMR units are
  inferred only when explicitly named in the source measurement label.
- Prescriptions remain orders, with original dose ranges preserved. Administration
  events retain their source descriptions: `Administered` is affirmed, explicit
  `Not Given`/`NotGiven`/`Refused`/`Held` events are negated, and other events remain
  possible. No outpatient adherence, uninterrupted use, or treatment stability is
  inferred. Future prescription stop dates remain only in original provenance,
  not searchable text.
- Profiles remain partial with explicit gaps. Conversion validity does not
  establish clinical correctness or completeness. Unsupported/bad rows produce
  retained failures, and later-than-cutoff rows are omitted with reasons.

### Optional later retrieval

After reviewing mappings and the frozen trial query, the generated documents can
be ranked with the existing local models. This conversion step does not run that
ranking or criterion-level eligibility assessment:

```bash
HF_HUB_OFFLINE=1 pixi run python -m clinical_trial_patient_matching.retrieval \
  --documents data/mimic/NCT03926806/processed-20/documents.jsonl \
  --query data/class5/query.json \
  --models data/class5/models.json \
  --output data/mimic/NCT03926806/processed-20/retrieval \
  --cache data/mimic/NCT03926806/processed-20/embeddings \
  --patient-specific-assessments
```

Offline mode requires the Class 5 model weights to be cached already. Keep
profiles, search documents, embeddings, and patient-level results in restricted
local storage outside Git. Querying MIMIC does not authorize sending its content
to an external language model; the adapter has no LLM path.

### Approved experimental Claude assessment

After confirming that the external processing arrangement is approved for MIMIC,
run `pixi run python scripts/run_mimic_claude.py`. This separate harness uses
`ANTHROPIC_MODEL` and `ANTHROPIC_API_KEY` from the environment. It sends all
extracted facts for three profiles by default against the frozen NCT03926806 query.
Use `--limit N` to change the patient count (maximum 20).
It saves inputs, raw model responses, token metadata, criterion assessments,
profile versions and citation validation under `data/mimic/NCT03926806/claude-live/`.
The manifest records the user's approval attestation; the script does not enable
or independently verify provider retention controls.

Run `pixi run python scripts/review_mimic_claude.py` after completion to write
`review.md` and `summary.json` for the latest run. For an inclusion criterion,
`met` means satisfied; for an exclusion criterion, `met` means the exclusion is
present. Missing evidence must remain unknown or require review. These are
experimental assessments, with human review required for every patient.
Schema and citation checks do not measure clinical accuracy; that requires
adjudicated criterion-level labels. This harness does not replace the planned
validated matching workflow or modify the existing patient profiles.

Source definitions: [hospital tables](https://mimic.mit.edu/docs/iv/modules/hosp/),
[laboratory events](https://mimic.mit.edu/docs/iv/modules/hosp/labevents.html),
[OMR](https://mimic.mit.edu/docs/iv/modules/hosp/omr.html).
