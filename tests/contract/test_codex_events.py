"""Contract: codex JSONL event parser — normal, fatal, malformed streams."""

import pytest

from agent_platform import codex_events
from agent_platform.errors import AdapterError

NORMAL_STREAM = [
    '{"type": "thread.started", "thread_id": "0199a213-81c0"}',
    '{"type": "turn.started"}',
    '{"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "ls"}}',
    '{"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "hello"}}',
    '{"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20,'
    ' "output_tokens": 50, "reasoning_output_tokens": 10}}',
]


def test_parse_normal_stream():
    summary = codex_events.summarize_stream(NORMAL_STREAM)
    assert summary.thread_id == "0199a213-81c0"
    assert summary.last_agent_message == "hello"
    assert summary.usage is not None
    assert summary.usage.input_tokens == 100
    assert summary.usage.output_tokens == 50
    assert summary.fatal_error is None


def test_missing_agent_message():
    lines = [line for line in NORMAL_STREAM if "agent_message" not in line]
    summary = codex_events.summarize_stream(lines)
    assert summary.last_agent_message is None


def test_turn_failed_is_fatal():
    lines = NORMAL_STREAM[:3] + ['{"type": "turn.failed", "message": "rate limited"}']
    summary = codex_events.summarize_stream(lines)
    with pytest.raises(AdapterError, match="rate limited"):
        codex_events.require_success(summary)


def test_error_event_is_fatal():
    lines = NORMAL_STREAM + ['{"type": "error", "message": "server exploded"}']
    summary = codex_events.summarize_stream(lines)
    with pytest.raises(AdapterError):
        codex_events.require_success(summary)


def test_multiple_agent_messages_last_wins():
    lines = [
        '{"type": "item.completed", "item": {"id": "1",'
        ' "type": "agent_message", "text": "first"}}',
        '{"type": "item.completed", "item": {"id": "2",'
        ' "type": "agent_message", "text": "second"}}',
    ]
    summary = codex_events.summarize_stream(lines)
    assert summary.last_agent_message == "second"


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "not json at all",
        "[1, 2, 3]",  # not an object
        '{"no_type": true}',
    ],
)
def test_malformed_lines_raise(line):
    with pytest.raises(codex_events.CodexEventError):
        codex_events.parse_event(line)


def test_thread_started_without_thread_id_raises():
    summary = codex_events.CodexRunSummary()
    event = codex_events.parse_event('{"type": "thread.started"}')
    with pytest.raises(codex_events.CodexEventError):
        codex_events.apply_event(summary, event)


def test_usage_with_non_integer_raises():
    summary = codex_events.CodexRunSummary()
    event = codex_events.parse_event(
        '{"type": "turn.completed", "usage": {"input_tokens": "many"}}'
    )
    with pytest.raises(codex_events.CodexEventError):
        codex_events.apply_event(summary, event)


def test_incomplete_stream_does_not_raise_without_require():
    # stream cut mid-run: parse succeeds, no fatal events — caller decides
    summary = codex_events.summarize_stream(NORMAL_STREAM[:3])
    assert summary.usage is None
    assert summary.fatal_error is None
