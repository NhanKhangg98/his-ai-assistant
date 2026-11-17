"""Durable Asyncio Worker with Lease Expiration, Crash Recovery, and 7-Step Transitions."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.adapters.discord_adapter import MockDiscordAdapter, get_discord_adapter
from app.adapters.git_adapter import LocalRepoGitAdapter, ProtectedBranchError, get_git_adapter
from app.adapters.jira_adapter import MockJiraAdapter, get_jira_adapter
from app.adapters.llm_router import LLMRouter, QuotaExceededError
from app.config import get_settings
from app.db.models import BackgroundTask, WorkflowStory, utc_now
from app.db.session import session_scope
from app.security.dlp import AllBoundaryDLP
from app.workflow.agents import WorkflowAgents
from app.workflow.state_machine import AuditLogger

logger = logging.getLogger(__name__)

ACTION_TO_ARTIFACT_KEY: dict[str, str] = {
    "analyze": "step_1_ba_analysis",
    "design-test": "step_2_qc_test_cases",
    "plan": "step_3_dev_plan",
    "implement": "step_4_dev_implementation",
    "summarize-pr": "step_5_pr_summary",
    "test": "step_6_qc_test_checklist",
    "bug": "step_6_bug_report",
    "uat": "step_7_ba_uat",
}


class DurableAsyncioWorker:
    """Polls PostgreSQL/SQLite background_tasks with lease locking and automatic crash recovery."""

    def __init__(
        self,
        database_url: str | None = None,
        discord_adapter: MockDiscordAdapter | None = None,
        jira_adapter: MockJiraAdapter | None = None,
        git_adapter: LocalRepoGitAdapter | None = None,
    ) -> None:
        self.settings = get_settings()
        self.database_url = database_url or self.settings.database_url
        self.discord_adapter = discord_adapter or get_discord_adapter()
        self.jira_adapter = jira_adapter or get_jira_adapter()
        self.git_adapter = git_adapter or get_git_adapter()
        self._stop_event: asyncio.Event | None = None
        self._bg_task: asyncio.Task[None] | None = None

    def recover_interrupted_tasks(self, force_reclaim_running: bool = True) -> int:
        """Reclaim expired or orphaned tasks left in RUNNING state back to PENDING (or FAILED)."""
        now = utc_now()
        reclaimed = 0
        with session_scope(self.database_url) as session:
            # 1. Dead-letter poison tasks (attempts >= max_attempts) left in RUNNING
            poison_stmt = select(BackgroundTask).where(
                BackgroundTask.status == "RUNNING",
                BackgroundTask.attempts >= BackgroundTask.max_attempts,
            )
            poison_tasks = session.scalars(poison_stmt).all()
            for pt in poison_tasks:
                pt.status = "FAILED"
                pt.error_message = "Task exceeded max retry attempts (dead lettered on recovery)."
                pt.lease_expires_at = None
                pt.updated_at = now

            # 2. Reclaimable tasks under max_attempts
            stmt = select(BackgroundTask).where(
                BackgroundTask.status == "RUNNING",
                BackgroundTask.attempts < BackgroundTask.max_attempts,
            )
            if not force_reclaim_running:
                stmt = stmt.where(
                    or_(
                        BackgroundTask.lease_expires_at.is_(None),
                        BackgroundTask.lease_expires_at <= now,
                    )
                )
            tasks = session.scalars(stmt).all()
            for t in tasks:
                t.status = "PENDING"
                t.lease_expires_at = None
                t.updated_at = now
                reclaimed += 1
        return reclaimed

    def process_next_task(self, llm_router_override: LLMRouter | None = None) -> str | None:
        """Claim and execute a single eligible task using a two-phase durable transaction."""
        now = utc_now()
        lease_until = now + timedelta(seconds=self.settings.worker_lease_seconds)
        claimed_task_id: str | None = None

        # Phase 1: Short atomic transaction to claim task and increment attempts
        with session_scope(self.database_url) as session:
            stmt = (
                select(BackgroundTask)
                .where(
                    BackgroundTask.attempts < BackgroundTask.max_attempts,
                    or_(
                        BackgroundTask.status == "PENDING",
                        (
                            (BackgroundTask.status == "RUNNING")
                            & (
                                BackgroundTask.lease_expires_at.is_(None)
                                | (BackgroundTask.lease_expires_at <= now)
                            )
                        ),
                    ),
                )
                .order_by(BackgroundTask.created_at.asc())
                .limit(1)
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)

            task = session.scalar(stmt)
            if task is None:
                return None

            task.status = "RUNNING"
            task.attempts += 1
            task.lease_expires_at = lease_until
            task.updated_at = now
            claimed_task_id = task.id

        if claimed_task_id is None:
            return None

        # Phase 2: Execute claimed task in its own transaction
        with session_scope(self.database_url) as session:
            task = session.scalar(
                select(BackgroundTask).where(BackgroundTask.id == claimed_task_id)
            )
            if task is not None:
                self._execute_claimed_task(session, task, llm_router_override=llm_router_override)

        return claimed_task_id

    def drain_pending_tasks(
        self, max_tasks: int = 50, llm_router_override: LLMRouter | None = None
    ) -> list[str]:
        """Process all currently pending or expired-lease tasks synchronously (useful for tests)."""
        processed_ids: list[str] = []
        for _ in range(max_tasks):
            tid = self.process_next_task(llm_router_override=llm_router_override)
            if tid is None:
                break
            processed_ids.append(tid)
        return processed_ids

    def _execute_claimed_task(
        self,
        session: Session,
        task: BackgroundTask,
        llm_router_override: LLMRouter | None = None,
    ) -> None:
        story = session.scalar(
            select(WorkflowStory).where(WorkflowStory.story_key == task.story_key)
        )
        if story is None:
            task.status = "FAILED"
            task.error_message = f"Story {task.story_key} not found."
            return

        payload: dict[str, Any] = dict(task.payload_json or {})
        user_role = str(payload.get("user_role", "BA"))
        extra_arg = str(payload.get("extra_arg", ""))
        prev_step_status = str(payload.get("previous_step_status", "WAITING_AGENT"))
        next_role_mention = str(payload.get("next_role_mention", user_role))
        from_step = story.current_step

        agents = WorkflowAgents(
            session=session,
            llm_router=llm_router_override or LLMRouter(session),
            jira_adapter=self.jira_adapter,
            git_adapter=self.git_adapter,
        )

        try:
            artifact_result = agents.execute_agent_action(
                action=task.action_name,
                story_key=task.story_key,
                user_id=task.user_id,
                user_role=user_role,
                extra_arg=extra_arg,
                existing_artifacts=story.artifacts_json,
            )
            clean_result = AllBoundaryDLP.sanitize_obj(artifact_result)

            updated_artifacts = dict(story.artifacts_json or {})
            art_key = ACTION_TO_ARTIFACT_KEY.get(task.action_name, f"step_{task.target_step}")
            updated_artifacts[art_key] = clean_result
            story.artifacts_json = updated_artifacts

            # Apply post-agent state machine transitions
            if task.action_name == "implement":
                # Step 4 has no manual approval gate -> auto-transitions to Step 5 (WAITING_AGENT)
                story.git_branch = clean_result.get("branch", f"bot/{story.story_key}")
                story.pr_id = clean_result.get("prId", f"PR-{story.story_key}")
                story.pr_status = clean_result.get("prStatus", "OPEN")
                story.current_step = 5
                story.step_status = "WAITING_AGENT"
                next_role_mention = "Dev"
            elif task.action_name == "bug":
                # Step 6 QC bug report -> loops back to Step 3 (WAITING_AGENT)
                story.loop_count += 1
                story.current_step = 3
                story.step_status = "WAITING_AGENT"
                next_role_mention = "Dev"
            else:
                story.step_status = "WAITING_APPROVAL"

            story.updated_at = utc_now()
            task.status = "COMPLETED"
            task.result_json = clean_result
            task.lease_expires_at = None
            task.updated_at = utc_now()

            AuditLogger.record(
                session,
                story_key=story.story_key,
                message_id=task.message_id,
                user_id=task.user_id,
                user_role=user_role,
                action=task.action_name,
                from_step=from_step,
                to_step=story.current_step,
                outcome_status="STEP_EXECUTED",
                sanitized_command=f"/{user_role.lower()} {task.action_name} {story.story_key}",
                detail={"taskId": task.id, "stepStatus": story.step_status},
            )

            self.discord_adapter.send_channel_message(
                channel=task.channel,
                content=(
                    f"[Task {task.id} COMPLETED] Story {story.story_key} action "
                    f"'{task.action_name}' finished. Current step: {story.current_step} "
                    f"({story.step_status}). Vui lòng kiểm tra và duyệt!"
                ),
                mentions=[f"@{next_role_mention}", f"@{task.user_id}"],
                story_key=story.story_key,
                step=story.current_step,
            )

        except QuotaExceededError as q_err:
            clean_err = AllBoundaryDLP.sanitize_text(str(q_err))
            story.step_status = (
                "WAITING_APPROVAL" if task.action_name == "bug" else prev_step_status
            )
            story.updated_at = utc_now()
            task.status = "FAILED"
            task.error_message = clean_err
            task.lease_expires_at = None
            task.updated_at = utc_now()

            AuditLogger.record(
                session,
                story_key=story.story_key,
                message_id=task.message_id,
                user_id=task.user_id,
                user_role=user_role,
                action=task.action_name,
                from_step=from_step,
                to_step=story.current_step,
                outcome_status="QUOTA_BLOCKED",
                sanitized_command=f"/{user_role.lower()} {task.action_name} {story.story_key}",
                detail={"error": clean_err, "taskId": task.id},
            )
            self.discord_adapter.send_channel_message(
                channel=task.channel,
                content=f"Cảnh báo vượt hạn mức LLM tháng: {clean_err}",
                mentions=[f"@{task.user_id}", f"@{user_role}"],
                story_key=story.story_key,
                step=story.current_step,
            )

        except ProtectedBranchError as g_err:
            clean_err = AllBoundaryDLP.sanitize_text(str(g_err))
            story.step_status = prev_step_status
            story.updated_at = utc_now()
            task.status = "FAILED"
            task.error_message = clean_err
            task.lease_expires_at = None
            task.updated_at = utc_now()

            AuditLogger.record(
                session,
                story_key=story.story_key,
                message_id=task.message_id,
                user_id=task.user_id,
                user_role=user_role,
                action=task.action_name,
                from_step=from_step,
                to_step=story.current_step,
                outcome_status="FAILED",
                sanitized_command=f"/{user_role.lower()} {task.action_name} {story.story_key}",
                detail={"error": clean_err, "taskId": task.id},
            )

        except Exception as exc:
            clean_err = AllBoundaryDLP.sanitize_text(str(exc))
            logger.error("Worker task %s failed: %s", task.id, clean_err)
            if task.attempts >= task.max_attempts:
                task.status = "FAILED"
                story.step_status = (
                    "WAITING_APPROVAL" if task.action_name == "bug" else prev_step_status
                )
            else:
                task.status = "PENDING"
            task.error_message = clean_err
            task.lease_expires_at = None
            task.updated_at = utc_now()
            story.updated_at = utc_now()

    async def run_forever(self) -> None:
        """Background loop polling for pending tasks off-thread until stop() is called."""
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        self.recover_interrupted_tasks(force_reclaim_running=False)
        while not self._stop_event.is_set():
            try:
                processed = await asyncio.to_thread(self.process_next_task)
                if processed is not None:
                    await asyncio.sleep(0)
                    continue
            except Exception as exc:
                logger.error("Worker poll loop error: %s", AllBoundaryDLP.sanitize_text(str(exc)))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.settings.worker_poll_interval_seconds,
                )
            except TimeoutError:
                pass

    def start(self) -> None:
        """Start the background asyncio loop."""
        if self._stop_event is not None:
            self._stop_event.clear()
        if self._bg_task is None or self._bg_task.done():
            self._bg_task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        """Signal the worker loop to stop and await completion."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._bg_task is not None:
            await self._bg_task
            self._bg_task = None
