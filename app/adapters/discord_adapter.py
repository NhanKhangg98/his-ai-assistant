"""Mock Discord Adapter recording channel notifications and @role/@user tags with DLP protection."""

from __future__ import annotations

from app.adapters.interfaces import DiscordAdapterProtocol, DiscordNotification
from app.security.dlp import AllBoundaryDLP


class MockDiscordAdapter(DiscordAdapterProtocol):
    """In-memory Discord adapter that sanitizes and stores outbound channel messages."""

    def __init__(self) -> None:
        self.sent_messages: list[DiscordNotification] = []

    def send_channel_message(
        self,
        channel: str,
        content: str,
        mentions: list[str] | None = None,
        story_key: str | None = None,
        step: int | None = None,
    ) -> DiscordNotification:
        clean_mentions = [AllBoundaryDLP.sanitize_text(m) for m in (mentions or [])]
        mention_prefix = " ".join(
            m if m.startswith("@") else f"@{m}" for m in clean_mentions
        )
        full_text = f"{mention_prefix} {content}".strip() if mention_prefix else content
        sanitized_content = AllBoundaryDLP.sanitize_text(full_text)

        notification = DiscordNotification(
            channel=channel,
            content=sanitized_content,
            mentions=clean_mentions,
            story_key=story_key,
            step=step,
        )
        self.sent_messages.append(notification)
        return notification

    def clear(self) -> None:
        self.sent_messages.clear()


_GLOBAL_DISCORD_ADAPTER = MockDiscordAdapter()


def get_discord_adapter() -> MockDiscordAdapter:
    """Return singleton MockDiscordAdapter instance."""
    return _GLOBAL_DISCORD_ADAPTER
