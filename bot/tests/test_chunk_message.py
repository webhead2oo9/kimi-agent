from __future__ import annotations

from agent.backfill import strip_chunk_marker
from discord_adapter.io import DISCORD_MAX_LENGTH, chunk_message


def _fences_balanced(chunk: str) -> bool:
    return chunk.count("```") % 2 == 0


def test_short_message_is_one_chunk() -> None:
    assert chunk_message("hello") == ["hello"]


def test_long_plain_text_splits_on_newlines_within_limit() -> None:
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(80))
    chunks = chunk_message(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= DISCORD_MAX_LENGTH
    rejoined = " ".join(strip_chunk_marker(c) for c in chunks)
    assert "line 0" in rejoined and "line 79" in rejoined


def test_code_fence_straddling_split_is_closed_and_reopened() -> None:
    code_lines = "\n".join(f"value_{i} = {i}" for i in range(200))
    text = f"Here is the code:\n```python\n{code_lines}\n```\ndone"
    assert len(text) > DISCORD_MAX_LENGTH

    chunks = chunk_message(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= DISCORD_MAX_LENGTH
        # Every chunk renders standalone: no unclosed ``` fence.
        assert _fences_balanced(strip_chunk_marker(chunk)), chunk
    # The continuation chunk reopens the block with its language tag.
    assert chunks[1].startswith("```python\n")


def test_fence_without_language_tag_reopens_bare() -> None:
    code_lines = "\n".join(f"value_{i} = {i}" for i in range(200))
    text = f"```\n{code_lines}\n```"

    chunks = chunk_message(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert _fences_balanced(strip_chunk_marker(chunk)), chunk
    assert chunks[1].startswith("```\n")


def test_multiple_balanced_fences_before_split_do_not_trigger_reopen() -> None:
    block = "```py\na = 1\n```\nplain text follows here\n"
    text = block * 60  # long, but every fence closes before the split point
    chunks = chunk_message(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert _fences_balanced(strip_chunk_marker(chunk)), chunk
    assert not chunks[1].startswith("```")


def test_unbroken_text_inside_fence_terminates_and_balances() -> None:
    # No newline/space split points inside the fence: forces hard cuts. The
    # loop must terminate and still emit balanced chunks.
    text = "```\n" + "x" * 6000 + "\n```"
    chunks = chunk_message(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= DISCORD_MAX_LENGTH
        assert _fences_balanced(strip_chunk_marker(chunk)), chunk


def test_split_inside_indented_code_block_preserves_indentation() -> None:
    body_line = "    total += compute_step(35)  # indented body line\n"
    text = "```python\n" + body_line * 80 + "```"
    assert len(text) > 2000

    chunks = chunk_message(text)

    assert len(chunks) > 1
    for chunk in chunks[1:]:
        # Continuation chunks reopen the fence; the first code line after the
        # reopen must keep its leading indentation (a bare lstrip() used to
        # delete it, corrupting the split code block).
        lines = chunk.split("\n")
        assert lines[0] == "```python"
        assert lines[1].startswith("    total"), lines[1]


def test_newline_split_outside_fence_does_not_strip_list_indentation() -> None:
    item = "- item with some text to pad the line out toward the limit\n"
    indented = "  - nested item that must keep its two leading spaces\n"
    text = (item + indented) * 40
    assert len(text) > 2000

    chunks = chunk_message(text)

    assert len(chunks) > 1
    for chunk in chunks[1:]:
        first_line = chunk.split("\n", 1)[0]
        assert first_line.startswith(("- ", "  - ")), first_line
