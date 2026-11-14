"""Chat Command Parser for the 7-Step BA/QC/Dev Workflow Assistant."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.security.dlp import AllBoundaryDLP

COMMAND_PREFIX_TO_ROLE: dict[str, str] = {
    "/ba": "BA",
    "/qc": "QC",
    "/dev": "Dev",
}


@dataclass(slots=True)
class CommandSpec:
    """Specification of a valid workflow command's step, role, execution mode, and states."""

    prefix: str
    action: str
    required_role: str
    target_step: int
    is_async_agent: bool
    allowed_step_statuses: tuple[str, ...]
    next_step_on_sync: int | None = None
    next_status_on_sync: str | None = None
    next_role_mention: str = "BA"


COMMAND_REGISTRY: dict[tuple[str, str], CommandSpec] = {
    ("/ba", "analyze"): CommandSpec(
        prefix="/ba",
        action="analyze",
        required_role="BA",
        target_step=1,
        is_async_agent=True,
        allowed_step_statuses=("WAITING_AGENT", "WAITING_APPROVAL"),
        next_role_mention="BA",
    ),
    ("/ba", "approve"): CommandSpec(
        prefix="/ba",
        action="approve",
        required_role="BA",
        target_step=1,
        is_async_agent=False,
        allowed_step_statuses=("WAITING_APPROVAL",),
        next_step_on_sync=2,
        next_status_on_sync="WAITING_AGENT",
        next_role_mention="QC",
    ),
    ("/qc", "design-test"): CommandSpec(
        prefix="/qc",
        action="design-test",
        required_role="QC",
        target_step=2,
        is_async_agent=True,
        allowed_step_statuses=("WAITING_AGENT", "WAITING_APPROVAL"),
        next_role_mention="QC",
    ),
    ("/qc", "approve"): CommandSpec(
        prefix="/qc",
        action="approve",
        required_role="QC",
        target_step=2,
        is_async_agent=False,
        allowed_step_statuses=("WAITING_APPROVAL",),
        next_step_on_sync=3,
        next_status_on_sync="WAITING_AGENT",
        next_role_mention="Dev",
    ),
    ("/dev", "plan"): CommandSpec(
        prefix="/dev",
        action="plan",
        required_role="Dev",
        target_step=3,
        is_async_agent=True,
        allowed_step_statuses=("WAITING_AGENT", "WAITING_APPROVAL"),
        next_role_mention="Dev",
    ),
    ("/dev", "approve"): CommandSpec(
        prefix="/dev",
        action="approve",
        required_role="Dev",
        target_step=3,
        is_async_agent=False,
        allowed_step_statuses=("WAITING_APPROVAL",),
        next_step_on_sync=4,
        next_status_on_sync="WAITING_AGENT",
        next_role_mention="Dev",
    ),
    ("/dev", "reject"): CommandSpec(
        prefix="/dev",
        action="reject",
        required_role="Dev",
        target_step=3,
        is_async_agent=False,
        allowed_step_statuses=("WAITING_APPROVAL",),
        next_step_on_sync=3,
        next_status_on_sync="WAITING_AGENT",
        next_role_mention="Dev",
    ),
    ("/dev", "implement"): CommandSpec(
        prefix="/dev",
        action="implement",
        required_role="Dev",
        target_step=4,
        is_async_agent=True,
        allowed_step_statuses=("WAITING_AGENT",),
        next_role_mention="Dev",
    ),
    ("/dev", "summarize-pr"): CommandSpec(
        prefix="/dev",
        action="summarize-pr",
        required_role="Dev",
        target_step=5,
        is_async_agent=True,
        allowed_step_statuses=("WAITING_AGENT", "WAITING_APPROVAL"),
        next_role_mention="Dev",
    ),
    ("/dev", "confirm-merge"): CommandSpec(
        prefix="/dev",
        action="confirm-merge",
        required_role="Dev",
        target_step=5,
        is_async_agent=False,
        allowed_step_statuses=("WAITING_APPROVAL",),
        next_step_on_sync=6,
        next_status_on_sync="WAITING_AGENT",
        next_role_mention="QC",
    ),
    ("/qc", "test"): CommandSpec(
        prefix="/qc",
        action="test",
        required_role="QC",
        target_step=6,
        is_async_agent=True,
        allowed_step_statuses=("WAITING_AGENT", "WAITING_APPROVAL"),
        next_role_mention="QC",
    ),
    ("/qc", "pass"): CommandSpec(
        prefix="/qc",
        action="pass",
        required_role="QC",
        target_step=6,
        is_async_agent=False,
        allowed_step_statuses=("WAITING_APPROVAL",),
        next_step_on_sync=7,
        next_status_on_sync="WAITING_AGENT",
        next_role_mention="BA",
    ),
    ("/qc", "bug"): CommandSpec(
        prefix="/qc",
        action="bug",
        required_role="QC",
        target_step=6,
        is_async_agent=True,
        allowed_step_statuses=("WAITING_APPROVAL",),
        next_role_mention="Dev",
    ),
    ("/ba", "uat"): CommandSpec(
        prefix="/ba",
        action="uat",
        required_role="BA",
        target_step=7,
        is_async_agent=True,
        allowed_step_statuses=("WAITING_AGENT", "WAITING_APPROVAL"),
        next_role_mention="BA",
    ),
    ("/ba", "close"): CommandSpec(
        prefix="/ba",
        action="close",
        required_role="BA",
        target_step=7,
        is_async_agent=False,
        allowed_step_statuses=("WAITING_APPROVAL",),
        next_step_on_sync=7,
        next_status_on_sync="DONE",
        next_role_mention="BA",
    ),
}

STORY_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[A-Z0-9_]+$")


class CommandParseError(ValueError):
    """Raised when a chat command does not match the 7-step command grammar."""


@dataclass(slots=True)
class ParsedCommand:
    """Parsed and DLP-sanitized chat command."""

    prefix: str
    action: str
    story_key: str
    extra_arg: str
    sanitized_text: str
    spec: CommandSpec


def parse_chat_command(raw_text: str) -> ParsedCommand:
    """Parse `/ba|/qc|/dev <action> <STORY_KEY> [extra]` and sanitize payload with DLP."""
    raw_stripped = raw_text.strip()
    if not raw_stripped:
        raise CommandParseError("Command text cannot be empty.")

    # Support pipe separator for `/qc bug HIS-101 | <error_log>`
    main_part = raw_stripped
    pipe_arg = ""
    if "|" in raw_stripped:
        main_part, pipe_arg = [p.strip() for p in raw_stripped.split("|", 1)]

    tokens = main_part.split(maxsplit=3)
    if len(tokens) < 3:
        raise CommandParseError(
            "Invalid command syntax. Expected: '/<ba|qc|dev> <action> <STORY_KEY> [details]'."
        )

    prefix = tokens[0].lower()
    action = tokens[1].lower()
    story_key = tokens[2].upper()
    inline_extra = tokens[3].strip() if len(tokens) > 3 else ""
    raw_extra = f"{inline_extra} {pipe_arg}".strip()
    extra_arg = AllBoundaryDLP.sanitize_text(raw_extra) if raw_extra else ""

    if not STORY_KEY_RE.match(story_key):
        safe_key = AllBoundaryDLP.sanitize_text(story_key)
        raise CommandParseError(f"Invalid Jira story key format: {safe_key!r}")

    spec = COMMAND_REGISTRY.get((prefix, action))
    if spec is None:
        raise CommandParseError(f"Unknown workflow command '{prefix} {action}'.")

    if action == "bug" and not extra_arg:
        extra_arg = "Error reported by QC during regression testing."

    sanitized = f"{prefix} {action} {story_key}" + (f" {extra_arg}" if extra_arg else "")

    return ParsedCommand(
        prefix=prefix,
        action=action,
        story_key=story_key,
        extra_arg=extra_arg,
        sanitized_text=sanitized,
        spec=spec,
    )
