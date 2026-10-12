"""The gateway form of the output schemas (codex_schema.py).

- no `$ref` / `$defs` / `default` / `title` left anywhere
- every object property is required
- constraints are kept: what the committed schema (and pydantic) rejects,
  the gateway form rejects too; a complete valid result passes both
- the plan adapter hands codex the gateway form, not the committed file
"""

import json
import stat
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from agent_platform.adapters.agent.local import LocalAgentAdapter
from agent_platform.codex_schema import gateway_schema, write_gateway_schema
from agent_platform.domain.models import PlanResult
from agent_platform.schema_export import build
from tests.contract.test_local_adapter_codex import JOB, VALID_PLAN, _jsonl_stream, _settings

MODELS = ("plan_result", "review_result", "execution_result")


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


@pytest.mark.parametrize("model", MODELS)
def test_gateway_form_has_no_refs_defaults_or_optional_properties(model):
    schema = gateway_schema(build(model))
    Draft202012Validator.check_schema(schema)
    for node in _walk(schema):
        assert not {"$ref", "$defs", "default", "title"} & node.keys(), node
        if isinstance(node.get("properties"), dict):
            assert node["required"] == list(node["properties"])


def test_plan_items_are_inlined_with_their_constraints():
    items = gateway_schema(build("plan_result"))["properties"]["proposed_changes"]["items"]
    assert items["type"] == "object"
    assert items["additionalProperties"] is False
    assert items["required"] == ["path", "reason"]
    assert items["properties"]["path"]["minLength"] == 1


def test_complete_plan_passes_gateway_form_and_model():
    Draft202012Validator(gateway_schema(build("plan_result"))).validate(VALID_PLAN)
    PlanResult.model_validate(VALID_PLAN)


@pytest.mark.parametrize(
    "change",
    [
        {"path": "", "reason": "empty path"},
        {"path": "a.py"},
        {"path": "a.py", "reason": "x", "extra": 1},
    ],
)
def test_invalid_change_is_still_rejected(change):
    bad = {**VALID_PLAN, "proposed_changes": [change]}
    with pytest.raises(ValidationError):
        Draft202012Validator(build("plan_result")).validate(bad)
    with pytest.raises(ValidationError):
        Draft202012Validator(gateway_schema(build("plan_result"))).validate(bad)


def test_omitted_field_is_rejected_by_gateway_form():
    # Optional in the contract, required for the model's output.
    bad = {k: v for k, v in VALID_PLAN.items() if k != "proposed_changes"}
    with pytest.raises(ValidationError):
        Draft202012Validator(gateway_schema(build("plan_result"))).validate(bad)


@pytest.mark.parametrize(
    ("schema", "match"),
    [
        ({"$ref": "http://example.invalid/x"}, "unsupported"),
        ({"$ref": "#/$defs/Missing", "$defs": {}}, "missing"),
        (
            {"$ref": "#/$defs/Node", "$defs": {"Node": {
                "type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}},
            }}},
            "recursive",
        ),
    ],
)
def test_unconvertible_refs_raise(schema, match):
    with pytest.raises(ValueError, match=match):
        gateway_schema(schema)


def test_write_gateway_schema_is_owner_only(tmp_path):
    source = tmp_path / "plan_result.schema.json"
    source.write_text(json.dumps(build("plan_result")))
    target = write_gateway_schema(source, tmp_path)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text()) == gateway_schema(build("plan_result"))


def test_plan_adapter_passes_gateway_form_to_codex(tmp_path):
    captured = tmp_path / "captured-schema.json"
    script = tmp_path / "fake-codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import shutil, sys\n"
        "args = sys.argv\n"
        f"shutil.copy(args[args.index('--output-schema') + 1], {str(captured)!r})\n"
        f"sys.stdout.write({_jsonl_stream(VALID_PLAN)!r})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    plan = LocalAgentAdapter(_settings(tmp_path)).plan(JOB)

    assert len(plan.proposed_changes) == 1
    assert json.loads(Path(captured).read_text()) == gateway_schema(build("plan_result"))
