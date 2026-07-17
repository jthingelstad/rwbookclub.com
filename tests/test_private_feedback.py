"""Private book feedback stays useful to Oliver without becoming public review copy."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent import commands, db, identities
from agent.tools import dispatch
from corpus.paths import DATA_DIR

USER_ID = 314159
NOTE = "I DNF'd it because the argument felt repetitive; avoid similar picks for me."


def _reviews() -> list[tuple]:
    with db.connect() as conn:
        return [tuple(row) for row in conn.execute("SELECT * FROM club_reviews ORDER BY id")]


def _corpus_snapshot() -> dict[str, bytes]:
    return {
        str(path.relative_to(DATA_DIR)): path.read_bytes()
        for path in DATA_DIR.rglob("*")
        if path.is_file()
    }


def _memory_count() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


class _Response:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.modal = None

    async def send_message(self, message: str, *, ephemeral: bool = False) -> None:
        self.messages.append((message, ephemeral))

    async def send_modal(self, modal) -> None:
        self.modal = modal


class _Interaction:
    def __init__(self, user_id: int) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.id = 271828
        self.response = _Response()


def _invoke_command(interaction: _Interaction, book: str) -> None:
    asyncio.run(commands.private_feedback_cmd.callback(interaction, book))


def test_linked_member_feedback_is_private_memory_only(fresh_db):
    identities.link_member_identity(str(USER_ID), "jamie", linked_by="test")
    reviews_before = _reviews()
    corpus_before = _corpus_snapshot()

    result = commands._save_private_book_feedback(
        user_id=USER_ID,
        book_value="watchmen",
        note=NOTE,
        source_message_id="interaction-271828",
    )

    assert result == {
        "id": result["id"],
        "member_slug": "jamie",
        "book_slug": "watchmen",
        "book_title": "Watchmen",
    }
    memories = fresh_db.get_memories(subject="jamie")
    assert len(memories) == 1
    assert memories[0]["scope"] == "member"
    assert memories[0]["subject"] == "jamie"
    assert memories[0]["source"] == "private_book_feedback"
    assert memories[0]["source_user_id"] == str(USER_ID)
    assert memories[0]["source_message_id"] == "interaction-271828"
    assert "Watchmen (watchmen)" in memories[0]["note"]
    assert NOTE in memories[0]["note"]
    assert memories[0]["created_at"]
    assert _reviews() == reviews_before
    assert _corpus_snapshot() == corpus_before


def test_unlinked_member_is_rejected_without_a_write(fresh_db):
    with pytest.raises(ValueError, match="linked club member"):
        commands._save_private_book_feedback(
            user_id=USER_ID,
            book_value="watchmen",
            note=NOTE,
        )
    assert _memory_count() == 0

    interaction = _Interaction(USER_ID)
    _invoke_command(interaction, "watchmen")
    assert interaction.response.modal is None
    assert interaction.response.messages == [
        (
            "I can only save private book feedback for linked club members — ask an admin to "
            "link your Discord account first.",
            True,
        )
    ]


def test_unknown_book_is_rejected_without_a_write(fresh_db):
    identities.link_member_identity(str(USER_ID), "jamie", linked_by="test")
    with pytest.raises(ValueError, match="couldn't find that book"):
        commands._save_private_book_feedback(
            user_id=USER_ID,
            book_value="definitely-not-a-club-book-xyz",
            note=NOTE,
        )
    assert _memory_count() == 0

    interaction = _Interaction(USER_ID)
    _invoke_command(interaction, "definitely-not-a-club-book-xyz")
    assert interaction.response.modal is None
    assert interaction.response.messages[0][1] is True
    assert "didn't save anything" in interaction.response.messages[0][0]
    assert _memory_count() == 0


def test_valid_command_opens_one_private_note_modal(fresh_db):
    identities.link_member_identity(str(USER_ID), "jamie", linked_by="test")
    interaction = _Interaction(USER_ID)

    _invoke_command(interaction, "watchmen")

    modal = interaction.response.modal
    assert isinstance(modal, commands.PrivateBookFeedbackModal)
    assert modal.book_slug == "watchmen"
    assert modal.note.text == "What should Oliver remember?"
    assert "DNF reason" in modal.note.component.placeholder
    payload = modal.to_dict()["components"][0]
    assert payload["type"] == 18  # Discord label container, not a legacy top-level TextInput
    assert payload["component"]["type"] == 4
    assert _memory_count() == 0  # opening the modal never records a partial note
    confirmation = commands.PRIVATE_FEEDBACK_CONFIRMATION.lower()
    assert "privately" in confirmation
    assert "oliver's memory" in confirmation
    assert "public review" in confirmation
    assert "website" in confirmation


def test_modal_submission_saves_private_feedback(fresh_db):
    identities.link_member_identity(str(USER_ID), "jamie", linked_by="test")
    modal = commands.PrivateBookFeedbackModal(book={"slug": "watchmen", "title": "Watchmen"})
    modal.note.component._value = NOTE
    interaction = _Interaction(USER_ID)

    asyncio.run(modal.on_submit(interaction))

    memories = fresh_db.get_memories(subject="jamie")
    assert len(memories) == 1
    assert NOTE in memories[0]["note"]
    assert interaction.response.messages == [(commands.PRIVATE_FEEDBACK_CONFIRMATION, True)]


def test_private_dnf_reason_is_recallable_only_by_its_member(fresh_db):
    identities.link_member_identity(str(USER_ID), "jamie", linked_by="test")
    commands._save_private_book_feedback(
        user_id=USER_ID,
        book_value="watchmen",
        note=NOTE,
        source_message_id="interaction-271828",
    )

    jamie = json.loads(
        dispatch(
            "recall",
            {"query": "argument felt repetitive"},
            {
                "speaker": "Jamie",
                "speaker_user_id": str(USER_ID),
                "member_slug": "jamie",
            },
        )
    )
    erik = json.loads(
        dispatch(
            "recall",
            {"query": "argument felt repetitive"},
            {
                "speaker": "Erik",
                "speaker_user_id": "other-user",
                "member_slug": "erik",
            },
        )
    )

    assert len(jamie) == 1
    assert jamie[0]["scope"] == "member"
    assert "Watchmen (watchmen)" in jamie[0]["note"]
    assert erik == []
