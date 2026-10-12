"""Review worker: review a PR diff in a READ-ONLY sandbox.

Inputs (env):
  REVIEW_TASK_ID / REVIEW_REPOSITORY / REVIEW_PLAN_HASH /
  REVIEW_BASE_SHA / REVIEW_HEAD_SHA
  REVIEW_ISSUE_TITLE / REVIEW_ISSUE_BODY — issue context (untrusted)
  REVIEW_PLAN_JSON                      — the approved plan (untrusted)
  REVIEW_DIFF                           — base..head patch (untrusted)
  REVIEW_TESTS                          — newline-separated test results
  AGENT_PLATFORM_AGENT_*                — runtime settings (config.py)
Output: /out/review_result.json and the REVIEW_RESULT log block.

The reviewer is a separate agent context from the implementer, cannot
modify anything and holds no credentials. Exit: 0 success, 1 failure.
"""

import json
import os
import sys
from pathlib import Path

from agent_platform.agent_runtime import RunMode, build_runtime
from agent_platform.config import Settings
from agent_platform.errors import AdapterError
from agent_platform.prompt_builder import build_review_prompt
from agent_platform.schema_export import schema_file


def main() -> int:
    settings = Settings.from_environ()
    runtime = build_runtime(settings)
    prompt = build_review_prompt(
        issue_title=os.environ.get("REVIEW_ISSUE_TITLE", ""),
        issue_body=os.environ.get("REVIEW_ISSUE_BODY", ""),
        plan_json=os.environ.get("REVIEW_PLAN_JSON", "{}"),
        diff=os.environ.get("REVIEW_DIFF", ""),
        tests=os.environ.get("REVIEW_TESTS", ""),
    )
    try:
        outcome = runtime.run(
            prompt=prompt, mode=RunMode.READ_ONLY, output_schema=schema_file("review_result")
        )
    except AdapterError as exc:
        print(f"runner: {exc}", file=sys.stderr)
        return 1
    print(f"runner: {outcome.audit_line()}", file=sys.stderr)
    if not outcome.ok:
        print(
            f"runner: {outcome.runtime} failed: {outcome.failure_detail(2000)}",
            file=sys.stderr,
        )
        return 1
    text = (outcome.final_message or "").strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = None
    if not isinstance(result, dict):
        print("runner: no structured ReviewResult in the agent output", file=sys.stderr)
        return 1

    # Identity fields come from the job, not the model; the orchestrator
    # re-validates the whole result against ReviewResult.
    result.setdefault("schema_version", "1")
    result["task_id"] = os.environ["REVIEW_TASK_ID"]
    result["repository"] = os.environ["REVIEW_REPOSITORY"]
    result["plan_hash"] = os.environ["REVIEW_PLAN_HASH"]
    result["base_sha"] = os.environ["REVIEW_BASE_SHA"]
    result["head_sha"] = os.environ["REVIEW_HEAD_SHA"]

    out = Path(os.environ.get("REVIEW_OUT_DIR", "/out"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "review_result.json").write_text(json.dumps(result, indent=2) + "\n")
    print("REVIEW_RESULT_BEGIN")
    print(json.dumps(result))
    print("REVIEW_RESULT_END")
    return 0


if __name__ == "__main__":
    sys.exit(main())
