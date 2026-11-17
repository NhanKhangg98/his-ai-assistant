"""Pytest fixtures configuring isolated database, sample staging repo, and FastAPI Test Client."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.adapters.discord_adapter import get_discord_adapter
from app.adapters.git_adapter import LocalRepoGitAdapter
from app.config import get_settings
from app.db.models import Base
from app.db.session import get_engine, get_session_factory, init_db
from app.main import (
    ChatCommandRequest,
    SearchRequest,
    app,
    get_audit_logs,
    get_story_state,
    get_task_status,
    get_worker,
    health_check,
    post_chat_command,
    search_kb,
)
from sample_data.init_sample_repo import ensure_sample_git_repo


class DirectFastAPIClient:
    """FastAPI test client executing routes & Pydantic validation synchronously."""

    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self._session_factory = get_session_factory(db_url)

    def _make_response(self, status_code: int, payload: Any) -> httpx.Response:
        if isinstance(payload, JSONResponse):
            return httpx.Response(
                status_code=payload.status_code,
                content=payload.body,
                headers={"content-type": "application/json"},
            )
        return httpx.Response(status_code=status_code, json=payload)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        parsed = urlparse(url)
        path = parsed.path
        qs = parse_qs(parsed.query)

        with self._session_factory() as session:
            try:
                if path == "/health":
                    return self._make_response(200, health_check())
                if path.startswith("/tasks/"):
                    task_id = path.split("/tasks/", 1)[1]
                    return self._make_response(200, get_task_status(task_id, session=session))
                if path.startswith("/stories/"):
                    story_key = path.split("/stories/", 1)[1]
                    return self._make_response(200, get_story_state(story_key, session=session))
                if path == "/audit-logs":
                    story_key = qs.get("story_key", [None])[0]
                    limit = int(qs.get("limit", ["100"])[0])
                    return self._make_response(
                        200, get_audit_logs(story_key=story_key, limit=limit, session=session)
                    )
                return self._make_response(404, {"detail": "Not Found"})
            except HTTPException as exc:
                return self._make_response(exc.status_code, {"detail": exc.detail})

    def post(self, url: str, json: dict[str, Any] | None = None, **kwargs: Any) -> httpx.Response:
        parsed = urlparse(url)
        path = parsed.path
        body = json or {}

        with self._session_factory() as session:
            try:
                if path == "/search":
                    req = SearchRequest.model_validate(body)
                    return self._make_response(200, search_kb(req, session=session))
                if path == "/chat/commands":
                    req_cmd = ChatCommandRequest.model_validate(body)
                    return self._make_response(200, post_chat_command(req_cmd, session=session))
                return self._make_response(404, {"detail": "Not Found"})
            except ValidationError as v_err:
                return self._make_response(422, {"detail": v_err.errors()})
            except HTTPException as exc:
                return self._make_response(exc.status_code, {"detail": exc.detail})


def _cleanup_sample_repo_bot_branches(repo_dir: Path) -> None:
    """Remove any leftover bot/* branches and mock PR state so his_staging stays pristine."""
    import subprocess

    git_dir = repo_dir / ".git"
    if not git_dir.exists():
        return
    subprocess.run(
        ["git", "checkout", "staging"], cwd=str(repo_dir), check=False, capture_output=True
    )
    proc = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/bot/"],
        cwd=str(repo_dir),
        check=False,
        capture_output=True,
        text=True,
    )
    for br in proc.stdout.splitlines():
        br_name = br.strip()
        if br_name:
            subprocess.run(
                ["git", "branch", "-D", br_name],
                cwd=str(repo_dir),
                check=False,
                capture_output=True,
            )
    pr_file = git_dir / "mock_pull_requests.json"
    if pr_file.exists():
        pr_file.unlink()


@pytest.fixture(scope="session", autouse=True)
def setup_sample_git_repo_session() -> None:
    """Ensure his_staging sample repository is present before any test runs."""
    repo_dir = (Path(__file__).resolve().parent.parent / "sample_data" / "his_staging").resolve()
    ensure_sample_git_repo(repo_dir)


@pytest.fixture()
def test_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    """Configure an isolated database per test and clean up sample repo branches on teardown."""
    db_file = tmp_path / "his_test.db"
    db_url = os.environ.get("TEST_POSTGRES_URL", f"sqlite:///{db_file.as_posix()}")
    repo_dir = (Path(__file__).resolve().parent.parent / "sample_data" / "his_staging").resolve()
    ensure_sample_git_repo(repo_dir)
    _cleanup_sample_repo_bot_branches(repo_dir)

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("HIS_REPO_PATH", str(repo_dir))
    monkeypatch.setenv("ENABLE_BACKGROUND_WORKER", "false")
    monkeypatch.setenv("LLM_SIMULATE_PRIMARY_FAILURE", "false")
    get_settings.cache_clear()

    engine = get_engine(db_url)
    Base.metadata.drop_all(bind=engine)
    init_db(db_url)
    get_discord_adapter().clear()

    worker = get_worker()
    worker.database_url = db_url
    worker.git_adapter = LocalRepoGitAdapter(repo_dir)
    try:
        yield db_url
    finally:
        _cleanup_sample_repo_bot_branches(repo_dir)


@pytest.fixture()
def db_session(test_db_url: str) -> Generator[Session, None, None]:
    """Yield a SQLAlchemy Session bound to the test database."""
    factory = get_session_factory(test_db_url)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(test_db_url: str) -> DirectFastAPIClient:
    """Return a FastAPI test client bound to the test database."""
    _ = app  # Ensure FastAPI app is loaded
    return DirectFastAPIClient(test_db_url)
