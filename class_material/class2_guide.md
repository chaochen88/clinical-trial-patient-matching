# Class 2 Pre-Read — Clinical Trial and Patient Data

Clinical trial matching combines three kinds of information: trial eligibility requirements, patient evidence, and reference judgments for evaluating the system. This project follows a **trial → patients** workflow: select a trial, retrieve candidate patients, and assess each candidate against the eligibility criteria.

| Data source | Content | Project role |
|---|---|---|
| AACT | Registered studies, eligibility criteria, conditions, interventions, and sites | Trial selection and criteria extraction |
| Synthea FHIR | Synthetic patient histories represented as linked healthcare records | Patient-data ingestion, evidence retrieval, and screening workflows |
| TrialGPT releases | Patient summaries, trial records, matching judgments, and expert criterion annotations | Evaluation of patient–criterion and patient–trial assessments |

## 1. Clinical Eligibility and Patient Evidence

**Inclusion criteria** specify requirements for participation, such as a diagnosis, age range, or treatment history. **Exclusion criteria** identify disqualifying conditions, such as a recent adverse event or use of a particular medication.

A criterion often combines several elements:

| Element | Example |
|---|---|
| Clinical concept | Type 1 diabetes |
| Numerical threshold | HbA1c between 5.6% and 9.5% |
| Duration | Diagnosis established for at least one year |
| Time window | Hospitalizations within the six months before screening |
| Logical relationship | Both a diagnosis and a specified treatment history are required |

Assessing a criterion requires evidence with the appropriate meaning, date, and level of detail. For example, a medication order documents prescribing; assessing uninterrupted use also requires evidence about actual use over the specified period.

An **electronic health record (EHR)** combines structured fields and narrative documentation. Structured data includes diagnosis codes, medication orders, and laboratory values. Narrative documentation describes clinical history, symptoms, assessments, and treatment in prose. **Longitudinal records** capture these observations across time.

**Provenance** identifies the source of each fact: dataset, patient identifier, record identifier, and relevant timestamp. It allows a reviewer to trace an assessment back to the underlying evidence.

## 2. AACT: Trial Data in PostgreSQL

**AACT—Aggregate Analysis of ClinicalTrials.gov—** organizes registry information into relational tables. PostgreSQL provides the database engine used to store and query those tables.

In the project database, `aact` is the database name and `ctgov` is the schema containing trial tables. A schema groups related database objects. The shared identifier `nct_id` links information belonging to the same trial.

### Core tables

| Table | Record content | Selected fields |
|---|---|---|
| `ctgov.studies` | Study identity and metadata | `nct_id`, `brief_title`, `overall_status`, `phase` |
| `ctgov.eligibilities` | Eligibility requirements | `criteria`, `minimum_age`, `maximum_age`, `gender` |
| `ctgov.conditions` | Conditions addressed by a study | `nct_id`, `name` |
| `ctgov.interventions` | Treatments or interventions | `nct_id`, `intervention_type`, `name` |
| `ctgov.facilities` | Study sites | `nct_id`, `name`, `city`, `country`, `status` |

A **join** combines related records. This query retrieves a trial's title, recruitment status, and eligibility text:

```sql
SELECT s.nct_id, s.brief_title, s.overall_status, e.criteria
FROM ctgov.studies AS s
JOIN ctgov.eligibilities AS e USING (nct_id)
WHERE s.nct_id = 'NCT04450407';
```

The eligibility record combines structured fields with free text. For example, `minimum_age` may contain `18 Years`, while `criteria` contains detailed inclusion and exclusion rules. A missing field is represented by SQL `NULL` and requires explicit handling during extraction.

### Row granularity and joins

**Granularity** describes what one row represents. A row in `studies` represents a study; a row in `facilities` represents a site associated with a study.

The saved example for `NCT04450407` has two interventions and 54 sites. Joining both child tables produces **2 × 54 = 108 combinations** for that single trial. To construct a one-row-per-trial dataset, aggregate interventions and sites separately before combining them with study metadata.

### Dataset versions

A **snapshot** is a saved version of the registry data. Record its date and preserve the original criteria alongside extracted rules. This makes results reproducible when registry records change.

Recruitment status and clinical eligibility are separate attributes. Historical snapshots support retrospective evaluation; recruitment workflows additionally check current trial and site availability.

## 3. Synthea FHIR: Patient Records and References

**Synthea** generates synthetic, longitudinal patient histories. **FHIR**, or *Fast Healthcare Interoperability Resources*, is the HL7 standard used to represent and exchange healthcare information. The project examples use **FHIR R4**.

Synthea produces the simulated history; FHIR defines its representation. A FHIR **resource** is a typed record, and a **Bundle** groups related resources. A single patient export can contain hundreds of resources.

### Common resources

| Resource | Content | Matching application |
|---|---|---|
| `Patient` | Identifiers and demographics | Age at the assessment date |
| `Encounter` | Visits, dates, and encounter types | Timeline and healthcare utilization |
| `Condition` | Diagnoses, onset, and status | Disease subtype and duration |
| `Observation` | Measurements, findings, and laboratory results | Numerical eligibility rules |
| `MedicationRequest` | Medication prescriptions and orders | Prescribed treatment history |
| `MedicationAdministration` | Recorded medication administration | Evidence of specific doses |
| `Procedure` | Procedures and dates | Surgical and procedural history |
| `AllergyIntolerance` | Allergy and intolerance records | Relevant exclusion criteria |

Resources connect through references. An Observation's `subject.reference` identifies the patient, while its `encounter.reference` can identify the associated visit. Within a Bundle, `entry.fullUrl` provides a reference target for the resource.

### Reading an Observation

The following selected fields come from the provided Synthea HbA1c observation:

```json
{
  "resourceType": "Observation",
  "id": "78bccfed-bfd9-4bb6-9623-64f744a694ac",
  "status": "final",
  "code": {
    "coding": [
      {"system": "http://loinc.org", "code": "4548-4"}
    ]
  },
  "subject": {
    "reference": "urn:uuid:338e0f93-2fb1-4152-8703-518686c567a8"
  },
  "effectiveDateTime": "2018-11-17T06:52:03-05:00",
  "valueQuantity": {"value": 6.938225162130798, "unit": "%"}
}
```

The record identifies the patient, measurement concept, value, unit, clinical time, and result status. These fields together form the evidence for a numerical comparison.

**LOINC** identifies measurement concepts; **SNOMED CT** represents clinical concepts such as diagnoses. Interpret a code together with its coding system. Keep human-readable labels for display and preserve the original codes for reliable processing.

For a trial requiring HbA1c below 8.5%, the value above satisfies the numerical threshold. Applying that result to a screening assessment also requires checking its date against the assessment time and any required measurement window.

### Constructing a patient profile

Convert linked resources into facts that retain their sources:

```text
patient_id | concept_system | concept_code | value | unit
           | effective_time | source_resource_id | evidence_status
```

A useful profile combines demographics, diagnoses, observations, medications, and procedures. Selecting a disease-specific patient cohort then involves checking diagnosis subtype, relevant measurements, treatment history, and temporal coverage.

## 4. TrialGPT: Matching Framework and Evaluation Data

**TrialGPT** is a published LLM-based framework with three stages:

1. **Retrieval:** find candidate trials for a patient summary.
2. **Matching:** assess the patient's evidence against individual trial criteria.
3. **Ranking:** aggregate criterion assessments to rank trials for that patient.

This project's trial → patients workflow uses the same patient–criterion assessment problem within its candidate-screening stage. Patient retrieval is evaluated separately against judgments for the selected trial and patient collection.

### Released data

| Data | Contents | Use |
|---|---|---|
| Patient summaries | Patient IDs and clinical narratives | Matching inputs |
| Trial records | Trial IDs and protocol text | Matching inputs |
| Patient–trial judgments | Reference labels for specified pairs | Pair-level evaluation |
| Expert criterion annotations | Criterion decisions and relevant patient sentences | Criterion and evidence evaluation |

A **benchmark** provides a defined task and reference judgments for comparing systems. The **unit of evaluation** matters: one patient can contribute multiple trial pairs, and one pair can contribute multiple criterion annotations.

### Criterion-level example

One released patient summary describes a 58-year-old woman. Two annotations for that patient illustrate different assessment outcomes:

| Criterion | Expert label | Evidence |
|---|---|---|
| Age 25–65 years | `included` | Sentence stating the patient's age |
| Specified chronic-angina diagnosis and abnormal perfusion scan | `not enough information` | Relevant symptom sentences provide incomplete support for the combined requirement |

A matching component receives the patient note, criterion text, and criterion type. An evaluator compares its output with `expert_eligibility` and `expert_sentences`. The release's GPT-4 predictions can serve as comparison outputs.

Keeping reference labels separate from model inputs prevents **data leakage**: evaluation answers influencing the system being assessed. Patient-separated evaluation partitions also help assess performance on unseen patients.

Pair judgments apply to their specified patient and trial versions. Preserve the source dataset and raw label when interpreting them, and distinguish explicitly judged pairs from unjudged pairs.

## 5. Evidence Status and Temporal Reasoning

A screening result should distinguish a supported decision from an unresolved question.

| Evidence situation | Interpretation |
|---|---|
| Relevant BMI measurement of 40 against an inclusion threshold of ≤35 | Criterion not satisfied |
| BMI requirement with insufficient measurement evidence | Criterion unresolved; obtain the required measurement |
| Conflicting results for the same measurement event | Reconcile source records and correction history |
| Different measurements on different dates | Evaluate the timeline and applicable time window |

Define an **assessment time** and use evidence available by that point. Retain timestamps, units, record status, and source references throughout processing. This supports auditable criterion decisions and specific follow-up questions for the research coordinator.
