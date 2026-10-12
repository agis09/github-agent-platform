"""Contract: committed JSON Schemas must equal pydantic-generated ones."""

import json
from pathlib import Path

import pytest

from agent_platform.schema_export import SCHEMAS, build

SCHEMA_FILES = {
    "plan_result": Path("schemas/plan_result.schema.json"),
    "execution_result": Path("schemas/execution_result.schema.json"),
    "review_result": Path("schemas/review_result.schema.json"),
    "search_gateway_request": Path("schemas/search_gateway_request.schema.json"),
    "search_result": Path("schemas/search_result.schema.json"),
}


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_committed_schema_matches_model(name: str):
    committed = json.loads(SCHEMA_FILES[name].read_text())
    assert committed == build(name)


def test_every_model_has_committed_file():
    assert set(SCHEMAS) == set(SCHEMA_FILES)
    for path in SCHEMA_FILES.values():
        assert path.exists()
