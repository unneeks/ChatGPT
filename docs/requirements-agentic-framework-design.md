# Agentic Requirements Gathering & Analysis Framework — Architecture & Design

**Context:** Retail/commercial bank. Jira is the front door for all demand intake.
**Goal:** An agentic framework that gathers, analyzes, and structures requirements for the SDLC,
with verifiable, traceable, auditable output and a **human-on-the-loop** operating model.
**Status:** Design only — no implementation in this document.

---

## 1. Design Attributes (carried forward from prior design notes)

These attributes are inherited from two prior design artifacts and govern every component below:

### From "Model Design Principles" (eventual consistency / state / idempotency / early response)

| Attribute | Application in this framework |
|---|---|
| Idempotency | Every agent tool call carries an idempotency key; re-running a stage never duplicates Jira comments, stories, or audit records. Write-once artifacts. |
| State management | Agent working memory is run-scoped and serializable; authoritative state lives in a durable workflow store, never in the agent process. Persist outputs, not process. |
| State machine discipline | Requirement lifecycle is an explicit state machine; illegal transitions rejected at the boundary. |
| Early response model | Long-running agent tasks return a `job_id` immediately (202-style); Jira is updated asynchronously. Status granularity: `pending → queued → running → review → complete/failed`. |
| Eventual consistency | Agents tolerate stale reads from Jira/Confluence; version/timestamp every artifact; correct forward (compensating updates), never silent overwrite. |
| Checkpointing | Every pipeline stage checkpoints to durable storage; any run is resumable and replayable. |

### From "2026 — mindset" (governance constitution)

| Principle | Application in this framework |
|---|---|
| Governance must be executable | Requirement quality rules compile into pipeline gates (policy-as-code), not documents. Test: *can an agent violate the rule by accident?* If yes, the gate is decorative. |
| Deterministic vs. probabilistic separation | Hard gates (PII, access control, mandatory fields, regulatory tagging) are deterministic and must never break. Quality signals (ambiguity, completeness scores) are probabilistic and route by threshold. |
| Runtime feedback loop | Verification is continuous sensing → decision → correction → learning, not one-time approval. |
| Autonomy by default, humans as exception handlers | Human-on-the-loop: reviewers handle exceptions and risk-tiered approvals, not every artifact. Safe defaults, cheap rollback, containable violations. |
| Metadata as operational intelligence | Provenance, confidence scores, and lineage actively route work (e.g., low-confidence → human queue); they are not passive records. |
| Failure as first-class | Quarantine states, confidence degradation (not binary stop), explainability hooks, incident playbooks for *governance* failures. |
| Control plane over tools | One orchestration/policy control plane; Jira, LLMs, and knowledge stores are replaceable executors. |
| Governance as friction reduction | The agent reduces BA rework; review burden must be measurably lower than manual elicitation, or adoption fails. |

### Requirement-quality attributes (target output standard)

Every produced requirement must be: **unambiguous, testable/verifiable, complete, consistent, atomic, traceable, prioritized, feasibility-annotated**, with INVEST-conformant stories and Given/When/Then acceptance criteria. Every assertion must carry a **citation to a source artifact** (Jira text, document section, transcript span). Uncited assertions are blocked by gate.

---

## 2. Operating Model: Human-ON-the-Loop (not in-the-loop everywhere)

Humans supervise the system and adjudicate exceptions; they do not gate every step. Approval is **risk-tiered**:

| Risk tier | Examples | Routing |
|---|---|---|
| Low | Internal tooling, copy changes, low-data-sensitivity | Auto-advance after gates pass; **sampled** human QA (e.g., 10%) |
| Medium | Standard feature work, single-system impact | Single reviewer (BA/PO) approves in Jira |
| High | Regulatory, payments, customer-impacting, PII/model-risk | **Maker–checker**: drafting reviewer + independent approver (two-person rule, bank standard) |

- Risk tier is assigned by a deterministic classifier rule set (data sensitivity, regulatory keywords, system criticality from CMDB) — never solely by the LLM.
- Every human decision (approve / edit / reject / escalate) is captured as a signed, timestamped event with reviewer identity and diff.
- SLA timers with escalation; an unreviewed item never silently expires — it transitions to `escalated`.
- Reviewer **overrides feed the learning loop**: edits are diffed against agent output to recalibrate rubrics and prompts.

---

## 3. Agent Topology

A pipeline of specialized agents under one orchestrator. Drafting and judging are **separated** (different prompts, ideally different models) to avoid self-grading bias.

```
                          ┌─────────────────────────────────────────────┐
 Jira (front door)        │            ORCHESTRATION CONTROL PLANE       │
 epic/initiative ─webhook─▶  durable workflow engine · state machine     │
 created/updated          │  checkpoints · idempotency keys · job_ids    │
                          └──────┬──────────────────────────────────────┘
                                 │
   ┌────────────┬────────────────┼──────────────────┬───────────────────┐
   ▼            ▼                ▼                  ▼                   ▼
 1.Intake &   2.Context        3.Elicitation      4.Analyst /         5.Compliance
   Triage       Retrieval        Agent              Synthesizer         & Risk Agent
   agent        agent (RAG)      (clarifying        (epics, stories,    (reg mapping,
   (classify,   (Confluence,     questions back     ACs, NFRs, data     risk tier,
   completeness SharePoint,      into Jira to       reqs, glossary      2nd-line flags)
   check)       CMDB, policy     stakeholders)      terms)
                corpus, backlog)
                                 │
                                 ▼
                  ┌──────────────────────────────────┐
                  │   6. VERIFICATION LAYER           │
                  │  a) deterministic policy gates    │
                  │  b) critic/LLM-judge ensemble     │
                  │  c) grounding & citation checker  │
                  │  d) duplicate/contradiction check │
                  └──────────────┬───────────────────┘
                                 ▼
                  ┌──────────────────────────────────┐
                  │   7. HUMAN-ON-THE-LOOP            │
                  │  risk-tiered routing · maker-     │
                  │  checker · sampling QA · sign-off │
                  └──────────────┬───────────────────┘
                                 ▼
                  ┌──────────────────────────────────┐
                  │   8. PUBLISH & TRACE              │
                  │  write stories/links to Jira ·    │
                  │  update RTM graph · audit ledger  │
                  └──────────────────────────────────┘
```

**Agent responsibilities**

1. **Intake & Triage** — listens to Jira webhooks; validates against an intake schema (problem statement, business outcome, sponsor, impacted systems, data touched); classifies demand type; assigns preliminary risk tier (deterministic rules). Incomplete intake → structured questions posted as Jira comments, state `awaiting-input`.
2. **Context Retrieval** — ACL-aware RAG over Confluence/SharePoint, policy/regulatory corpus, CMDB, enterprise glossary, and the existing Jira backlog (for duplicates/conflicts). Returns *cited* context bundles only.
3. **Elicitation** — generates targeted clarifying questions per stakeholder role, posted into Jira (and optionally email/Teams); ingests answers and meeting transcripts; tracks open-question debt.
4. **Analyst / Synthesizer** — drafts epics, user stories, Given/When/Then acceptance criteria, NFRs (security, availability, performance, data retention), data requirements, and assumption/decision logs. Every statement carries inline provenance references.
5. **Compliance & Risk** — maps requirements to regulatory obligations from the policy corpus, confirms/raises the risk tier, and flags items requiring second-line (risk/compliance) review.
6. **Verification layer** — see §4.
7. **Human review** — see §2.
8. **Publish & Trace** — writes approved artifacts back to Jira (issues, links, custom fields), updates the requirements traceability matrix, emits audit events.

---

## 4. Verification Design — how output is proven reliable

Five layers; an artifact must pass all applicable layers before publication.

**Layer 1 — Deterministic gates (must never break; policy-as-code, e.g., OPA-style):**
- Intake/output schema validation; mandatory fields present
- PII / secret / customer-data leakage scan on all generated text
- Glossary conformance (terms must resolve to the enterprise glossary)
- Risk-tier and regulatory-tag presence
- Structural lint: story atomicity, AC in Given/When/Then, no TBD/TODO placeholders

**Layer 2 — Probabilistic quality scoring (thresholded, routed not blocked):**
- Ambiguity score (vague quantifiers, escape clauses, passive voice on actors)
- Completeness vs. intake intent (coverage of stated outcomes)
- Internal contradiction detection across the story set
- Duplicate/conflict detection vs. existing backlog
- **LLM-judge ensemble with a published rubric**, run on a *different model/prompt lineage* than the drafting agent; scores below threshold → human queue with the judge's reasoning attached

**Layer 3 — Grounding & citation check:**
- Every requirement claim must cite ≥1 source artifact (Jira text span, document section, transcript segment)
- Citations are resolved and verified to exist and to actually support the claim (entailment check)
- Orphan (uncited) assertions are hard-blocked → returned to elicitation as open questions, never silently invented

**Layer 4 — Human verification (per §2):** risk-tiered approval, maker–checker for high tier, sampling QA on auto-passed items, signed decisions.

**Layer 5 — Continuous evaluation (the runtime feedback loop):**
- **Golden dataset**: curated set of historical intakes with known-good requirement outputs; every prompt/model/policy change runs regression against it before promotion
- Drift monitoring on quality scores and reviewer-override rates
- Downstream signal: defect/rework rates and "requirement defect escape" tied back to originating requirement via the RTM — closes the loop from build/test back into rubric and prompt tuning
- All eval results versioned and stored; promotion of any prompt/model is itself an audited, approved change

---

## 5. Traceability & Audit (regulator-grade)

**Requirements Traceability Matrix (RTM), stored as a graph:**

```
Business need (Jira initiative)
  └─ Epic ── Story ── Acceptance criterion ── Test case ── Build/Release
       │        │
       │        └─ Source citations (doc §, comment, transcript span)
       └─ Regulatory obligation(s) · Risk tier · NFRs · Decisions/Assumptions
```

Links are bidirectional and queryable both ways: "what does obligation X touch?" and "why does story Y exist?"

**Immutable audit ledger (append-only, WORM-class storage):**
- Every agent action: input hash, output hash, **prompt version, model ID/version, policy version**, retrieval set used, timestamps, job_id
- Every gate result and judge score with reasoning
- Every human decision with identity, role, and diff
- Supports full **replay**: for any requirement, reconstruct exactly how it came to exist, what evidence it used, who approved it, under which policy version. This is the artifact you hand an internal auditor or regulator.

**Versioning triple:** every published artifact records `(prompt_version, model_version, policy_version)`. Nothing is generated by an unversioned configuration.

---

## 6. Recommended Solution Architecture Components

| Capability | Recommendation | Notes |
|---|---|---|
| Front door / UX | **Jira (existing)** + webhooks/Connect app; custom fields for risk tier, confidence, provenance link; Jira approval workflow for sign-off | Keep humans in the tool they already use — friction reduction |
| Orchestration control plane | Durable workflow engine — **Temporal** (or AWS Step Functions if cloud-native; LangGraph acceptable inside a single agent run) | Gives checkpointing, retries, idempotent activities, explicit state machine out of the box |
| LLM access | **Central LLM gateway** (model routing, logging, rate limits, PII redaction in/out, no-training guarantees) fronting bank-approved models via **AWS Bedrock / Azure / GCP Vertex** private endpoints | One choke point = one place to audit and to enforce policy |
| Knowledge layer | ACL-aware retrieval service: vector + keyword index over Confluence/SharePoint/policy corpus/CMDB; **enterprise glossary/ontology** as first-class service | Retrieval must respect the asker's entitlements, not the agent's |
| Policy-as-code | **OPA (Rego)** or equivalent rules engine for all Layer-1 gates and risk-tier classification | Deterministic, testable, versioned in git |
| Evaluation service | Eval harness (golden sets, rubric judges, regression on change) — promptfoo-class tooling or in-house; results persisted | Required before any prompt/model promotion |
| Audit ledger | Append-only event store: **Kafka → WORM object storage (S3 Object Lock)** or immutable ledger DB | Regulator replay requirement |
| Traceability store | Graph DB (**Neo4j**/Neptune) holding the RTM, synced with Jira issue links | Jira links alone are insufficient for cross-cutting queries |
| Observability | OpenTelemetry tracing across agents; dashboards for KPIs (below); alerting on drift/gate-failure spikes | |
| Identity & security | SSO/OIDC, RBAC on every surface; agents run with **least-privilege service identities**, distinct per agent; secrets in vault; data residency per bank policy | |
| Human review surface | Jira-native first; optional thin review console showing side-by-side draft vs. sources vs. judge reasoning | Build only if Jira proves insufficient |

**Deliberately deferred:** fine-tuned models (start with retrieval + prompting), autonomous publication of high-tier items (always maker–checker), and any agent-initiated external communication beyond Jira comments.

---

## 7. KPIs

- Requirement defect escape rate (defects traced to requirements, via RTM)
- Reviewer override/edit rate per tier (proxy for agent reliability; drives tier thresholds)
- % artifacts auto-passed vs. human-corrected; sampling-QA failure rate
- Cycle time: intake → approved requirements (vs. manual baseline)
- Open-question debt per epic; citation coverage (must trend to 100%)
- Audit completeness: % artifacts with full replayable lineage (target: 100%, hard requirement)

---

## 8. Phased Rollout

1. **Phase 0 — Foundations:** Jira intake schema, LLM gateway, audit ledger, policy gates, golden-set seed from historical epics.
2. **Phase 1 — Shadow mode:** agents draft everything; humans review 100%; build eval baselines and calibrate judges against human edits. No autonomy.
3. **Phase 2 — Assisted:** low-tier auto-advance with sampling QA; medium/high tiers human-approved. Tune thresholds on override-rate evidence.
4. **Phase 3 — Human-on-the-loop at scale:** steady-state per §2; quarterly model/prompt recertification through the eval harness; governance incident playbooks live.

Promotion between phases is gated on KPI evidence (override rate, escape rate), not on time.
