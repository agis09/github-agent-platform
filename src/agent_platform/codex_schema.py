"""The --output-schema form handed to codex (structured output on the gateway).

The committed schemas (schemas/*.schema.json, generated from the pydantic
models) stay the contract. The gateway's constrained decoding mishandles
two features of those files: `$ref` into `$defs`, and optional properties
with a `default`. With them, `proposed_changes` (a `$ref` item array with
`default: []`) came back empty in 18/20 local runs for an issue that named
its files; inlined + all-required + no default gave a non-empty list in
17/19.

So codex gets a derived schema: `$ref`s inlined, every object property
required, `default` / `title` removed. Constraints (type, enum, const,
anyOf, minLength, additionalProperties, ...) are kept unchanged, so any
output that satisfies it still has to pass the pydantic model afterwards —
the conversion narrows what the model may emit, it never relaxes validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DROPPED_KEYS = frozenset({"default", "title"})


def gateway_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the gateway-compatible form of a committed JSON Schema."""
    defs = schema.get("$defs", {})

    def convert(node: Any, seen: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [convert(item, seen) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if ref is not None:
            prefix = "#/$defs/"
            if not isinstance(ref, str) or not ref.startswith(prefix):
                msg = f"unsupported $ref: {ref!r}"
                raise ValueError(msg)
            name = ref[len(prefix):]
            if name not in defs:
                msg = f"$ref to missing definition: {ref!r}"
                raise ValueError(msg)
            if name in seen:
                msg = f"recursive $ref cannot be inlined: {ref!r}"
                raise ValueError(msg)
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            return convert({**defs[name], **siblings}, (*seen, name))
        out = {
            key: convert(value, seen)
            for key, value in node.items()
            if key not in _DROPPED_KEYS and key != "$defs"
        }
        properties = out.get("properties")
        if isinstance(properties, dict):
            out["required"] = list(properties)
        return out

    converted = convert(schema, ())
    if not isinstance(converted, dict):  # pragma: no cover — schema root is an object
        msg = "schema root must be an object"
        raise ValueError(msg)
    return converted


def write_gateway_schema(source: Path, directory: Path) -> Path:
    """Write the gateway form of `source` into `directory` and return its path."""
    schema = json.loads(source.read_text())
    target = directory / f"gateway-{source.name}"
    target.write_text(json.dumps(gateway_schema(schema), indent=2, sort_keys=True))
    target.chmod(0o600)
    return target
