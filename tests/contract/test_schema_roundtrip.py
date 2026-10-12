"""Contract: fixtures validate against both pydantic and jsonschema;
invalid fixtures are rejected by both; round-trip is byte-stable."""

import json
from pathlib import Path
from typing import Final

import pytest
from jsonschema import Draft202012Validator, ValidationError
from pydantic import ValidationError as PydanticValidationError

from agent_platform.domain.models import ExecutionResult, PlanResult, ReviewResult
from agent_platform.schema_export import build

MODEL_BY_NAME: Final = {
    "plan_result": PlanResult,
    "execution_result": ExecutionResult,
    "review_result": ReviewResult,
}
FIXTURES: Final = Path("tests/fixtures")


@pytest.mark.parametrize(
    ("schema_name", "fixture_name"),
    [
        ("plan_result", "plan_result_valid.json"),
        ("execution_result", "execution_result_valid.json"),
        ("review_result", "review_result_valid.json"),
    ],
)
def test_valid_fixture_passes_jsonschema_and_pydantic(schema_name, fixture_name):
    data = json.loads((FIXTURES / fixture_name).read_text())
    Draft202012Validator(build(schema_name)).validate(data)
    model = MODEL_BY_NAME[schema_name].model_validate(data)
    # round-trip: validate -> dump -> validate again
    dumped = json.loads(model.model_dump_json())
    Draft202012Validator(build(schema_name)).validate(dumped)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "plan_result_missing_task_id.json",
        "plan_result_bad_sha.json",
        "plan_result_extra_field.json",
        "execution_result_bad_status.json",
    ],
)
def test_invalid_fixtures_rejected_by_both(fixture_name):
    path = FIXTURES / "invalid" / fixture_name
    data = json.loads(path.read_text())
    # figure out which schema applies by presence of fields
    if "status" in data and "plan_hash" in data:
        schema_name = "execution_result"
    else:
        schema_name = "plan_result"
    validator = Draft202012Validator(build(schema_name))
    with pytest.raises(ValidationError):
        validator.validate(data)
    model = MODEL_BY_NAME[schema_name]
    with pytest.raises(PydanticValidationError):
        model.model_validate(data)
