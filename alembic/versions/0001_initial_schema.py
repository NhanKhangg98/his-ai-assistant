"""Initial schema with pgvector extension, 10 tables, HNSW/GIN indexes, and default users.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-14 00:00:00
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from alembic import op
from app.db.models import Base
from app.db.session import seed_default_users

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create pgvector extension (on PostgreSQL), all 10 tables, indexes, and default users."""
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == "postgresql":
        op.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))

    Base.metadata.create_all(bind=bind)

    if dialect_name == "postgresql":
        op.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding_hnsw "
                "ON kb_chunks USING hnsw (embedding vector_cosine_ops);"
            )
        )
        op.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_kb_chunks_tsv_gin "
                "ON kb_chunks USING gin (content_tsv);"
            )
        )

    session = Session(bind=bind)
    try:
        seed_default_users(session)
    finally:
        session.close()


def downgrade() -> None:
    """Drop all 10 tables."""
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
