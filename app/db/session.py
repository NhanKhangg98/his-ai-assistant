"""Database engine, session factory, schema initialization, and default user seeding."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Base, User

DEFAULT_USERS: tuple[tuple[str, str, str], ...] = (
    ("ba_user", "BA", "Chuyen vien Phan tich Nghiep vu 315 (BA)"),
    ("qc_user", "QC", "Chuyen vien Kiem thu Chat luong 315 (QC)"),
    ("dev_user", "Dev", "Ky su Phat trien Phan mem 315 (Dev)"),
)

_ENGINE_CACHE: dict[str, Engine] = {}


def get_engine(database_url: str | None = None) -> Engine:
    """Create or retrieve cached SQLAlchemy 2.0 Engine."""
    url = database_url or get_settings().database_url
    if url not in _ENGINE_CACHE:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _ENGINE_CACHE[url] = create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
    return _ENGINE_CACHE[url]


def get_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    """Return a configured SessionMaker bound to the engine."""
    engine = get_engine(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def seed_default_users(session: Session) -> None:
    """Ensure standard BA, QC, and Dev users exist in the users table."""
    for user_id, role, display_name in DEFAULT_USERS:
        existing = session.scalar(select(User).where(User.user_id == user_id))
        if existing is None:
            session.add(
                User(
                    user_id=user_id,
                    role=role,
                    display_name=display_name,
                    is_active=True,
                )
            )
    session.commit()


def init_db(database_url: str | None = None) -> None:
    """Initialize database extensions, tables, PostgreSQL HNSW/GIN indexes, and seed users."""
    engine = get_engine(database_url)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding_hnsw "
                    "ON kb_chunks USING hnsw (embedding vector_cosine_ops);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_kb_chunks_tsv_gin "
                    "ON kb_chunks USING gin (content_tsv);"
                )
            )
    factory = get_session_factory(database_url)
    with factory() as session:
        seed_default_users(session)


@contextmanager
def session_scope(database_url: str | None = None) -> Generator[Session, None, None]:
    """Transactional session context manager."""
    factory = get_session_factory(database_url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def get_db_session() -> AsyncGenerator[Session, None]:
    """FastAPI async dependency yielding a database session without threadpool overhead."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()
