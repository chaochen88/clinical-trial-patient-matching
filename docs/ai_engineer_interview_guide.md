# AI Engineer Interview Guide

Prepared for your clinical-trial patient-matching project. Repository review date: November 19, 2026.

## How to practice

Answer each question aloud before reading the sample answer. Aim for 60–90 seconds for a normal answer and three minutes for a design question. Explain the problem, your choice, the tradeoff, and your evidence. The follow-ups are prompts to practice, not additional claims about your implementation.

The project answers below describe repository code and recorded engineering results, not a new execution or independent clinical validation. Replace “I” with “we” where appropriate and describe only work you personally did. Behavioral answers are templates: fill in your own experience.

## 1. Your project story

### 1. Tell me about your project.

**Sample answer:** I built a prototype for retrieving patients for a clinical trial and assessing them against its criteria. It separates trial-criteria extraction, source-linked patient profiles, hybrid retrieval, criterion assessment, and bounded evidence investigation. Retrieval narrows the candidate pool; deterministic rules assess supported criteria; an optional LLM planner requests missing evidence through validated tools. Every stage preserves uncertainty and provenance. The output supports screening review rather than making an enrollment decision. My next extension is persistent human review and recovery across worker restarts.

**Follow-up:** Draw the system and explain what enters and leaves each stage.

### 2. What makes this an AI engineering project rather than an LLM API wrapper?

**Sample answer:** The model is one component inside a controlled pipeline. The engineering includes strict schemas, source validation, temporal filtering, hybrid search, evaluation, tool authorization, caching, bounded execution, and explicit failure states. For example, the model can propose an observation lookup, but application code validates the patient, code, cutoff, and budget before executing it. Deterministic rules then compare the returned value with the criterion.

**Follow-up:** Which safeguards would still work if the model ignored its prompt?

### 3. Why separate retrieval from eligibility assessment?

**Sample answer:** They optimize different objectives. Retrieval finds patients worth examining, so missing a relevant patient is costly. Assessment checks exact predicates against usable evidence. A high similarity score does not establish a numeric threshold, treatment duration, or absence of an exclusion. I preserve full profiles separately from compact search documents so later assessment can inspect evidence that the encoder did not see.

**Follow-up:** Can a patient rank first and still fail screening? Give an example.

### 4. What have you implemented, and what remains planned?

**Sample answer:** The repository implements extraction and patient profiles, BM25 and vector retrieval with fusion and reranking, criterion assessment, and a bounded evidence-investigation graph with direct and MCP tool access. The current investigation graph compiles without a persistent checkpointer. PostgreSQL-backed review interrupts, a durable resume queue, correction-driven report review, and Langfuse experiments are Class 8 requirements I would describe as planned extensions rather than completed features.

**Follow-up:** What would be the smallest complete Class 8 demonstration?

### 5. What results can you defend?

**Sample answer:** The documented public retrieval run processed 555 Synthea patient IDs, fused two top-50 lists, and selected 20 patients after reranking. It had no independent relevance labels, so I cannot claim clinical recall. On three fictional trials with 20 synthetic patients, every method achieved Recall@10 of 1.0, but reranking improved nDCG for one trial and reduced it for the other two. The Class 7 comparison documented equivalent direct and MCP results across four scenarios. These establish bounded engineering behavior, not clinical validity.

**Follow-up:** Why is a perfect metric on a small synthetic fixture weak evidence?

## 2. Retrieval, RAG, and evaluation

### 6. Why combine BM25 and embeddings?

**Sample answer:** BM25 is useful for exact codes, uncommon terms, and lexical matches. Embeddings can capture semantic similarity when wording differs. Neither is consistently superior across all queries. Combining their rankings gives me a baseline that can benefit from both, while evaluating the separate methods reveals whether the combination actually helps.

**Follow-up:** Give a query where lexical retrieval might outperform dense retrieval.

### 7. Explain reciprocal rank fusion.

**Sample answer:** RRF combines ranks rather than incompatible raw scores. For a document, I sum `1 / (c + rank)` over the lists where it appears, using ranks starting at one. The project uses `c = 60`. A document appearing near the top of both lists receives strong combined support. RRF is simple and avoids score calibration, but discards information about score margins and still needs evaluation.

**Follow-up:** What happens to a document present in only one list?

### 8. What is the difference between a bi-encoder and a cross-encoder?

**Sample answer:** A bi-encoder embeds the query and document separately, allowing document vectors to be precomputed and searched efficiently. A cross-encoder jointly processes a query-document pair and scores their interaction. It is more expensive per candidate, so I use it after candidate retrieval. In this project, the cross-encoder reranks 50 fused candidates to select 20; it does not scan the whole corpus.

**Follow-up:** Why can reranking never recover a patient absent from its candidate set?

### 9. Did reranking improve your project?

**Sample answer:** Not consistently in the recorded synthetic comparison. For the type-1 example, nDCG@10 improved from approximately 0.983 after fusion to 1.000 after reranking. For type-2 and obesity, it declined. That suggests a generic reranker should remain an evaluated option rather than an assumed improvement. I would use a larger, independently reviewed development set to investigate domain mismatch and reserve a separate test set for the final comparison.

**Follow-up:** When would you remove the reranker entirely?

### 10. Explain Recall@K, Precision@K, and nDCG@K.

**Sample answer:** Recall@K is the relevant items retrieved in the top K divided by all relevant items in the evaluation pool. Precision@K measures how many top-K positions are relevant. nDCG@K rewards putting relevant items earlier, normalizing discounted gain against the ideal ranking. My current evaluation uses binary relevance. Recall captures candidate loss; ranking quality helps estimate review burden. None of these measures clinical eligibility correctness.

**Follow-up:** With ten relevant patients and four retrieved in the top ten, what is Recall@10? **Answer:** 0.4.

### 11. How do you handle incomplete relevance labels?

**Sample answer:** Unreviewed patients are unresolved, not irrelevant. The evaluation code tracks judgment coverage and leaves recall and nDCG undefined until the fixed pool is completely judged and has positive examples. This avoids publishing a reassuring metric based on silently treating missing labels as negatives. Even complete labeling of a small pool supports conclusions about that pool, not the entire patient population.

**Follow-up:** How would you build a judgment pool that avoids favoring one retriever?

### 12. Is your system RAG?

**Sample answer:** It uses retrieval and evidence-grounded processing, but the central output is a structured screening report rather than a conventional generated chat answer. I would describe the actual stages instead of relying on the RAG label. If I added a natural-language explanation, I would generate it from validated criterion results and source references, then evaluate its factual support separately.

**Follow-up:** How would you keep a generated summary from overstating an unknown result?

### 13. How would you choose chunking for a document RAG system?

**Sample answer:** I would start with the retrieval unit the task needs: a clause, section, note, or patient summary. I would preserve document identity, timestamps, and structural boundaries; measure actual tokenizer lengths; and compare chunk sizes and overlap on labeled queries. Smaller chunks improve focus but can lose context; larger chunks preserve context but can dilute relevance. For this project, the search unit is a compact patient document linked to the complete profile, rather than arbitrary chunks of every raw record.

**Follow-up:** How would you retrieve a criterion whose evidence spans two sections?

### 14. How do you avoid evaluation leakage?

**Sample answer:** Expected answers stay outside prompts and search inputs. I would split related examples by patient and source or template when necessary, rather than randomly splitting near-duplicate records. Prompt and retrieval tuning happen on a development set; the held-out test set is evaluated after choices are fixed. The project also excludes evidence unavailable at the assessment cutoff, preventing temporal leakage.

**Follow-up:** Why can two notes for the same patient leak information across a split?

## 3. Evidence, schemas, and correctness

### 15. Why use Pydantic and structured output?

**Sample answer:** Strict models make the interface explicit and reject malformed values or unexpected fields. They make downstream code easier to reason about and give targeted feedback for bounded repair. But valid JSON and a valid schema do not establish factual correctness. I also validate source references, timestamps, patient identity, units, and rule applicability. Clinical meaning remains a separate verification problem.

**Follow-up:** Give an example of schema-valid but factually wrong output.

### 16. How do you reduce hallucination?

**Sample answer:** I limit the model's responsibilities, require source-linked outputs, validate quotes, and use deterministic comparisons for reviewed rules. Missing, conflicting, or unsupported evidence remains unknown. The extraction workflow allows a bounded repair rather than endless regeneration. These measures reduce specific errors and expose others; they do not prove that every source quote entails the extracted claim.

**Follow-up:** Can an exact quote still be cited incorrectly? **Answer:** Yes—for example, a family-history quote can be misattributed to the patient.

### 17. What is the difference between clinical time and availability time?

**Sample answer:** Clinical time is when the observation or event occurred. Availability time is when it became available to the screening workflow. For retrospective screening, both must be no later than the assessment cutoff. A result measured Monday but released Thursday must not influence a Wednesday assessment. Keeping both times prevents using future knowledge accidentally.

**Follow-up:** How should you handle an observation with a missing timestamp?

### 18. How do you handle contradictory observations?

**Sample answer:** I preserve the underlying facts and their provenance instead of silently averaging or letting ingestion order choose a winner. Reconciliation exposes conflicts. If usable evidence does not establish a unique result under the rule, the criterion stays unknown with a follow-up request. A reviewed correction should append a versioned decision and identify which source is superseded while retaining the history.

**Follow-up:** Why is “take the latest value” insufficient for same-encounter conflicts?

### 19. Explain inclusion/exclusion polarity and three-valued logic.

**Sample answer:** A criterion result describes whether its stated predicate is met. If an exclusion says “has condition X,” met means the exclusion is present, which is adverse to screening. Group logic is evaluated before applying inclusion or exclusion polarity. With unknown values, `false AND unknown` is false, while `true OR unknown` is true; other combinations may remain unknown. This avoids turning missing evidence into a false statement.

**Follow-up:** What is `true AND unknown`? **Answer:** Unknown.

### 20. How do you make runs reproducible?

**Sample answer:** I retain frozen input snapshots, content hashes, profile and trial versions, assessment cutoff, model revisions, prompt version, and relevant configuration. Cached embeddings are associated with document versions and model settings. Model outputs and usage logs help explain a historical run. Low temperature alone is not a reproducibility guarantee, especially for external model services.

**Follow-up:** Which artifacts would you need to explain why an old report differs from a new one?

## 4. Agents, tools, and MCP

### 21. What makes your evidence workflow agentic?

**Sample answer:** An optional model planner chooses whether to request additional evidence and which permitted lookup to propose based on unresolved criteria. The application controls execution and reassessment. This adaptive choice is the agentic part; the rest is an explicit workflow. Sufficient evidence bypasses the planner. I use agency only where choosing the next evidence action is useful.

**Follow-up:** When would a fixed workflow be preferable?

### 22. Why LangGraph instead of a loop?

**Sample answer:** A normal loop could implement a small version. LangGraph gives explicit nodes, branching, shared state, and parallel criterion checks, making transitions easier to inspect. In the project, criterion checks fan out with `Send` and merge through a reducer. Persistence and interrupts are planned next. I would justify the framework by these needs rather than claiming every LLM application requires it.

**Follow-up:** What complexity does using a graph framework introduce?

### 23. How do you stop the agent from looping or overspending?

**Sample answer:** The investigation has at most two additional evidence rounds, four reserved tool attempts per patient, and a planner-call bound, plus a shared run budget. It rejects repeated identical requests and stops when no usable evidence arrives. A multi-call proposal must fit the budget before dispatch. Cache keys include normalized arguments and the source version. These are application-enforced bounds, not instructions the model can choose to ignore.

**Follow-up:** Why reserve a whole batch before executing any call?

### 24. What is MCP, and why use it here?

**Sample answer:** MCP provides a standard interface for clients to discover and call tools exposed by a server. My server wraps the same read-only evidence backend used by direct calls. The client handles discovery, execution, and normalized errors. This lets me compare the two paths without changing the underlying evidence logic. MCP does not itself establish authorization or clinical correctness; the backend still enforces scope and validates inputs.

**Follow-up:** What additional failure modes come from the transport layer?

### 25. How do you defend against prompt injection in patient records?

**Sample answer:** Record text is untrusted input. The planner receives a narrow context of unresolved predicates, fixed identifiers, cutoff, and tool definitions. The backend independently enforces patient/trial scope and read-only access, and the model cannot expand those permissions through text. I would test adversarial records, cross-patient requests, and attempts to change the cutoff. Prompt instructions help, but enforceable boundaries provide the stronger protection.

**Follow-up:** Why is “ignore malicious instructions” in the system prompt insufficient?

### 26. How do parallel checks merge safely?

**Sample answer:** Each criterion check reads a frozen profile and returns a result keyed by round, patient, and criterion. The reducer merges independent results and rejects conflicting values for the same identity. Aggregation expects all required criteria to have results, including explicit failures. Shared budgets use locking in the current process; a distributed deployment would need database transactions or another shared coordination mechanism.

**Follow-up:** Why is a Python thread lock insufficient across multiple workers?

## 5. Persistence and production design — proposed extensions

### 27. What is a checkpoint? Does the coordinator click it?

**Sample answer:** A checkpoint is persisted execution state, saved by the workflow runtime. The coordinator sees a review request and submits a decision through the UI. The backend uses that decision to resume the matching run. The review screen is an application feature; checkpointing enables the workflow to remember where it paused. I would preserve the same run identity across the initial execution and resume.

**Follow-up:** What belongs in execution state versus the review audit table?

### 28. Why PostgreSQL? Is it required?

**Sample answer:** Durable storage is required for recovery; PostgreSQL is a design choice. It can store checkpoints and application records and supports transactions and uniqueness constraints for review decisions and queued work. SQLite can serve smaller persistent applications, and memory storage is useful for disposable tests. I would choose based on concurrency, operational requirements, and framework support. The current evidence lookup backend uses read-only SQLite; that is separate from the proposed PostgreSQL checkpoint store.

**Follow-up:** Does storing a checkpoint automatically create a complete audit history?

### 29. How would you handle duplicate review submissions?

**Sample answer:** I would use a stable review request ID and a normalized payload fingerprint. In one transaction, I would validate the pending report version, save the review event, and enqueue the resume job. An identical retry returns the saved response; a different payload under the same identity is a conflict. Workers serialize execution per run and check whether the decision was already applied. Reports also need unique run/version identities because crashes can replay application operations.

**Follow-up:** What if the database commits but the HTTP response is lost?

### 30. What happens when a worker crashes or a run is cancelled?

**Sample answer:** For recovery, I would load persisted state and inspect whether the run is awaiting review, has resumable pending work, or is terminal. A queued decision can be applied after restart. For cancellation, I would persist a flag, stop dispatching work, attempt to cancel in-flight operations, and recheck before committing reports. A timeout around a thread does not necessarily stop the underlying operation, so late results must also be guarded. Completed and cancelled runs should not be restarted blindly.

**Follow-up:** Which crash boundaries would you test with a fresh worker process?

### 31. How would you add retries and observability?

**Sample answer:** I would classify failures, retry transient ones with bounded backoff and jitter, and count every attempt against persisted budgets. Invalid requests and authentication errors need correction rather than repeated calls. Traces would capture node/tool duration, errors, retries, usage, and versions. Initial and resumed invocations would share a screening session identity. Human waiting time should be separated from execution time. Trace export is observability; application storage remains authoritative for decisions and state.

**Follow-up:** How would you avoid nested client and worker retries multiplying attempts?

### 32. Design a deployed version of this system.

**Sample answer:** I would first clarify patient volume, screening frequency, privacy constraints, and latency expectations. A backend API would create runs and accept validated reviews. Workers would execute the graph from a durable queue, with per-run coordination. PostgreSQL would store application state and checkpoints; versioned storage would hold source artifacts; a retrieval index would contain permitted search projections. Tenant/patient authorization would apply before retrieval and evidence access. I would add monitoring, backups, failure recovery tests, and a reviewed evaluation gate before expanding usage. This is a proposed architecture, not the current deployment.

**Follow-up:** How would you rebuild the index without serving mixed embedding versions?

## 6. AI engineering topics beyond this project

### 33. When would you use prompting, RAG, or fine-tuning?

**Sample answer:** I would start with the observed failure. Prompting can clarify instructions and output format. Retrieval supplies external knowledge, updates, and traceable context. Fine-tuning can improve repeated behavior or domain tasks when representative training examples and a stable evaluation justify it. It is not a reliable substitute for current factual retrieval or access control. I would compare the simplest baseline against alternatives on held-out quality, latency, and cost.

**Follow-up:** What evidence would justify fine-tuning rather than improving retrieval?

### 34. Explain tokens, attention, and the context window.

**Sample answer:** Tokens are the model's text units. Attention lets representations incorporate information from other permitted positions; a causal language model restricts access to later tokens. The context window limits how much input and generated output the model can consider in one request. Filling the window does not guarantee good use of every fact. I would measure tokenization, retrieval coverage, truncation, and answer quality rather than assuming more context is always better.

**Follow-up:** Why are tokens not interchangeable with words or characters?

### 35. What are temperature and top-p? Does temperature zero guarantee correctness?

**Sample answer:** Temperature changes how concentrated the next-token distribution is; lower values generally favor higher-probability choices. Top-p restricts sampling to a probability mass of candidate tokens. Neither establishes truth. Even low-temperature output can be wrong, and exact repeatability can depend on the provider and execution environment. I choose generation settings for the task and verify outputs through evaluation and application checks.

**Follow-up:** Where might more diverse generation be useful?

### 36. What are embeddings, cosine similarity, and approximate nearest-neighbor search?

**Sample answer:** An embedding maps an input into a numeric representation. Cosine similarity compares vector direction; for unit-normalized vectors it equals their dot product. At small scale, exact search is simple and gives a useful baseline, as in this project. At larger scale, approximate nearest-neighbor indexes trade some retrieval accuracy for lower search cost. I would evaluate ANN recall against exact neighbors separately from whether either ranking is relevant to the task.

**Follow-up:** Why does a high vector similarity not mean a calibrated probability?

### 37. How would you evaluate a generative assistant?

**Sample answer:** I would define task success and build a versioned dataset with realistic, difficult, and adversarial cases. I would evaluate answer correctness, evidence support, refusal or uncertainty behavior, tool success, latency, and cost. Deterministic checks cover formats and exact constraints; human review handles ambiguous quality. Model judges can help scale evaluation, but I would calibrate them against humans, inspect disagreement, and keep expected answers out of the model's input.

**Follow-up:** How could an LLM judge favor a verbose but incorrect answer?

### 38. How do you optimize latency and cost?

**Sample answer:** I would profile stages first, separating model startup, indexing, retrieval, reranking, generation, and queue time. Then I would remove unnecessary calls, cache versioned work, batch offline computation, and route simpler tasks to cheaper components if evaluation supports it. I would track p50/p95 latency, tokens per successful task, and quality alongside spend. Fewer tokens or lower latency is not an improvement if task failures increase substantially.

**Follow-up:** When can parallelization reduce latency but increase total cost?

### 39. Explain async I/O, threads, and processes in Python.

**Sample answer:** Async I/O is useful for coordinating many waiting operations when clients support it. Threads can accommodate blocking I/O and some native-library work. Processes are useful for isolating work and parallelizing CPU-bound Python code, though startup and serialization have costs. I would choose based on the bottleneck and client behavior. Cancelling an await does not universally cancel the external operation; deadlines and cleanup need support at the underlying client boundary too.

**Follow-up:** What happens if blocking model calls run directly inside an async request handler?

### 40. What API and database fundamentals should an AI engineer know?

**Sample answer:** I need input validation, authentication, authorization, timeouts, rate limiting, pagination, stable error contracts, and idempotency for retried writes. In SQL, I need parameterized queries, indexes, transactions, uniqueness constraints, and an understanding of concurrent updates. For a review endpoint, a conditional update on the pending report version helps reject stale submissions, while a unique request identity prevents duplicate decisions. An ORM does not remove the need to understand these guarantees.

**Follow-up:** Explain authentication versus authorization using the coordinator workflow.

### 41. How would you handle model changes and monitoring after release?

**Sample answer:** I would version models, prompts, tools, schemas, and retrieval indexes. Before rollout I would run regression evaluations, then use a controlled release with monitoring and a rollback path. I would distinguish operational drift, such as rising timeouts, from input drift and quality degradation. User acceptance is useful feedback but is not equivalent to correctness. I would periodically review labeled samples and inspect failures by slice rather than relying only on one aggregate score.

**Follow-up:** Which model changes require rebuilding embeddings?

### 42. How would you handle sensitive data?

**Sample answer:** I would begin with the deployment's data-access requirements and minimize what each component receives. Controls would include scoped authorization, encryption, secret management, retention limits, and audit records. I would treat logs, traces, caches, and embeddings as potentially sensitive, not just raw text. Before sending records to an external provider, I would verify that the approved data-handling arrangement covers that use. Public and synthetic fixtures are useful for development but do not establish production readiness.

**Follow-up:** How could observability accidentally create a second sensitive-data store?

## 7. Coding and practical exercises

### 43. Implement an RRF function. What edge cases matter?

**Sample answer:** I would accept ranked ID lists, add `1 / (c + rank)` once per unique ID per list, and sort by descending total score with a stable ID tie-breaker. I would validate the constant and define how duplicates and empty inputs behave. Complexity is linear in the input lengths plus sorting the unique candidates. Tests should cover overlap, one-list-only items, duplicates, empty lists, and ties.

**Practice:** Write it without a retrieval library, then explain why summing raw BM25 and cosine scores is problematic.

### 44. Design a cache key for an evidence tool.

**Sample answer:** I would include the tool name, normalized validated arguments, source snapshot/version, and any configuration that changes the result. The authorization scope must either be represented in the key or checked independently on every access so cached data cannot leak across scopes. I would define expiry and whether transient errors are cacheable. A cache is an optimization, not an authority bypass.

**Practice:** Explain why argument ordering should not create two cache entries for the same request.

### 45. Design tests for an unreliable model or tool.

**Sample answer:** I would use deterministic fixtures to cover control flow and reserve live tests for actual integrations. Cases would include malformed output, wrong-patient citations, unsupported units, future timestamps, empty success, truncation, timeout, conflicting evidence, repeated calls, and budget exhaustion. I would assert meaningful behavior such as zero unauthorized reads and preserved unknowns. Fixtures verify my code's behavior; they do not measure the live model's general reliability.

**Practice:** Describe a test where a tool returns HTTP success but unusable evidence.

## 8. Behavioral questions — personalize these answers

### 46. Tell me about a technical choice that did not work as expected.

**Answer framework:** Use the reranking result if you personally investigated it. Explain your hypothesis, the frozen comparison, the mixed nDCG results, and the latency tradeoff. Say what you changed or would change based on that evidence. Do not invent a production incident or business impact. A strong lesson is that adding a more sophisticated component requires measured benefit.

**Follow-up:** What experiment would you run next?

### 47. How do you make decisions under uncertainty?

**Answer framework:** Describe one concrete unknown, the smallest experiment that could reduce it, and what decision the result would change. Your project offers examples: retaining unknown clinical criteria, comparing retrieval baselines, or checking direct/MCP equivalence. Explain how you recorded limitations and avoided converting incomplete evidence into a confident claim.

**Follow-up:** When would you ship a limited feature, and when would you stop?

### 48. Describe a disagreement or collaboration challenge.

**Answer framework:** Use a real example with situation, responsibility, action, and result. Explain the other person's concern fairly, how you established shared criteria, and how evidence influenced the decision. If this project was solo, say so and choose another real collaboration example. Do not manufacture teammates or code-review experiences.

**Follow-up:** What would the other person say you could have done better?

### 49. What is the biggest weakness in your project?

**Sample answer:** The evaluation is not sufficient to establish clinical effectiveness. The synthetic fixtures verify engineering behavior, but the public retrieval run lacks independently reviewed relevance labels, and the agent supports only a narrow set of observation mappings. Durable human review is also planned rather than implemented. My priorities would be a reviewed evaluation set, stronger failure analysis, and a complete persistence/recovery demonstration before broader deployment claims.

**Follow-up:** Which improvement would you prioritize in one week, and why?

### 50. Why should we hire you as an AI engineer?

**Sample answer to personalize:** My project shows that I think beyond model calls. I can connect retrieval, structured data, evidence validation, tool orchestration, and evaluation into a working prototype, and I can explain the limits of the results. I use deterministic code where it provides stronger guarantees and models where flexible interpretation or action selection adds value. I would bring that approach to your product while learning its users, operational requirements, and evaluation standards.

**Follow-up:** Adapt this answer to the actual role instead of listing every technology you know.

## 9. A focused preparation plan

1. **Project narrative:** Practice questions 1–5 and draw the full pipeline from memory.
2. **Retrieval:** Work through questions 6–14 and implement the RRF exercise.
3. **Correctness:** Practice questions 15–20 using one source-linked criterion example.
4. **Agents:** Trace a missing-BMI case through questions 21–26, including the failure path.
5. **Systems:** Design the proposed review endpoint, transaction, queue, and recovery flow from questions 27–32.
6. **Breadth:** Review questions 33–45 and practice one coding exercise under time pressure.
7. **Mock interview:** Answer ten mixed questions without notes; revise unclear answers and prepare two real behavioral stories.

## 10. Questions to ask the interviewer

- How do you define and measure quality for your AI features?
- What are the most common production failure modes today?
- How much of this role is application engineering, evaluation, data work, or model training?
- What are your latency, cost, and data-access constraints?
- How are prompt and model changes reviewed and rolled back?
- What would successful work in the first three months look like?

## Repository references and claim boundaries

- [Project overview](../README.md)
- [Patient profiles](new_syllabus/class4_patient_profiles.md)
- [Retrieval architecture](new_syllabus/class5_retrieval.md)
- [Recorded retrieval results and limitations](new_syllabus/class5_engineering_results.md)
- [Assessment contracts and semantics](new_syllabus/class6_assessment.md)
- [Evidence agent, MCP comparison, and limits](new_syllabus/class7_evidence_agent.md)
- [Current evidence graph](../src/clinical_trial_patient_matching/evidence_agent.py)
- [Retrieval evaluation implementation](../src/clinical_trial_patient_matching/retrieval_evaluation.py)
- [Class 8 planned requirements](../class_material/class8.md)

Do not claim production deployment, clinical validation, general agent reliability, independent labeling, or completed Class 8 features without additional evidence. Recorded local timings are machine-specific observations. This guide covers stable concepts and your repository; it is not a survey of current job openings, provider prices, or changing API signatures.
