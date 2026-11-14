"""FastAPI Application exposing POST /search, POST /chat/commands, and inspection endpoints."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import BackgroundTask, WorkflowAuditLog, WorkflowStory
from app.db.session import get_db_session, init_db
from app.kb.search_service import HybridSearchService
from app.security.dlp import AllBoundaryDLP
from app.security.log_filter import install_global_log_sanitizer
from app.workflow.command_parser import CommandParseError
from app.workflow.state_machine import (
    IdempotencyPayloadMismatchError,
    RoleForbiddenError,
    StepConflictError,
    WorkflowStateMachine,
)
from app.workflow.worker import DurableAsyncioWorker
from sample_data.init_sample_repo import ensure_sample_git_repo

install_global_log_sanitizer()

_WORKER_INSTANCE: DurableAsyncioWorker | None = None


def get_worker() -> DurableAsyncioWorker:
    """Return global DurableAsyncioWorker instance."""
    global _WORKER_INSTANCE
    if _WORKER_INSTANCE is None:
        _WORKER_INSTANCE = DurableAsyncioWorker()
    return _WORKER_INSTANCE


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize DB, sample staging repo, and start DurableAsyncioWorker on startup."""
    install_global_log_sanitizer()
    settings = get_settings()
    ensure_sample_git_repo(settings.his_repo_path)
    init_db(settings.database_url)
    worker = get_worker()
    worker.database_url = settings.database_url
    worker.recover_interrupted_tasks(force_reclaim_running=True)
    if settings.enable_background_worker:
        worker.start()
    try:
        yield
    finally:
        if settings.enable_background_worker:
            await worker.stop()


app = FastAPI(
    title="HIS 315 AI Assistant & Knowledge Base API",
    version="0.1.0",
    lifespan=lifespan,
)


class SearchRequest(BaseModel):
    """Request schema for POST /search."""

    userId: str | None = Field(default=None, max_length=64)
    role: str | None = Field(default=None, pattern="^(BA|QC|Dev)$")
    query: str = Field(..., min_length=1, max_length=2000)
    topK: int = Field(default=5, ge=1, le=20)
    hisModule: str | None = Field(default=None, max_length=64)


class ChatCommandRequest(BaseModel):
    """Request schema for POST /chat/commands."""

    messageId: str = Field(..., min_length=1, max_length=128)
    userId: str = Field(..., min_length=1, max_length=64)
    channel: str = Field(..., min_length=1, max_length=64)
    text: str = Field(..., min_length=1, max_length=4000)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Service liveness check."""
    return {"status": "ok"}


@app.post("/search")
def search_kb(
    payload: SearchRequest,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Hybrid Vector + Full-Text Search with strict RBAC pre-filtering and citations."""
    svc = HybridSearchService(session)
    try:
        resolved_role = svc.resolve_user_role(payload.userId, payload.role)
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail={"errorCode": "ROLE_MISMATCH", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"errorCode": "INVALID_ROLE", "message": str(exc)},
        ) from exc

    return svc.search(
        query=payload.query,
        role=resolved_role,
        top_k=payload.topK,
        his_module=payload.hisModule,
    )


@app.post("/chat/commands")
def post_chat_command(
    payload: ChatCommandRequest,
    session: Session = Depends(get_db_session),
) -> JSONResponse:
    """Idempotent chat command endpoint driving the 7-step BA/QC/Dev workflow."""
    sm = WorkflowStateMachine(session)
    try:
        status_code, response_body = sm.handle_chat_command(
            message_id=payload.messageId,
            user_id=payload.userId,
            channel=payload.channel,
            text=payload.text,
        )
        return JSONResponse(status_code=status_code, content=response_body)
    except IdempotencyPayloadMismatchError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except CommandParseError as exc:
        raise HTTPException(
            status_code=400,
            detail={"errorCode": "INVALID_COMMAND_SYNTAX", "message": str(exc)},
        ) from exc
    except RoleForbiddenError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc
    except StepConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


@app.get("/tasks/{task_id}")
def get_task_status(
    task_id: str,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Retrieve status and DLP-sanitized result of a durable background task."""
    task = session.scalar(select(BackgroundTask).where(BackgroundTask.id == task_id))
    if task is None:
        raise HTTPException(status_code=404, detail={"errorCode": "TASK_NOT_FOUND"})
    return {
        "taskId": task.id,
        "messageId": task.message_id,
        "storyKey": task.story_key,
        "targetStep": task.target_step,
        "actionName": task.action_name,
        "status": task.status,
        "attempts": task.attempts,
        "result": AllBoundaryDLP.sanitize_obj(task.result_json),
        "error": AllBoundaryDLP.sanitize_text(task.error_message or "")
        if task.error_message
        else None,
    }


@app.get("/stories/{story_key}")
def get_story_state(
    story_key: str,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Retrieve current step, status, Git branch, PR status, loop count, and story artifacts."""
    key = story_key.strip().upper()
    story = session.scalar(select(WorkflowStory).where(WorkflowStory.story_key == key))
    if story is None:
        raise HTTPException(status_code=404, detail={"errorCode": "STORY_NOT_FOUND"})
    return {
        "storyKey": story.story_key,
        "title": AllBoundaryDLP.sanitize_text(story.title),
        "currentStep": story.current_step,
        "stepStatus": story.step_status,
        "gitBranch": story.git_branch,
        "prId": story.pr_id,
        "prStatus": story.pr_status,
        "loopCount": story.loop_count,
        "artifacts": AllBoundaryDLP.sanitize_obj(story.artifacts_json),
    }


@app.get("/audit-logs")
def get_audit_logs(
    story_key: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """List append-only workflow audit logs (optionally filtered by story_key)."""
    stmt = select(WorkflowAuditLog).order_by(WorkflowAuditLog.created_at.asc())
    if story_key:
        stmt = stmt.where(WorkflowAuditLog.story_key == story_key.strip().upper())
    stmt = stmt.limit(limit)
    rows = session.scalars(stmt).all()

    logs = [
        {
            "id": r.id,
            "storyKey": r.story_key,
            "messageId": r.message_id,
            "userId": r.user_id,
            "userRole": r.user_role,
            "action": r.action,
            "fromStep": r.from_step,
            "toStep": r.to_step,
            "outcomeStatus": r.outcome_status,
            "sanitizedCommand": AllBoundaryDLP.sanitize_text(r.sanitized_command),
            "detail": AllBoundaryDLP.sanitize_obj(r.detail_json),
            "createdAt": r.created_at.isoformat(),
        }
        for r in rows
    ]
    return {
        "storyKey": story_key.upper() if story_key else None,
        "totalLogs": len(logs),
        "logs": logs,
    }
