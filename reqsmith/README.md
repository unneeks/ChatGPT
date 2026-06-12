# reqsmith

Agentic requirements gathering & analysis framework — MVP shadow mode + Teams elicitation.
Design: [`../docs/requirements-agentic-framework-design.md`](../docs/requirements-agentic-framework-design.md).

Jira is the front door. The pipeline: intake → triage → retrieval → elicitation → drafting →
verification (deterministic gates + LLM judges + grounding) → human review (maker–checker) →
publish back to Jira with full traceability (RTM edges) and an append-only audit ledger.

## Architecture invariants

- **State machine discipline** — `orchestrator/state_machine.py` enumerates every legal transition;
  `RunRepo.transition()` is the only way state changes.
- **Idempotency everywhere** — natural-key UNIQUE + insert-or-noop + read-back; replayed webhooks,
  retried stages, and double-clicked approvals never duplicate side effects.
- **Append-only audit** — `audit_events` / `outreach_events` are INSERT-only (DB trigger on
  Postgres); every event carries the `(prompt_version, model_id, policy_version)` triple.
- **Deterministic vs probabilistic separation** — risk tiers and policy gates are YAML + code
  (`config/policies/`); LLM agents (CrewAI crews) never set a tier or bypass a gate.
- **Early response** — webhooks return `202 + job_id` immediately; work is async
  (`GET /jobs/{id}` for status). Safe on scale-to-zero Azure Container Apps.

## Dev quickstart

```bash
cd reqsmith
uv venv && uv pip install -e ".[dev]"
pytest -q && ruff check src tests alembic

# full stack with Postgres
cp .env.example .env
docker compose up --build
```

Heavy optional stacks: `pip install -e ".[agents]"` (CrewAI) and `".[outreach]"`
(Bot Framework + MSAL) — core pipeline tests run without them.

## Layout

See the design doc §6 and the implementation plan. Key seams kept swappable:
`orchestrator/engine.py` (→ Temporal later), `audit/ledger.py` (→ Kafka/WORM later),
`persistence` RTM tables (→ Neo4j later), `adapters/*` (every external system behind a Protocol
with an in-memory fake).
