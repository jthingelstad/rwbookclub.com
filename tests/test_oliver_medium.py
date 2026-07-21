"""Oliver writes for the medium it's on — email vs Discord — via _system_blocks(medium),
plus the guardrail that it never posts to the group on a member's behalf."""

from agent import oliver


def _texts(medium):
    return " ".join(b["text"] for b in oliver._system_blocks(medium))


def test_email_medium_gives_warm_personal_email_voice():
    tl = _texts("email").lower()
    assert "this message — email" in tl
    assert "warm" in tl and "greeting" in tl
    assert "only the email body" in tl  # don't narrate the runtime/thread
    assert "don't sign off" in tl
    assert "this message — discord" not in tl


def test_discord_medium_gives_short_chat_voice():
    tl = _texts("discord").lower()
    assert "this message — discord" in tl
    assert "no greeting" in tl and "no sign-off" in tl
    assert "this message — email" not in tl


def test_raw_medium_adds_no_framing():
    # generate()'s intentionally-sectioned topic/release-notes emails must NOT get the
    # "no headers, warm greeting" email framing — it would fight their own prompts.
    blocks = oliver._system_blocks("raw")
    assert len(blocks) == 2
    assert "THIS MESSAGE" not in " ".join(b["text"] for b in blocks)


def test_charter_block_keeps_its_own_cache_breakpoint():
    # The large charter is cached once and shared across mediums (the medium block is the tail).
    assert oliver._system_blocks("email")[0].get("cache_control") == {"type": "ephemeral"}


def test_guardrail_never_posts_to_group_on_a_members_behalf():
    p = oliver.OPERATIONAL_PROMPT
    assert "on another member's behalf" in p
    assert "post it themselves" in p


def test_member_pronoun_policy_is_explicit_neutral_and_not_performative():
    prompt = oliver.OPERATIONAL_PROMPT
    assert "NEVER infer pronouns" in prompt
    assert "use their name or singular they/them" in prompt
    assert "only when an ordinary sentence genuinely needs" in prompt
    assert "Do not introduce a pronoun merely to demonstrate" in prompt
    assert "do not volunteer, list, emphasize, or remark" in prompt
    assert "do not repeat pronouns where the member's name would read more naturally" in prompt


def test_horizon_prompt_is_status_not_picker_pressure():
    prompt = oliver.OPERATIONAL_PROMPT
    assert "call horizon" in prompt
    assert "calm runway/status read" in prompt
    assert "not that they are behind, summoned, or required to pick" in prompt


def test_book_mention_memory_uses_cultural_language_and_revised_tags():
    prompt = oliver.OPERATIONAL_PROMPT
    assert "caution (negative fit, DNF, or too long), context, inquiry, or joke" in prompt
    assert "never call it the Book Cloud unless the member used that term first" in prompt
    assert "books we've been circling" in prompt
