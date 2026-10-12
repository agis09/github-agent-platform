"""Export pydantic model JSON Schemas (source of truth) to stdout.

Run ``uv run python -m agent_platform.schema_export`` and write the output
into ``schemas/``; the contract test asserts the committed files match.
"""

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from agent_platform.domain.models import (
    ExecutionResult,
    PlanResult,
    ReviewResult,
)
from agent_platform.search.base import GatewaySearchRequest, SearchResult

SCHEMAS: Final[Mapping[str, type[BaseModel]]] = {
    "plan_result": PlanResult,
    "execution_result": ExecutionResult,
    "review_result": ReviewResult,
    # Search Gateway contract (worker <-> gateway).
    "search_gateway_request": GatewaySearchRequest,
    "search_result": SearchResult,
}


def build(schema_name: str) -> dict[str, object]:
    try:
        model = SCHEMAS[schema_name]
    except KeyError as exc:
        msg = f"unknown schema: {schema_name!r}"
        raise ValueError(msg) from exc
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


def schema_file(schema_name: str) -> Path:
    """Path of the committed schemas/<name>.schema.json. Resolved from the
    package location (image layout: /opt/agent-platform/{src,schemas}),
    then from the CWD for repo checkouts."""
    package_root = Path(__file__).resolve().parents[2]
    for base in (package_root, Path.cwd()):
        path = base / "schemas" / f"{schema_name}.schema.json"
        if path.exists():
            return path
    msg = f"schema file missing: schemas/{schema_name}.schema.json"
    raise FileNotFoundError(msg)


def main() -> int:
    for name in SCHEMAS:
        json.dump(build(name), sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
