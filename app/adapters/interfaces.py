"""Protocol interfaces for LLM, Jira, Git (strictly without merge), and Discord adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class LLMResponse:
    """Standardized response from an LLM provider with token and USD cost accounting."""

    provider_name: str
    content: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: Decimal
    used_fallback: bool = False


@runtime_checkable
class LLMProviderProtocol(Protocol):
    """Interface for Primary and Fallback LLM providers."""

    provider_name: str

    def generate(self, prompt: str, context: dict[str, Any] | None = None) -> LLMResponse: ...


@dataclass(slots=True)
class JiraIssue:
    """Representation of a Jira Story ticket."""

    key: str
    summary: str
    description: str
    acceptance_criteria: list[str] = field(default_factory=list)
    his_module: str = "Registration"
    status: str = "TO DO"


@runtime_checkable
class JiraAdapterProtocol(Protocol):
    """Interface for Jira issue retrieval and status updates."""

    def get_issue(self, story_key: str) -> JiraIssue: ...

    def update_issue_status(self, story_key: str, new_status: str) -> None: ...


@dataclass(slots=True)
class PullRequestInfo:
    """Representation of an opened or updated Pull Request."""

    pr_id: str
    head_branch: str
    base_branch: str
    title: str
    body: str
    head_commit_sha: str
    status: str = "OPEN"


@runtime_checkable
class GitAdapterProtocol(Protocol):
    """Interface for bot Git operations. Explicitly omits any merge() method by design."""

    def ensure_bot_branch(self, branch: str, from_ref: str = "develop") -> str: ...

    def commit_files(self, branch: str, files: dict[str, str], commit_message: str) -> str: ...

    def push_branch(self, branch: str) -> str: ...

    def create_or_update_pull_request(
        self,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PullRequestInfo: ...


@dataclass(slots=True)
class DiscordNotification:
    """Recorded Discord channel message with role/user mentions."""

    channel: str
    content: str
    mentions: list[str]
    story_key: str | None = None
    step: int | None = None


@runtime_checkable
class DiscordAdapterProtocol(Protocol):
    """Interface for sending channel notifications and @mentions."""

    def send_channel_message(
        self,
        channel: str,
        content: str,
        mentions: list[str] | None = None,
        story_key: str | None = None,
        step: int | None = None,
    ) -> DiscordNotification: ...
