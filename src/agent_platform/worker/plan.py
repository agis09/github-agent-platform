"""Plan worker: produce a PlanResult for one issue (read-only sandbox).

Inputs (env):
  PLAN_TASK_ID / PLAN_REPOSITORY / PLAN_ISSUE_NUMBER / PLAN_BASE_BRANCH /
  PLAN_BASE_SHA                     — PlanJobSpec fields
  PLAN_ISSUE_TITLE / PLAN_ISSUE_BODY — issue content (untrusted)
  AGENT_PLATFORM_AGENT_*            — runtime settings (config.py)
  /workspace/repo                   — optional repository snapshot
Output: /out/plan_result.json and the PLAN_RESULT log block.
Exit: 0 success, 1 the plan failed (no fallback result).
"""

import json
import os
import sys
from pathlib import Path

from agent_platform.adapters.agent.local import LocalAgentAdapter
from agent_platform.config import Settings
from agent_platform.domain.models import PlanJobSpec
from agent_platform.errors import AgentPlatformError
from agent_platform.worker.network_guard import wait_for_egress_policy


def main() -> int:
    settings = Settings.from_environ()
    repo_root = Path(os.environ.get("PLAN_REPO_DIR", "/workspace/repo"))
    spec = PlanJobSpec(
        task_id=os.environ["PLAN_TASK_ID"],
        repository=os.environ["PLAN_REPOSITORY"],
        issue_number=int(os.environ["PLAN_ISSUE_NUMBER"]),
        base_branch=os.environ["PLAN_BASE_BRANCH"],
        base_sha=os.environ["PLAN_BASE_SHA"],
        issue_title=os.environ.get("PLAN_ISSUE_TITLE") or None,
        issue_body=os.environ.get("PLAN_ISSUE_BODY") or None,
    )
    adapter = LocalAgentAdapter(settings, repo_root=repo_root if repo_root.exists() else None)
    try:
        print(f"worker: {wait_for_egress_policy()}", file=sys.stderr)
        plan = adapter.plan(spec)
    except AgentPlatformError as exc:
        print(f"worker: plan failed: {exc}", file=sys.stderr)
        return 1
    finally:
        # Audit trail: event counts + token usage only. The message text is
        # never logged (LLM request/response stays out of logs).
        if adapter.last_outcome is not None:
            print(f"worker: {adapter.last_outcome.audit_line()}", file=sys.stderr)

    payload = json.loads(plan.model_dump_json())
    # The orchestrator recomputes plan_hash; the adapter's value is never trusted.
    payload.pop("plan_hash", None)
    result_text = json.dumps(payload, indent=2) + "\n"
    out_dir = Path(os.environ.get("PLAN_OUT_DIR", "/out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan_result.json").write_text(result_text)
    print("PLAN_RESULT_BEGIN")
    print(result_text, end="")
    print("PLAN_RESULT_END")
    print(f"worker: plan_result.json written ({len(plan.proposed_changes)} changes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
