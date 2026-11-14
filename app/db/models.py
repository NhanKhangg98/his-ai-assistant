"""SQLAlchemy 2.0 ORM Models for HIS Knowledge Base and 7-Step Workflow Assistant."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def new_uuid_str() -> str:
    """Generate UUID4 string primary key."""
    return str(uuid.uuid4())


class EmbeddingVector(TypeDecorator[list[float]]):
    """Dialect-aware 128-dim embedding type (pgvector Vector on PostgreSQL, JSON on SQLite)."""

    impl = JSON
    cache_ok = True

    def __init__(self, dim: int = 128) -> None:
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Vector(self.dim))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: list[float] | None, dialect: Any) -> Any:
        if value is None:
            return None
        return [float(x) for x in value]

    def process_result_value(self, value: Any, dialect: Any) -> list[float] | None:
        if value is None:
            return None
        return [float(x) for x in value]


JsonVariant = JSON().with_variant(JSONB(), "postgresql")
TsvVariant = Text().with_variant(TSVECTOR(), "postgresql")


class Base(DeclarativeBase):
    """Declarative base for all 10 database tables."""


class User(Base):
    """Table 1: System users mapped to RBAC roles (BA, QC, Dev)."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('BA', 'QC', 'Dev')", name="ck_users_role"),
        Index("idx_users_role", "role"),
    )

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class IngestionRun(Base):
    """Table 2: Tracks incremental ingestion runs and staging branch commit SHA."""

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING', 'SUCCESS', 'FAILED')", name="ck_ingestion_runs_status"
        ),
        Index("idx_ingestion_runs_started_at", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid_str)
    branch: Mapped[str] = mapped_column(String(64), default="staging", nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    repo_path: Mapped[str] = mapped_column(String(512), nullable=False)
    files_added: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    files_modified: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    files_deleted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    files_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunks_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    code_maps_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    secrets_redacted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pii_masked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KbFile(Base):
    """Table 3: Indexed files with SHA-256 hash for incremental ingestion."""

    __tablename__ = "kb_files"
    __table_args__ = (
        UniqueConstraint("file_path", name="uq_kb_files_file_path"),
        Index("idx_kb_files_his_module", "his_module"),
        Index("idx_kb_files_hash", "file_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid_str)
    last_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("ingestion_runs.id", ondelete="SET NULL"), nullable=True
    )
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(16), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    his_module: Mapped[str] = mapped_column(String(64), nullable=False)
    last_commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    chunks: Mapped[list[KbChunk]] = relationship(
        "KbChunk", back_populates="file", cascade="all, delete-orphan"
    )
    code_map_entries: Mapped[list[CodeMapEntry]] = relationship(
        "CodeMapEntry", back_populates="file", cascade="all, delete-orphan"
    )


class KbChunk(Base):
    """Table 4: DLP-sanitized knowledge chunks (doc, code_map, source_code) with 128-d vector."""

    __tablename__ = "kb_chunks"
    __table_args__ = (
        CheckConstraint(
            "chunk_type IN ('doc', 'code_map', 'source_code')",
            name="ck_kb_chunks_chunk_type",
        ),
        Index("idx_kb_chunks_rbac", "chunk_type", "his_module"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid_str)
    file_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kb_files.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_type: Mapped[str] = mapped_column(String(32), nullable=False)
    his_module: Mapped[str] = mapped_column(String(64), nullable=False)
    heading_or_symbol: Mapped[str] = mapped_column(String(256), nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    lexical_tokens: Mapped[str] = mapped_column(Text, default="", nullable=False)
    embedding: Mapped[list[float]] = mapped_column(EmbeddingVector(128), nullable=False)
    content_tsv: Mapped[str | None] = mapped_column(TsvVariant, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JsonVariant, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    file: Mapped[KbFile] = relationship("KbFile", back_populates="chunks")


class CodeMapEntry(Base):
    """Table 5: Extracted API routes, controllers, and entities from staging source code."""

    __tablename__ = "code_map_entries"
    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('api_endpoint', 'entity', 'component')",
            name="ck_code_map_entry_type",
        ),
        Index("idx_code_map_module_type", "his_module", "entry_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid_str)
    file_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("kb_files.id", ondelete="CASCADE"), nullable=False
    )
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)
    http_method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    route_path: Mapped[str | None] = mapped_column(String(256), nullable=True)
    controller_or_class: Mapped[str] = mapped_column(String(128), nullable=False)
    action_or_symbol: Mapped[str] = mapped_column(String(128), nullable=False)
    his_module: Mapped[str] = mapped_column(String(64), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)

    file: Mapped[KbFile] = relationship("KbFile", back_populates="code_map_entries")


class WorkflowStory(Base):
    """Table 6: Tracks 7-step state machine per Jira story."""

    __tablename__ = "workflow_stories"
    __table_args__ = (
        CheckConstraint("current_step BETWEEN 1 AND 7", name="ck_workflow_stories_step"),
        CheckConstraint(
            "step_status IN ('WAITING_AGENT', 'RUNNING_AGENT', 'WAITING_APPROVAL', 'DONE')",
            name="ck_workflow_stories_status",
        ),
        Index("idx_workflow_stories_step", "current_step", "step_status"),
    )

    story_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    step_status: Mapped[str] = mapped_column(String(32), default="WAITING_AGENT", nullable=False)
    git_branch: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pr_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pr_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    loop_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    artifacts_json: Mapped[dict[str, Any]] = mapped_column(
        JsonVariant, default=dict, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ProcessedMessage(Base):
    """Table 7: Stripe-style idempotency key store with payload_sha256 conflict detection."""

    __tablename__ = "processed_messages"
    __table_args__ = (Index("idx_processed_messages_created", "created_at"),)

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    sanitized_text: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JsonVariant, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class BackgroundTask(Base):
    """Table 8: Durable asyncio worker queue with lease expiration for crash recovery."""

    __tablename__ = "background_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_background_tasks_status",
        ),
        Index("idx_bg_tasks_poll", "status", "lease_expires_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid_str)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    story_key: Mapped[str] = mapped_column(
        String(32), ForeignKey("workflow_stories.story_key", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.user_id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    target_step: Mapped[int] = mapped_column(Integer, nullable=False)
    action_name: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JsonVariant, default=dict, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JsonVariant, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class WorkflowAuditLog(Base):
    """Table 9: Append-only audit log for every command and step transition."""

    __tablename__ = "workflow_audit_logs"
    __table_args__ = (Index("idx_audit_story_created", "story_key", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid_str)
    story_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_role: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    from_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome_status: Mapped[str] = mapped_column(String(32), nullable=False)
    sanitized_command: Mapped[str] = mapped_column(Text, nullable=False)
    detail_json: Mapped[dict[str, Any]] = mapped_column(JsonVariant, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class LlmUsageLedger(Base):
    """Table 10: Tracks monthly token and USD cost usage per user and role."""

    __tablename__ = "llm_usage_ledgers"
    __table_args__ = (
        CheckConstraint(
            "total_tokens = prompt_tokens + completion_tokens",
            name="ck_llm_usage_tokens_sum",
        ),
        CheckConstraint("cost_usd >= 0", name="ck_llm_usage_cost_nonneg"),
        Index("idx_llm_usage_user_month", "user_id", "usage_month"),
        Index("idx_llm_usage_role_month", "user_role", "usage_month"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid_str)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.user_id"), nullable=False)
    user_role: Mapped[str] = mapped_column(String(16), nullable=False)
    story_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    usage_month: Mapped[str] = mapped_column(String(7), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    used_fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
