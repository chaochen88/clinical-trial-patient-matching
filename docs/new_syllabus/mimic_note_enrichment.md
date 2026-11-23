# MIMIC-IV hospital and note evidence

This extension joins discharge summaries to the existing NCT03926806 convenience
cohort and combines extracted narrative claims with the shared hospital profiles.
It does not reselect patients or replace the previous exports, profiles, or Claude
experiment. No eligibility decision or score improvement is claimed from a new
concept appearing in a note.

## What was run

The available BigQuery note dataset is `physionet-data.mimiciv_note`; its discharge
table has `note_id`, `subject_id`, `hadm_id`, `note_type`, `note_seq`, `charttime`,
`storetime` and `text`. It is in the same US region as the hospital dataset.
The exporter joins both patient and admission IDs against hospital admissions,
then retains notes from completed admissions with chart times within the original
365-day lookback. The original cohort export is verified by its hash.

The bounded query scanned approximately 3.296 GiB, below its 10-GiB cap. It
returned 19 discharge notes for 13 of the existing 20 patients. Seven notes for
five patients were stored by the original discharge cutoffs. Twelve notes were
stored later and are retained locally but excluded from cutoff-valid evidence.
Seven patients had no matching notes in this dataset and lookback.

Using the user's previously confirmed approved Anthropic processing arrangement,
the pilot processed one available note for each of three patients, preserving the
earlier three-patient live-test limit. Selection is the first three cohort IDs
with available notes, not necessarily the same patients as the earlier hospital-only
experiment. Notes are prioritized by selected admission, then most recent chart
time. At most three 16,000-character chunks per note are processed, with a
400-character overlap; omitted notes or chunks remain explicit limitations.

The original strict quote run was retained. Its already-paid responses were then
revalidated locally with no further API calls. The final merged export has 20
profiles, with 69 added source-linked candidate claims across three patients.
Examples of documented topics include alcohol, allergy history, medications,
adherence, diet, weight history, and relevant diseases. These are not independently
verified clinical facts or proof that the corresponding trial requirement is met.

| Local output | Purpose |
|---|---|
| `data/mimic/NCT03926806/notes/20261029T011440Z/` | Original note rows, SQL, job/cost metadata, hashes and availability coverage |
| `data/mimic/NCT03926806/with-notes/20261029T011831Z/` | Original live responses and failed strict batch validations, retained for audit |
| `data/mimic/NCT03926806/with-notes/20261029T012542Z/profiles/` | Final merged profiles with original hospital facts unchanged |
| `.../documents.jsonl` | Search projections of the combined profiles |
| `.../enrichment_report.json` | Selected patients, attempts, rejected claims, deferred notes, added concepts and source/profile versions |
| `.../review.md` | Coverage and per-patient before/after concept summary |
| `.../patient_note_evidence.md` | Tables of note claims, assertions, experiencers, scope, exact quotes and source note IDs |
| `.../audit.json` | Exact span, temporal cutoff, identity and hospital-preservation checks |

All patient-level records and outputs remain in ignored `data/` and must retain
the access controls of the source data. They are not test fixtures or public web
assets. The source dataset alias is not itself a frozen release identifier;
export hashes, source metadata and BigQuery job IDs bind the local snapshot.

## Source and time semantics

`mimic_notes.py` uses the existing `Fact` and `Profile` schemas. Every added fact
keeps the note ID, patient/admission IDs, full original note text, row version,
source hash, exact character offsets, model quote, extraction prompt hash, store
time, and admission/discharge/historical/unspecified scope.

`clinical_time` is deliberately null: a note's chart time is not a laboratory
measurement date, diagnosis onset, or the beginning of a regimen. Redacted dates
and approximate ages are not reconstructed. `available_time` is the source store
time, in the existing MIMIC shifted-clock convention, not real UTC. Later and
unknown-availability notes are never supplied to extraction for the original cutoff.

Literal quote matches are preferred. If only whitespace differs, a quote may be
aligned to a unique source span without changing any words, punctuation, case or
negation. The stored quote is then the exact original span; the model quote and
alignment method are retained. Ambiguous matches and unsupported quotes are
rejected. Valid claims in a partial response are retained; one repair is allowed,
and remaining rejected claims stay unresolved. Repeated equivalent claims from
retries/chunk overlap are deduplicated within the note.

The final audit verified 40 literal quotes and 29 unambiguous whitespace-aligned
quotes. Twenty-two rejected claim occurrences across the replayed attempts were
not admitted as facts. Extra checks prevent generic NKDA/allergy statements from
becoming lactose-intolerance or milk-allergy assertions, and require explicit
stability/change/time language for dose-stability claims. These checks catch
specific unsupported generalizations; they do not establish clinical entailment.

Original hospital facts remain unchanged. Note measurements never override labs
or OMR values. Different same-concept/unit values generate source-review pairs;
without verified temporal alignment they are not automatically treated as
contradictory measurements from the same encounter. Hospital-only missingness
findings are retained with a label identifying their original scope.

Search documents include note quotes, assertions and scope, with unknown event
dates labeled. Class 6 can supply these notes as narrative context when both note
chart/store times are verified before the cutoff and the time basis matches.
Full note text is included once per source hash in the narrative context, with
compact fact references, rather than repeated for every extracted assertion.
This does not make them dated numeric observations: deterministic numeric rules
still reject unknown measurement times, and narrative results remain unverified.

## Commands

Inspect metadata only, or dry-run the exact linked-note query:

```bash
pixi run mimic-notes-fetch --inspect
pixi run mimic-notes-fetch
```

Export under the byte cap into a new timestamped directory:

```bash
pixi run mimic-notes-fetch --execute --max-gib 10
```

The exporter does not broaden the cohort when note coverage is poor. A dry-run
budget failure prevents the billed query. An export row-limit failure marks the
run incomplete, and the adapter refuses an incomplete export.

Live extraction requires an approved external processing arrangement and the
configured `ANTHROPIC_MODEL` / `ANTHROPIC_API_KEY`; the code does not enable or
independently verify provider retention settings:

```bash
pixi run mimic-notes-extract \
  --notes data/mimic/NCT03926806/notes/20261029T011440Z \
  --patients 3 --notes-per-patient 1 --max-chunks 3
```

Revalidate the existing responses without contacting Claude:

```bash
pixi run mimic-notes-extract \
  --notes data/mimic/NCT03926806/notes/20261029T011440Z \
  --replay-from data/mimic/NCT03926806/with-notes/20261029T011831Z \
  --patients 3 --notes-per-patient 1 --max-chunks 3
```

Replay requires exact input-chunk hashes and has no live fallback. A new output
directory is created, preserving the original responses. Audit and generate the
readable patient tables for the final output:

```bash
pixi run mimic-notes-review \
  --input data/mimic/NCT03926806/with-notes/20261029T012542Z
```

Missing diet stability, three-month outpatient dose history, actual adherence,
food-specific allergies and other trial details must remain unresolved unless
the source explicitly supports them. Using notes signed after discharge would
require a separately defined post-discharge review time and a new versioned
assessment; they must not silently fill the original cutoff's gaps.

Sources: [MIMIC-IV note module](https://mimic.mit.edu/docs/iv/modules/note/),
[discharge summaries and timestamp definitions](https://mimic.mit.edu/docs/iv/modules/note/discharge.html).

## End-to-end retrieval, reranking and live assessment

```bash
pixi run mimic-pipeline-live --patients 3
```

This explicitly invokes the approved Anthropic account. It compares local
hospital-only and combined-profile retrieval for the same 20 patients using the
cached, pinned MiniLM encoder, BM25, reciprocal-rank fusion and cross-encoder.
No cloud embedding service or additional BigQuery export is used. The small pool
retains all 20 reranked candidates; a three-patient live pilot selects the
highest-ranked shortlisted patients with note evidence, rather than assuming the
overall top three have notes. Patient IDs, profile versions, the frozen trial
hash and patient-specific cutoffs are checked before assessment.

All 20 combined profiles receive deterministic assessments. At most three
patients then receive Class 6 rules plus live Claude narrative assessment:
numeric comparisons stay deterministic, narrative responses require exact
patient-specific citations, and one repair is allowed. Raw provider responses,
input contexts, feedback and token metadata are saved immediately under `calls/`.
The final results remain unverified for narrative interpretation; their statuses
do not automatically increase the verified evidence score or approve eligibility.

Outputs go to a new directory under `data/mimic/NCT03926806/pipeline-live/`:

| Output | Contents |
|---|---|
| `retrieval-hospital/shortlist.json` | Hospital-only lexical, vector, fused and reranked scores |
| `retrieval-combined/shortlist.json` | Combined-profile rankings with truncation and evidence references |
| `ranking.csv`, `ranking.json` | Rank changes, reranker scores, note coverage and pilot selection |
| `reports/` | All deterministic reports plus the three live-assisted reports |
| `patient-*-claude.md` | Per-patient criterion tables, follow-ups and failure reasons |
| `calls/` | Exact live inputs, raw responses and token metadata |
| `manifest.json` | Source versions, selection policy, prompt/configuration and completion state |

Use `mimic-pipeline-review --input PATH_TO_RUN` to audit the saved results and
generate `review.md` and `summary.json` without additional model calls. The
existing `scripts/render_class6_heatmap.py --input PATH_TO_RUN --output
PATH_TO_RUN/heatmap.html` renders the same saved reports as an offline heatmap.
All note content and resulting HTML must remain restricted local data.

This is an integration pilot. MIMIC patient relevance and criterion gold labels
are not available here, so recall, nDCG, criterion F1 and clinical accuracy are
not reported. Rank changes are not themselves evidence of improvement. In the
current retrieval implementation, token-bounded text is used by BM25 and the
cross-encoder as well as the encoder; late note evidence can therefore be cut
off during ranking even when full sources remain available to assessment.
