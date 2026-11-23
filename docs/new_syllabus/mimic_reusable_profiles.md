# Reusable MIMIC patient expansion

The additional cohort contains 20 adults distinct from the original 20-patient
diabetes cohort. Selection does not require diabetes or any trial criterion.
Each selected patient has a linked discharge note charted and stored within the
365-day lookback and available by their selected discharge cutoff. This produces
a note-covered sample biased toward documented repeat care, not a representative
MIMIC sample or a trial-eligible cohort.

Local output: `data/mimic/reusable/additional-20/`.

Completed run: all 20 new patients were enriched by live Claude, preserving
7,480 hospital facts and adding 599 note facts. The audit verified all retained
note quotes against source offsets and confirmed note availability by cutoff.
It rejected 48 claim occurrences across extraction/repair attempts. These are
source-validation results, not a clinical accuracy measurement.

| Folder | Contents |
|---|---|
| `hospital` | Bounded BigQuery exports, SQL, hashes, query budgets and coverage |
| `processed` | Deterministic hospital profiles and search documents |
| `notes` | Linked discharge notes with availability classifications |
| `combined` | Claude-enriched profiles, saved responses, search documents and review |

Hospital extraction includes diagnosis codes, prescriptions, administrations,
height/weight/BMI, and selected blood laboratories: HbA1c, creatinine, glucose,
hemoglobin, platelets, white cells, sodium, potassium, albumin, bilirubin and
aminotransferases. These are selected evidence domains, not the full hospital
record. Exact native lab item codes, units and rows are retained.

Claude processes all 20 patients, up to two available discharge notes per patient,
three chunks per note, with one validation repair per chunk. Broad targets add
medical/surgical history, named drugs and doses, malignancy, infections,
respiratory/neurological/psychiatric history, smoking, function, pregnancy and
family history. The 20-fact response limit per chunk and note/chunk limits can
omit evidence; omissions and rejected claims remain visible in saved attempts.
Extraction does not decide eligibility. Note event times remain unknown unless
separately established; note timestamps are never measurement timestamps.

```bash
python scripts/fetch_mimic_cohort.py --reusable \
  --exclude-cohort data/mimic/NCT03926806/initial-20/cohort.jsonl \
  --dataset physionet-data.mimiciv_3_1_hosp --patients 20 \
  --max-gib-per-query 10 --max-gib-total 30 \
  --output data/mimic/reusable/additional-20/hospital --execute
python -m clinical_trial_patient_matching.mimic_adapter \
  --source data/mimic/reusable/additional-20/hospital \
  --output data/mimic/reusable/additional-20/processed
python scripts/fetch_mimic_notes.py \
  --source data/mimic/reusable/additional-20/hospital \
  --output data/mimic/reusable/additional-20/notes --execute
python -m clinical_trial_patient_matching.mimic_notes --patients 20 --reusable \
  --notes-per-patient 2 --max-chunks 3 \
  --hospital data/mimic/reusable/additional-20/processed \
  --notes data/mimic/reusable/additional-20/notes \
  --output data/mimic/reusable/additional-20/combined
python scripts/review_mimic_notes.py \
  --input data/mimic/reusable/additional-20/combined
```

Use a fresh output directory when rerunning; a live extraction makes additional
API calls. `--replay-from` revalidates saved exact-chunk responses locally without
calling Claude. Patient data, notes, profiles and model responses stay in ignored
local data folders.

The original combined cohort and this expansion can share a 40-patient document
pool through `scripts/combine_mimic_profile_pools.py`. It rejects overlapping
patient IDs and stale document/profile versions, preserves original profile
files, and writes an index with source hashes. The original cohort still has
only three note-enriched patients; adding 20 gives 23 with note facts in a
40-patient pool. Trial-specific retrieval and assessment use these shared
profiles without changing their source evidence. Ranking's existing truncation
limitation still needs separate work before comparing note effects on ranking.

The saved shared pool is `data/mimic/reusable/pool-40/`: `manifest.json`
indexes profile locations and hashes, `documents.jsonl` supplies shared search
documents, and `review.md` lists hospital/note fact counts per patient.

```bash
python scripts/combine_mimic_profile_pools.py \
  --source data/mimic/NCT03926806/with-notes/20261029T012542Z \
  --source data/mimic/reusable/additional-20/combined \
  --output data/mimic/reusable/pool-40
```

## Retrieval, ranking and assessment of the shared pool

The expanded runner accepts hash-bound pool indexes. `--top-ranked --patients 20`
selects ranks 1–20 for live Claude, including patients without notes. All 40 still
receive deterministic assessments. `--all-patients` can explicitly select the
whole pool and requires `--patients` to equal the pool size. The original
three-patient note-only pilot remains the default. Retrieval and shortlist sizes
expand to the complete pool.

```bash
python scripts/combine_mimic_profile_pools.py \
  --source data/mimic/NCT03926806/processed-20 \
  --source data/mimic/reusable/additional-20/processed \
  --output data/mimic/reusable/pool-40-hospital
python scripts/run_mimic_pipeline.py \
  --combined data/mimic/reusable/pool-40 \
  --hospital data/mimic/reusable/pool-40-hospital \
  --patients 20 --top-ranked --workers 2 --transport-retries 2 \
  --output data/mimic/reusable/pipeline-40/NCT03926806-20261029
python scripts/review_mimic_pipeline.py \
  --input data/mimic/reusable/pipeline-40/NCT03926806-20261029
python scripts/render_class6_heatmap.py \
  --input data/mimic/reusable/pipeline-40/NCT03926806-20261029 \
  --output data/mimic/reusable/pipeline-40/NCT03926806-20261029/heatmap.html
```

This uses the existing frozen NCT03926806 query and trial by default; both must
share the same trial source hash. Each patient's own shifted discharge cutoff
remains in force. All 40 receive deterministic assessment; only ranks 1–20 receive
live Claude narrative interpretation. Missing notes remain missing evidence. Claude outputs
remain unverified and do not earn verified eligibility-score credit.

`--workers` controls concurrent patients (one by default, at most four).
Response IDs and manifest writes are protected by a lock. Optional transport
retries handle transient API errors separately from the single content-validation
repair. Provider usage reflects returned model responses; transport failures may
not return usage. The existing prefix truncation in retrieval remains unchanged
and is counted in the saved review, so hospital-versus-note rank changes do not
measure the full value of the notes.

The initial live limit of 40 was reduced to the top 20 at the user's request.
Continuation uses `--resume` with the same output folder. It validates input
hashes, reuses the existing rankings and completed reports, and replays saved
responses only when patient, evidence context and repair feedback match exactly.
Unfinished requests without a saved response may need a fresh request. The prior
manifest is retained as `manifest-before-continuation.json`.

Completed NCT03926806 run: 40 patients ranked, 40 deterministic reports and
20 Claude-assisted reports (260 live criterion results). The audit confirmed
that all provider calls belong to ranks 1–20. There are 255 saved responses,
86 validated citations, 37 criterion interpretations that still failed
validation after repair, and 810 per-criterion context-omission occurrences.
Live statuses are 247 unknown, 11 met and 2 not met; all 20 patients need review.
All 40 search documents were truncated and adding notes changed no ranks under
this bounded-prefix implementation. These are integration/validation outcomes,
not clinical accuracy or eligibility confirmation. The heatmap contains 40
rules-only and 20 Claude-assisted views and defaults to the latter.
