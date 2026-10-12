"""Canonical JSON and plan_hash computation.

Canonicalization rules: recursively sorted keys, no separator whitespace,
UTF-8, ensure_ascii=False, allow_nan=False. Floats are structurally banned
from the models; if one is ever needed, extend this to RFC 8785 number
formatting first.
"""

import hashlib
import json
from typing import Any

from agent_platform.domain.models import PlanResult


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def compute_plan_hash(result: PlanResult) -> str:
    """SHA-256 over the canonical JSON of the plan, excluding plan_hash itself.

    Includes task_id / repository / base_sha, so a plan for another task,
    repo, or base SHA never passes approval hash matching.
    """
    data = result.model_dump(mode="json", exclude={"plan_hash"})
    return "sha256:" + hashlib.sha256(canonical_json(data)).hexdigest()


def verify_plan_hash(result: PlanResult) -> bool:
    return result.plan_hash == compute_plan_hash(result)
