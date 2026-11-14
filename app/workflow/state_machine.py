"""7-Step Workflow State Machine, Role Gate, In-Flight Lock, and Append-Only Audit Logger."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.discord_adapter import MockDiscordAdapter, get_discord_adapter
from app.adapters.jira_adapter import MockJiraAdapter, get_jira_adapter
from app.db.models import (
    BackgroundTask,
    ProcessedMessage,
    User,
    WorkflowAuditLog,
    WorkflowStory,
    utc_now,
)
from app.security.dlp import AllBoundaryDLP
from app.workflow.command_parser import CommandParseError, ParsedCommand, parse_chat_command


class RoleForbiddenError(PermissionError):
    """HTTP 403 error raised when a user invokes a command or approval gate for another role."""

    def __init__(
        self, message: str, story_key: str, current_step: int, error_code: str = "ROLE_FORBIDDEN"
    ) -> None:
        super().__init__(message)
        self.detail = {
            "errorCode": error_code,
            "message": message,
            "storyKey": story_key,
            "currentStep": current_step,
        }


class StepConflictError(RuntimeError):
    """HTTP 409 error raised on invalid step jump, unready approval, or in-flight agent lock."""

    def __init__(
        self,
        message: str,
        story_key: str,
        current_step: int,
        step_status: str,
        error_code: str = "INVALID_STEP_TRANSITION",
    ) -> None:
        super().__init__(message)
        self.detail = {
            "errorCode": error_code,
            "message": message,
            "storyKey": story_key,
            "currentStep": current_step,
            "stepStatus": step_status,
        }


class IdempotencyPayloadMismatchError(ValueError):
    """HTTP 422 error raised when a duplicate messageId is sent with a mutated payload."""

    def __init__(self, message_id: str) -> None:
        super().__init__(
            f"Idempotency key '{message_id}' was already used with a different payload."
        )
        self.detail = {
            "errorCode": "IDEMPOTENCY_PAYLOAD_MISMATCH",
            "message": (
                f"messageId '{message_id}' was already processed with a different request payload."
            ),
            "messageId": message_id,
        }


def compute_request_payload_sha256(user_id: str, channel: str, text: str) -> str:
    """Compute canonical SHA-256 fingerprint of a chat command payload for idempotency."""
    canonical = json.dumps(
        {"userId": user_id.strip(), "channel": channel.strip(), "text": text.strip()},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLogger:
    """Append-only audit logger sanitizing all command strings and detail payloads via DLP."""

    @staticmethod
    def record(
        session: Session,
        *,
        story_key: str | None,
        message_id: str,
        user_id: str,
        user_role: str,
        action: str,
        from_step: int | None,
        to_step: int | None,
        outcome_status: str,
        sanitized_command: str,
        detail: dict[str, Any] | None = None,
    ) -> WorkflowAuditLog:
        log_entry = WorkflowAuditLog(
            story_key=story_key,
            message_id=message_id,
            user_id=user_id,
            user_role=user_role,
            action=action,
            from_step=from_step,
            to_step=to_step,
            outcome_status=outcome_status,
            sanitized_command=AllBoundaryDLP.sanitize_text(sanitized_command),
            detail_json=AllBoundaryDLP.sanitize_obj(detail or {}),
        )
        session.add(log_entry)
        session.flush()
        return log_entry


class WorkflowStateMachine:
    """Enforces the 7-step BA/QC/Dev workflow, Role Gates, In-Flight locks, and Idempotency."""

    def __init__(
        self,
        session: Session,
        jira_adapter: MockJiraAdapter | None = None,
        discord_adapter: MockDiscordAdapter | None = None,
    ) -> None:
        self.session = session
        self.jira_adapter = jira_adapter or get_jira_adapter()
        self.discord_adapter = discord_adapter or get_discord_adapter()

    def _get_or_create_story_locked(self, story_key: str) -> WorkflowStory:
        stmt = select(WorkflowStory).where(WorkflowStory.story_key == story_key)
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        story = self.session.scalar(stmt)
        if story is None:
            issue = self.jira_adapter.get_issue(story_key)
            story = WorkflowStory(
                story_key=story_key,
                title=issue.summary,
                current_step=1,
                step_status="WAITING_AGENT",
                loop_count=0,
                artifacts_json={},
            )
            self.session.add(story)
            self.session.flush()
        return story

    def _resolve_user(self, user_id: str) -> User:
        user = self.session.scalar(select(User).where(User.user_id == user_id))
        if user is None or not user.is_active:
            raise RoleForbiddenError(
                message=f"Unknown or inactive user '{user_id}'.",
                story_key="UNKNOWN",
                current_step=1,
                error_code="USER_NOT_FOUND_OR_FORBIDDEN",
            )
        return user

    def handle_chat_command(
        self,
        message_id: str,
        user_id: str,
        channel: str,
        text: str,
    ) -> tuple[int, dict[str, Any]]:
        """Process a chat command idempotently, returning (http_status_code, response_dict)."""
        raw_stripped = text.strip()
        # Hash canonical raw payload so mutated PII/secrets with same messageId trigger 422
        payload_hash = compute_request_payload_sha256(user_id, channel, raw_stripped)
        clean_text = AllBoundaryDLP.sanitize_text(raw_stripped)

        # 1. Check Stripe-style idempotency table (processed_messages)
        existing_msg = self.session.scalar(
            select(ProcessedMessage).where(ProcessedMessage.message_id == message_id)
        )
        if existing_msg is not None:
            if existing_msg.payload_sha256 != payload_hash:
                raise IdempotencyPayloadMismatchError(message_id)
            replay_payload = dict(existing_msg.response_json)
            replay_payload["idempotentReplay"] = True
            return existing_msg.status_code, replay_payload

        user = self._resolve_user(user_id)
        parsed: ParsedCommand = parse_chat_command(raw_stripped)
        clean_text = parsed.sanitized_text
        story = self._get_or_create_story_locked(parsed.story_key)

        # 2. Enforce RBAC Role Gate (403 Forbidden)
        if user.role != parsed.spec.required_role:
            err = RoleForbiddenError(
                message=(
                    f"User '{user.user_id}' with role '{user.role}' is not permitted to execute "
                    f"'{parsed.prefix} {parsed.action}' at Step {parsed.spec.target_step}. "
                    f"Required role: '{parsed.spec.required_role}'."
                ),
                story_key=story.story_key,
                current_step=story.current_step,
            )
            AuditLogger.record(
                self.session,
                story_key=story.story_key,
                message_id=message_id,
                user_id=user.user_id,
                user_role=user.role,
                action=f"{parsed.prefix} {parsed.action}",
                from_step=story.current_step,
                to_step=story.current_step,
                outcome_status="REJECTED_ROLE",
                sanitized_command=clean_text,
                detail=err.detail,
            )
            self._save_processed_message(
                message_id=message_id,
                user_id=user.user_id,
                channel=channel,
                sanitized_text=clean_text,
                payload_sha256=payload_hash,
                task_id=None,
                status_code=403,
                response_json={"detail": err.detail},
            )
            self.session.commit()
            raise err

        # 3. Enforce In-Flight Agent Lock (409 Conflict if an agent task is currently running)
        if story.step_status == "RUNNING_AGENT":
            err_in_flight = StepConflictError(
                message=(
                    f"Story '{story.story_key}' currently has an in-flight background agent task "
                    f"at Step {story.current_step} (step_status='RUNNING_AGENT')."
                ),
                story_key=story.story_key,
                current_step=story.current_step,
                step_status=story.step_status,
                error_code="AGENT_TASK_IN_PROGRESS",
            )
            AuditLogger.record(
                self.session,
                story_key=story.story_key,
                message_id=message_id,
                user_id=user.user_id,
                user_role=user.role,
                action=f"{parsed.prefix} {parsed.action}",
                from_step=story.current_step,
                to_step=story.current_step,
                outcome_status="REJECTED_STEP",
                sanitized_command=clean_text,
                detail=err_in_flight.detail,
            )
            self._save_processed_message(
                message_id=message_id,
                user_id=user.user_id,
                channel=channel,
                sanitized_text=clean_text,
                payload_sha256=payload_hash,
                task_id=None,
                status_code=409,
                response_json={"detail": err_in_flight.detail},
            )
            self.session.commit()
            raise err_in_flight

        # 4. Enforce 7-Step Order & Step Status Gate (409 on step jumping or unready approval)
        if (
            story.current_step != parsed.spec.target_step
            or story.step_status not in parsed.spec.allowed_step_statuses
        ):
            err_step = StepConflictError(
                message=(
                    f"Cannot execute Step {parsed.spec.target_step} action '{parsed.action}' "
                    f"while story '{story.story_key}' is at Step {story.current_step} "
                    f"(status='{story.step_status}')."
                ),
                story_key=story.story_key,
                current_step=story.current_step,
                step_status=story.step_status,
                error_code="INVALID_STEP_TRANSITION",
            )
            AuditLogger.record(
                self.session,
                story_key=story.story_key,
                message_id=message_id,
                user_id=user.user_id,
                user_role=user.role,
                action=f"{parsed.prefix} {parsed.action}",
                from_step=story.current_step,
                to_step=story.current_step,
                outcome_status="REJECTED_STEP",
                sanitized_command=clean_text,
                detail=err_step.detail,
            )
            self._save_processed_message(
                message_id=message_id,
                user_id=user.user_id,
                channel=channel,
                sanitized_text=clean_text,
                payload_sha256=payload_hash,
                task_id=None,
                status_code=409,
                response_json={"detail": err_step.detail},
            )
            self.session.commit()
            raise err_step

        # 5A. Handle Synchronous Approval / Transition Commands (200 OK)
        if not parsed.spec.is_async_agent:
            prev_step = story.current_step
            next_step = parsed.spec.next_step_on_sync or prev_step
            next_status = parsed.spec.next_status_on_sync or "WAITING_AGENT"

            story.current_step = next_step
            story.step_status = next_status
            story.updated_at = utc_now()

            if parsed.action == "confirm-merge":
                story.pr_status = "MERGED"
            elif parsed.action == "close":
                self.jira_adapter.update_issue_status(story.story_key, "DONE")

            outcome_label = "REJECTED_PLAN" if parsed.action == "reject" else "APPROVED"
            response_payload = {
                "messageId": message_id,
                "taskId": None,
                "storyKey": story.story_key,
                "previousStep": prev_step,
                "currentStep": story.current_step,
                "stepStatus": story.step_status,
                "status": "DONE" if next_status == "DONE" else outcome_label,
                "idempotentReplay": False,
                "nextExpectedRole": parsed.spec.next_role_mention,
                "message": (
                    f"Step {prev_step} action '{parsed.action}' completed by {user.role}. "
                    f"Story {story.story_key} is now at Step {story.current_step} "
                    f"({story.step_status})."
                ),
            }

            AuditLogger.record(
                self.session,
                story_key=story.story_key,
                message_id=message_id,
                user_id=user.user_id,
                user_role=user.role,
                action=f"{parsed.prefix} {parsed.action}",
                from_step=prev_step,
                to_step=story.current_step,
                outcome_status=outcome_label,
                sanitized_command=clean_text,
                detail={"reason": parsed.extra_arg} if parsed.extra_arg else {},
            )
            self._save_processed_message(
                message_id=message_id,
                user_id=user.user_id,
                channel=channel,
                sanitized_text=clean_text,
                payload_sha256=payload_hash,
                task_id=None,
                status_code=200,
                response_json=response_payload,
            )
            self.session.commit()

            self.discord_adapter.send_channel_message(
                channel=channel,
                content=response_payload["message"],
                mentions=[f"@{parsed.spec.next_role_mention}"],
                story_key=story.story_key,
                step=story.current_step,
            )
            return 200, response_payload

        # 5B. Handle Asynchronous Agent Commands (202 Accepted + Durable Task Queue)
        previous_step_status = story.step_status
        story.step_status = "RUNNING_AGENT"
        story.updated_at = utc_now()

        bg_task = BackgroundTask(
            message_id=message_id,
            story_key=story.story_key,
            user_id=user.user_id,
            channel=channel,
            target_step=parsed.spec.target_step,
            action_name=parsed.action,
            status="PENDING",
            attempts=0,
            max_attempts=3,
            payload_json={
                "extra_arg": parsed.extra_arg,
                "user_role": user.role,
                "previous_step_status": previous_step_status,
                "next_role_mention": parsed.spec.next_role_mention,
            },
        )
        self.session.add(bg_task)
        self.session.flush()

        async_response = {
            "messageId": message_id,
            "taskId": bg_task.id,
            "storyKey": story.story_key,
            "step": story.current_step,
            "stepStatus": story.step_status,
            "action": parsed.action,
            "status": "PENDING",
            "idempotentReplay": False,
            "message": "Task accepted and queued for durable asyncio worker execution.",
        }

        AuditLogger.record(
            self.session,
            story_key=story.story_key,
            message_id=message_id,
            user_id=user.user_id,
            user_role=user.role,
            action=f"{parsed.prefix} {parsed.action}",
            from_step=story.current_step,
            to_step=story.current_step,
            outcome_status="ACCEPTED_ASYNC",
            sanitized_command=clean_text,
            detail={"taskId": bg_task.id},
        )
        self._save_processed_message(
            message_id=message_id,
            user_id=user.user_id,
            channel=channel,
            sanitized_text=clean_text,
            payload_sha256=payload_hash,
            task_id=bg_task.id,
            status_code=202,
            response_json=async_response,
        )
        self.session.commit()
        return 202, async_response

    def _save_processed_message(
        self,
        *,
        message_id: str,
        user_id: str,
        channel: str,
        sanitized_text: str,
        payload_sha256: str,
        task_id: str | None,
        status_code: int,
        response_json: dict[str, Any],
    ) -> None:
        pm = ProcessedMessage(
            message_id=message_id,
            user_id=user_id,
            channel=channel,
            sanitized_text=sanitized_text,
            payload_sha256=payload_sha256,
            task_id=task_id,
            status_code=status_code,
            response_json=AllBoundaryDLP.sanitize_obj(response_json),
        )
        self.session.add(pm)


__all__ = [
    "AuditLogger",
    "CommandParseError",
    "IdempotencyPayloadMismatchError",
    "RoleForbiddenError",
    "StepConflictError",
    "WorkflowStateMachine",
]
