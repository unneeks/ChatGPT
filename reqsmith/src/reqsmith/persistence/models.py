"""Database schema.

Discipline (from the design doc §2/§5):
- audit_events and outreach_events are APPEND-ONLY. The repository layer only exposes
  INSERT for them, and migration 001 adds a Postgres trigger blocking UPDATE/DELETE.
- artifacts are write-once per (run_id, kind, version): a change is a new version row.
- Every external side effect is keyed by a natural-key UNIQUE constraint so retries
  collapse into no-ops (ON CONFLICT DO NOTHING + read-back).
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class RunState(enum.StrEnum):
    INTAKE = "intake"
    TRIAGE = "triage"
    AWAITING_INPUT = "awaiting_input"
    RETRIEVAL = "retrieval"
    ELICITATION = "elicitation"
    DRAFTING = "drafting"
    VERIFICATION = "verification"
    REVIEW = "review"
    CHECKER_REVIEW = "checker_review"
    PUBLISHING = "publishing"
    COMPLETE = "complete"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    ESCALATED = "escalated"


class JobStatus(enum.StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    REVIEW = "review"
    COMPLETE = "complete"
    FAILED = "failed"


class RiskTier(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    jira_issue_key: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[RunState] = mapped_column(
        Enum(RunState, values_callable=lambda e: [m.value for m in e]),
        default=RunState.INTAKE,
    )
    risk_tier: Mapped[RiskTier | None] = mapped_column(
        Enum(RiskTier, values_callable=lambda e: [m.value for m in e]), nullable=True
    )
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    prompt_pack_version: Mapped[str] = mapped_column(String(32))
    policy_version: Mapped[str] = mapped_column(String(32))
    # natural key: issue + trigger content hash → replayed webhook never creates a 2nd run
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("run_id", "stage", "attempt", name="uq_job_stage_attempt"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(64))
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, values_callable=lambda e: [m.value for m in e]),
        default=JobStatus.PENDING,
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("run_id", "kind", "version", name="uq_artifact_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(48))  # intake_snapshot|context_bundle|draft_story|...
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceDocument(Base):
    __tablename__ = "source_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    origin: Mapped[str] = mapped_column(String(32))  # jira_description|jira_comment|attachment|...
    external_ref: Mapped[str] = mapped_column(String(512))
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Citation(Base):
    __tablename__ = "citations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    claim_path: Mapped[str] = mapped_column(String(512))  # JSON pointer into artifact content
    source_document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"))
    span_start: Mapped[int] = mapped_column(Integer)
    span_end: Mapped[int] = mapped_column(Integer)
    entailment_verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    # cross-channel idempotency key, embedded in Jira comments as [REQ-Q:<uuid>]
    question_id: Mapped[str] = mapped_column(String(64), unique=True)
    text: Mapped[str] = mapped_column(Text)
    stakeholder_aad_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stakeholder_display: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")
    current_rung: Mapped[int] = mapped_column(Integer, default=1)
    sla_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_documents.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class OutreachEvent(Base):
    """APPEND-ONLY — repository exposes insert only; Postgres trigger blocks UPDATE/DELETE."""

    __tablename__ = "outreach_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("questions.question_id"), index=True)
    channel: Mapped[str] = mapped_column(String(24))  # jira_comment|teams_card|meeting_invite|...
    direction: Mapped[str] = mapped_column(String(4))  # out|in
    payload_hash: Mapped[str] = mapped_column(String(64))
    external_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GateResult(Base):
    __tablename__ = "gate_results"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    layer: Mapped[int] = mapped_column(Integer)  # 1 deterministic | 2 probabilistic | 3 grounding
    rule_id: Mapped[str] = mapped_column(String(128))
    verdict: Mapped[str] = mapped_column(String(8))  # pass|fail
    score: Mapped[float | None] = mapped_column(Numeric(6, 3), nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    # one decision per (run, artifact, role, reviewer) — replayed webhooks / double-clicks collapse
    __table_args__ = (
        UniqueConstraint("run_id", "artifact_id", "role", "reviewer_identity", name="uq_approval"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"))
    role: Mapped[str] = mapped_column(String(16))  # maker|checker|reviewer
    decision: Mapped[str] = mapped_column(String(16))  # approve|edit|reject|escalate
    reviewer_identity: Mapped[str] = mapped_column(String(256))
    diff: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    jira_transition_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RtmEdge(Base):
    __tablename__ = "rtm_edges"
    __table_args__ = (
        UniqueConstraint(
            "from_type", "from_ref", "to_type", "to_ref", "edge_kind", name="uq_rtm_edge"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    from_type: Mapped[str] = mapped_column(String(32), index=True)
    from_ref: Mapped[str] = mapped_column(String(256), index=True)
    to_type: Mapped[str] = mapped_column(String(32), index=True)
    to_ref: Mapped[str] = mapped_column(String(256), index=True)
    edge_kind: Mapped[str] = mapped_column(String(24))  # derives_from|cites|satisfies|tested_by
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    """APPEND-ONLY — the regulator-replay table. Never updated, never deleted."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor: Mapped[str] = mapped_column(String(128))  # agent name | human identity | system
    action: Mapped[str] = mapped_column(String(128))
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemFlag(Base):
    """Small key/value control table (e.g. outreach kill switch, last cron tick)."""

    __tablename__ = "system_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
