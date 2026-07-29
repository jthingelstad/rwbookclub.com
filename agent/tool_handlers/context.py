"""Trusted, typed request context passed to every model-tool capability handler."""

from __future__ import annotations

from dataclasses import dataclass

from agent import access


@dataclass(frozen=True)
class RequestContext:
    actor: access.Actor
    channel_id: str | int | None
    speaker: str | None
    speaker_user_id: str | int | None
    source_message_id: str | int | None

    @classmethod
    def from_runtime(cls, runtime: dict, *, actor: access.Actor) -> "RequestContext":
        return cls(
            actor=actor,
            channel_id=runtime.get("channel_id"),
            speaker=runtime.get("speaker"),
            speaker_user_id=runtime.get("speaker_user_id"),
            source_message_id=runtime.get("source_message_id"),
        )

    @property
    def member_slug(self) -> str | None:
        return self.actor.member_slug

    @property
    def surface(self) -> str:
        channel = str(self.channel_id or "")
        return (
            "mailing_list"
            if channel.startswith("email:list:")
            else "email"
            if channel.startswith("email:")
            else "discord"
        )

    @property
    def is_email(self) -> bool:
        return str(self.channel_id or "").startswith("email:")

    @property
    def identity_is_email(self) -> bool:
        """Whether the trusted speaker identity came from inbound email."""
        return str(self.speaker_user_id or "").startswith("email:")

    @property
    def is_shared_output(self) -> bool:
        return access.is_shared_conversation(str(self.channel_id or ""))

    @property
    def output_visibility(self) -> str:
        return "shared_club" if self.is_shared_output else "private_member"

    @property
    def private_context_output_policy(self) -> str:
        if not self.is_shared_output:
            return "private_reply_may_reference"
        return (
            "silent_private_calibration_only: do not name, quote, paraphrase, attribute, or imply "
            "which member supplied this signal; express reception uncertainty as a club-level "
            "tradeoff"
        )
