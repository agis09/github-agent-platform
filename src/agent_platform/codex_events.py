"""Parser for `codex exec --json` JSONL event streams (contract-tested).

Reference: codex non-interactive mode. Event kinds:
- thread.started  {thread_id}
- turn.started
- turn.completed  {usage: {input_tokens, cached_input_tokens,
                    output_tokens, reasoning_output_tokens}}
- turn.failed
- item.started / item.completed / item.updated  {item: {id, type, status, ...}}
- error

The parser is strict: malformed lines raise CodexEventError — silent
skipping would hide adapter failures.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from agent_platform.errors import AdapterError


class CodexEventError(AdapterError):
    """Malformed or fatal event in the codex JSONL stream."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0


@dataclass
class CodexRunSummary:
    thread_id: str | None = None
    agent_messages: list[str] = field(default_factory=list)
    usage: Usage | None = None
    turn_failed: str | None = None
    error: str | None = None

    @property
    def last_agent_message(self) -> str | None:
        return self.agent_messages[-1] if self.agent_messages else None

    @property
    def fatal_error(self) -> str | None:
        return self.turn_failed or self.error


@dataclass(frozen=True)
class ParsedEvent:
    kind: str
    payload: dict[str, Any]


def parse_event(line: str) -> ParsedEvent:
    """Parse one JSONL line into (kind, payload). Strict on malformed input."""
    if not line.strip():
        raise CodexEventError("empty event line")
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        msg = f"malformed JSONL event: {exc}"
        raise CodexEventError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"event must be a JSON object, got {type(payload).__name__}"
        raise CodexEventError(msg)
    kind = payload.get("type")
    if not isinstance(kind, str) or not kind:
        msg = f"event missing 'type': keys={sorted(payload)}"
        raise CodexEventError(msg)
    return ParsedEvent(kind=kind, payload=payload)


def apply_event(summary: CodexRunSummary, event: ParsedEvent) -> None:
    """Fold one parsed event into the run summary."""
    kind = event.kind
    if kind == "thread.started":
        thread_id = event.payload.get("thread_id")
        if not isinstance(thread_id, str):
            raise CodexEventError("thread.started without string thread_id")
        summary.thread_id = thread_id
    elif kind == "turn.completed":
        usage = event.payload.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise CodexEventError("turn.completed usage must be an object")
            summary.usage = Usage(
                input_tokens=_int_field(usage, "input_tokens"),
                cached_input_tokens=_int_field(usage, "cached_input_tokens"),
                output_tokens=_int_field(usage, "output_tokens"),
                reasoning_output_tokens=_int_field(usage, "reasoning_output_tokens"),
            )
    elif kind == "turn.failed":
        message = event.payload.get("message") or json.dumps(event.payload)
        summary.turn_failed = str(message)
    elif kind == "error":
        message = event.payload.get("message") or json.dumps(event.payload)
        summary.error = str(message)
    elif kind.startswith("item."):
        item = event.payload.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if kind in ("item.completed", "item.updated") and isinstance(text, str):
                summary.agent_messages.append(text)
    # other event kinds are informational; record nothing


def _int_field(data: dict[str, Any], name: str) -> int:
    value = data.get(name, 0)
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"usage field {name} must be an integer"
        raise CodexEventError(msg)
    return value


def summarize_stream(lines: list[str]) -> CodexRunSummary:
    """Fold a full JSONL stream (list of lines) into a CodexRunSummary."""
    summary = CodexRunSummary()
    for line in lines:
        apply_event(summary, parse_event(line))
    return summary


def require_success(summary: CodexRunSummary) -> CodexRunSummary:
    """Raise on fatal events. Timeout mapping is done by the caller (which
    knows whether the process died from a timeout kill)."""
    if summary.fatal_error is not None:
        raise AdapterError(f"codex run failed: {summary.fatal_error}")
    return summary
