from __future__ import annotations

from agent.auto_handoff import (
    build_auto_handoff_request,
    derive_thread_name,
    reply_exceeds_threshold,
)


def test_reply_exceeds_threshold_by_lines() -> None:
    five_lines = "a\nb\nc\nd\ne"
    assert reply_exceeds_threshold(five_lines, min_lines=4, min_chars=None) is True
    assert reply_exceeds_threshold("a\nb\nc\nd", min_lines=4, min_chars=None) is False


def test_reply_exceeds_threshold_by_chars_catches_wrapped_wall() -> None:
    wall = "x" * 601  # a single line, no newlines, so only the char check catches it
    assert reply_exceeds_threshold(wall, min_lines=4, min_chars=600) is True
    assert reply_exceeds_threshold("x" * 600, min_lines=4, min_chars=600) is False


def test_reply_exceeds_threshold_absent_threshold_not_checked() -> None:
    assert reply_exceeds_threshold("a\nb\nc\nd\ne", min_lines=None, min_chars=None) is False
    long_line = "x" * 5000
    assert reply_exceeds_threshold(long_line, min_lines=4, min_chars=None) is False


def test_derive_thread_name_collapses_and_truncates() -> None:
    assert derive_thread_name("  what   is   foveated\nrendering  ", fallback="fb") == (
        "what is foveated rendering"
    )
    assert derive_thread_name("a" * 250, fallback="fb") == "a" * 100


def test_derive_thread_name_falls_back_when_empty() -> None:
    assert derive_thread_name("   ", fallback="Chat with Kimi") == "Chat with Kimi"
    assert derive_thread_name("", fallback="") == "Thread"


def test_build_request_none_without_thresholds() -> None:
    assert (
        build_auto_handoff_request(
            response_text="a\nb\nc\nd\ne",
            question_text="hi",
            bot_name="Kimi",
            min_lines=None,
            min_chars=None,
        )
        is None
    )


def test_build_request_none_for_short_reply() -> None:
    assert (
        build_auto_handoff_request(
            response_text="just a line or two",
            question_text="hi",
            bot_name="Kimi",
            min_lines=4,
            min_chars=600,
        )
        is None
    )


def test_build_request_synthesizes_named_thread() -> None:
    request = build_auto_handoff_request(
        response_text="a\nb\nc\nd\ne\nf",
        question_text="how do I fix Quest 3 link cable",
        bot_name="Kimi",
        min_lines=4,
        min_chars=600,
    )
    assert request is not None
    assert request.name == "how do I fix Quest 3 link cable"
    # The backstop threads a long reply where it was already going. Only a user
    # who names a channel can send one elsewhere, so this path must never carry
    # a target: an operator threshold is not a request to move channels.
    assert request.target_channel_id is None


def test_build_request_always_skips_length_check() -> None:
    request = build_auto_handoff_request(
        response_text="ok",
        question_text="quick one",
        bot_name="Kimi",
        min_lines=None,
        min_chars=None,
        always=True,
    )
    assert request is not None
    assert request.name == "quick one"


def test_build_request_uses_fallback_name_when_question_blank() -> None:
    request = build_auto_handoff_request(
        response_text="a\nb\nc\nd\ne\nf",
        question_text="   ",
        bot_name="Kimi",
        min_lines=4,
        min_chars=None,
    )
    assert request is not None
    assert request.name == "Chat with Kimi"
