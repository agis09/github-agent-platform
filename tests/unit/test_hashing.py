"""plan_hash canonicalization: determinism, exclusion, sensitivity."""

import json

import pytest

from agent_platform.domain.hashing import canonical_json, compute_plan_hash, verify_plan_hash
from agent_platform.domain.models import PlanResult, ProposedChange


def _plan(**overrides) -> PlanResult:
    data = dict(
        task_id="task-abc123",
        repository="owner/repo",
        base_sha="a" * 40,
        summary="plan summary",
        proposed_changes=[ProposedChange(path="src/x.py", reason="fix")],
        test_commands=["pytest -q"],
    )
    data.update(overrides)
    return PlanResult(**data)


def test_canonical_json_sorts_keys_recursively():
    value = {"b": 1, "a": {"d": 2, "c": 3}}
    assert canonical_json(value) == b'{"a":{"c":3,"d":2},"b":1}'


def test_canonical_json_no_whitespace_unicode():
    value = {"key": "値", "n": [1, 2]}
    assert canonical_json(value) == '{"key":"値","n":[1,2]}'.encode()


def test_canonical_json_rejects_float_nan():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_canonical_json_distinguishes_int_and_str():
    assert canonical_json({"x": 1}) != canonical_json({"x": "1"})


def test_plan_hash_format():
    h = compute_plan_hash(_plan())
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64
    int(h.removeprefix("sha256:"), 16)  # hex parseable


def test_plan_hash_deterministic():
    assert compute_plan_hash(_plan()) == compute_plan_hash(_plan())


def test_plan_hash_excludes_plan_hash_field():
    a = _plan(plan_hash="sha256:" + "0" * 64)
    b = _plan(plan_hash="")
    assert compute_plan_hash(a) == compute_plan_hash(b)


def test_plan_hash_changes_on_any_field_change():
    base = compute_plan_hash(_plan())
    variants = [
        _plan(summary="different"),
        _plan(task_id="task-other"),
        _plan(repository="owner/other"),
        _plan(base_sha="b" * 40),
        _plan(questions=["why?"]),
        _plan(test_commands=["pytest -q", "ruff check"]),
        _plan(requires_human_input=True),
    ]
    for variant in variants:
        assert compute_plan_hash(variant) != base


def test_verify_plan_hash():
    plan = _plan(plan_hash=compute_plan_hash(_plan()))
    assert verify_plan_hash(plan)
    assert not verify_plan_hash(_plan(plan_hash="sha256:" + "f" * 64))


def test_canonical_json_matches_reference_implementation():
    value = {"z": [3, 1], "a": {"y": "x"}}
    expected = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    assert canonical_json(value) == expected
