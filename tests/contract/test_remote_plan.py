"""Contract: RemotePlanAdapter + KubeJobLauncher against a fake kubectl.

Covers the plan orchestration contract offline: launch renders the Job template,
wait distinguishes success/failure reasons, collect extracts the log-marker
PlanResult, and duplicate-launch protection refuses an active job.
"""

import json
import stat
import textwrap
from pathlib import Path

import pytest

from agent_platform.adapters.agent.remote import RemotePlanAdapter
from agent_platform.adapters.kubernetes.kube import KubeJobLauncher, _extract_result
from agent_platform.adapters.kubernetes.models import JobLaunchSpec, JobPhase
from agent_platform.errors import AdapterContractError, AdapterError, AdapterTimeoutError

BASE_SHA = "c" * 40
TASK_ID = "task-abc123456789"


def _spec(**overrides: object) -> JobLaunchSpec:
    values = {
        "task_id": TASK_ID,
        "repository": "owner/sandbox",
        "issue_number": 1,
        "base_branch": "main",
        "base_sha": BASE_SHA,
        "issue_title": "Fix the thing",
        "issue_body": "steps...",
        "plan_image": "worker-p2:p2",
    }
    values.update(overrides)
    return JobLaunchSpec(**values)  # type: ignore[arg-type]


def _valid_plan_json(task_id: str = TASK_ID) -> str:
    return json.dumps(
        {
            "schema_version": "1",
            "task_id": task_id,
            "repository": "owner/sandbox",
            "base_sha": BASE_SHA,
            "summary": "s",
            "assumptions": [],
            "questions": [],
            "proposed_changes": [],
            "test_commands": [],
            "risks": [],
            "requires_human_input": False,
        }
    )


def _normalize_script(script: str) -> str:
    """Prefix that shifts kubectl global args out of $@ so test scripts can
    match on the subcommand as $1."""
    prelude = (
        "for arg do\n"
        "  case $arg in apply|get|logs|delete|create) break;; esac\n"
        "  shift\n"
        "done\n"
        "if [ \"$1\" = \"delete\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"apply\" ]; then echo created; exit 0; fi\n"
    )
    return prelude + textwrap.dedent(script)


def _make_fake_kubectl(tmp_path: Path, script: str) -> KubeJobLauncher:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    kubectl = bin_dir / "kubectl"
    kubectl.write_text("#!/bin/sh\n" + _normalize_script(script))
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
    return KubeJobLauncher(
        namespace="agent-platform",
        kubectl_path=str(kubectl),
        template_dir=Path(__file__).resolve().parents[2] / "deploy" / "jobs",
    )


def _launcher(tmp_path: Path) -> KubeJobLauncher:
    # Default fake: get -> not found; apply -> ok; logs -> success markers.
    return _make_fake_kubectl(
        tmp_path,
        """\
        case "$1 $2" in
          "get job/"*)
            # unknown job -> exit 1 (not found)
            echo "Error from server (NotFound)" >&2; exit 1 ;;
          "apply -f")
            echo "job.batch/plan-x created"; exit 0 ;;
          "logs job/"*)
            echo "PLAN_RESULT_BEGIN"; echo '{}' ; echo "PLAN_RESULT_END" ;;
          *) echo "unexpected: $*" >&2; exit 64 ;;
        esac
        """,
    )


def test_launch_renders_full_manifest(tmp_path):
    captured = {}

    def fake_apply(*args, **kwargs):
        captured["manifest"] = args[3] if len(args) > 3 else ""
        return "created\n"

    launcher = _make_fake_kubectl(
        tmp_path,
        """\
        if [ "$1" = "get" ]; then
          echo '{"status":{"conditions":[{"type":"Complete","status":"True"}]}}'
          exit 0
        fi
        if [ "$1" = "apply" ]; then cat; exit 0; fi
        exit 64
        """,
    )
    original = launcher._kubectl_run

    def spy(*args, **kwargs):
        if args[0] == "apply":
            captured["manifest"] = kwargs.get("input", "")
            return "created"
        return original(*args, **kwargs)

    launcher._kubectl_run = spy  # type: ignore[method-assign]
    handle = launcher.launch(_spec())
    assert handle.job_name == f"plan-{TASK_ID}"
    manifest = captured["manifest"]
    assert "runAsUser: 10001" in manifest
    assert "readOnlyRootFilesystem: true" in manifest
    assert 'drop: ["ALL"]' in manifest
    assert "activeDeadlineSeconds: 900" in manifest
    assert "ttlSecondsAfterFinished: 600" in manifest
    assert "secretKeyRef" in manifest
    assert TASK_ID in manifest


def test_launch_refuses_active_duplicate(tmp_path):
    launcher = _make_fake_kubectl(
        tmp_path,
        """\
        if [ "$1" = "get" ]; then
          echo '{"status":{"conditions":[{"type":"Ready","status":"True"}]}}'
          exit 0
        fi
        exit 64
        """,
    )
    with pytest.raises(AdapterError, match="duplicate launch refused"):
        launcher.launch(_spec())


def test_launch_deletes_finished_then_applies(tmp_path):
    calls: list[list[str]] = []
    original = KubeJobLauncher._kubectl_run

    def spy(self, *args, **kwargs):
        calls.append(list(args))
        if args[0] == "get":
            return json.dumps(
                {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
            )
        if args[0] == "delete":
            return ""
        if args[0] == "apply":
            return "created"
        return ""

    # type ignore: test double replaces the method wholesale
    KubeJobLauncher._kubectl_run = spy  # type: ignore[method-assign]
    try:
        launcher = _make_fake_kubectl(tmp_path, "exit 0")
        launcher.launch(_spec())
    finally:
        KubeJobLauncher._kubectl_run = original  # type: ignore[method-assign]
    verbs = [c[0] for c in calls]
    assert verbs == ["get", "delete", "apply"]


def test_wait_success(tmp_path):
    launcher = _make_fake_kubectl(
        tmp_path,
        """\
        if [ "$1" = "get" ]; then
          echo '{"status":{"conditions":[{"type":"Complete","status":"True"}]}}'
          exit 0
        fi
        exit 64
        """,
    )
    handle = launcher.launch(_spec())
    status = launcher.wait(handle, timeout_seconds=5)
    assert status.phase == JobPhase.SUCCEEDED


def test_wait_failed_reason_from_condition(tmp_path):
    launcher = _make_fake_kubectl(
        tmp_path,
        """\
        if [ "$1" = "get" ]; then
          echo '{"status":{"conditions":[
            {"type":"Failed","status":"True","reason":"DeadlineExceeded"}]}}'
          exit 0
        fi
        exit 64
        """,
    )
    handle = launcher.launch(_spec())
    status = launcher.wait(handle, timeout_seconds=5)
    assert status.phase == JobPhase.FAILED
    assert status.reason == "DeadlineExceeded"


def test_wait_timeout(tmp_path):
    launcher = _make_fake_kubectl(
        tmp_path,
        """\
        if [ "$1" = "get" ]; then echo '{"status":{}}'; exit 0; fi
        exit 64
        """,
    )
    from agent_platform.adapters.kubernetes.models import JobHandle

    handle = JobHandle(job_name="plan-x", namespace="agent-platform")
    with pytest.raises(AdapterTimeoutError):
        launcher.wait(handle, timeout_seconds=1)


def test_collect_extracts_plan_result(tmp_path):
    plan_json = _valid_plan_json()
    launcher = _make_fake_kubectl(
        tmp_path,
        f"""\
        cat <<'LOG'
worker: starting
PLAN_RESULT_BEGIN
{plan_json}
PLAN_RESULT_END
worker: done
LOG
        """,
    )
    raw = launcher.collect(
        type("H", (), {"job_name": "plan-x", "namespace": "agent-poc"})()
    )
    assert json.loads(raw)["task_id"] == TASK_ID


def test_collect_missing_markers_raises(tmp_path):
    launcher = _make_fake_kubectl(tmp_path, 'echo "no markers here"')
    with pytest.raises(AdapterError, match="no PLAN_RESULT_BEGIN"):
        launcher.collect(
            type("H", (), {"job_name": "plan-x", "namespace": "agent-poc"})()
        )


def test_extract_result_unit():
    logs = 'a\nPLAN_RESULT_BEGIN\n{"x": 1}\nPLAN_RESULT_END\nb'
    assert _extract_result(logs) == '{"x": 1}\n'
    assert _extract_result("no markers") is None


def test_remote_adapter_happy_path(tmp_path):
    plan_json = _valid_plan_json()
    launcher = _make_fake_kubectl(
        tmp_path,
        f"""\
        if [ "$1" = "get" ]; then
          echo '{{"status":{{"conditions":[{{"type":"Complete","status":"True"}}]}}}}'
          exit 0
        fi
        if [ "$1" = "apply" ]; then echo created; exit 0; fi
        if [ "$1" = "logs" ]; then
          echo "PLAN_RESULT_BEGIN"
          echo '{plan_json}'
          echo "PLAN_RESULT_END"
          exit 0
        fi
        exit 64
        """,
    )
    from agent_platform.domain.models import PlanJobSpec

    adapter = RemotePlanAdapter(launcher, plan_image="worker-p2:p2")
    job = PlanJobSpec(
        task_id=TASK_ID,
        repository="owner/sandbox",
        issue_number=1,
        base_branch="main",
        base_sha=BASE_SHA,
        issue_title="t",
        issue_body="b",
    )
    plan = adapter.plan(job)
    assert plan.task_id == TASK_ID
    assert plan.plan_hash == ""  # orchestrator recomputes


def test_remote_adapter_rejects_worker_supplied_hash(tmp_path):
    payload = json.loads(_valid_plan_json())
    payload["plan_hash"] = "sha256:" + "0" * 64
    plan_json = json.dumps(payload)
    launcher = _make_fake_kubectl(
        tmp_path,
        f"""\
        if [ "$1" = "get" ]; then
          echo '{{"status":{{"conditions":[{{"type":"Complete","status":"True"}}]}}}}'
          exit 0
        fi
        if [ "$1" = "apply" ]; then echo created; exit 0; fi
        if [ "$1" = "logs" ]; then
          echo "PLAN_RESULT_BEGIN"; echo '{plan_json}'; echo "PLAN_RESULT_END"
          exit 0
        fi
        exit 64
        """,
    )
    from agent_platform.domain.models import PlanJobSpec

    adapter = RemotePlanAdapter(launcher, plan_image="worker-p2:p2")
    job = PlanJobSpec(
        task_id=TASK_ID,
        repository="owner/sandbox",
        issue_number=1,
        base_branch="main",
        base_sha=BASE_SHA,
        issue_title="t",
        issue_body="b",
    )
    plan = adapter.plan(job)
    assert plan.plan_hash == ""  # worker hash dropped, never trusted


def test_remote_adapter_rejects_identity_mismatch(tmp_path):
    payload = json.loads(_valid_plan_json())
    payload["task_id"] = "task-other"
    plan_json = json.dumps(payload)
    launcher = _make_fake_kubectl(
        tmp_path,
        f"""\
        if [ "$1" = "get" ]; then
          echo '{{"status":{{"conditions":[{{"type":"Complete","status":"True"}}]}}}}'
          exit 0
        fi
        if [ "$1" = "apply" ]; then echo created; exit 0; fi
        if [ "$1" = "logs" ]; then
          echo "PLAN_RESULT_BEGIN"; echo '{plan_json}'; echo "PLAN_RESULT_END"
          exit 0
        fi
        exit 64
        """,
    )
    from agent_platform.domain.models import PlanJobSpec

    adapter = RemotePlanAdapter(launcher, plan_image="worker-p2:p2")
    job = PlanJobSpec(
        task_id=TASK_ID,
        repository="owner/sandbox",
        issue_number=1,
        base_branch="main",
        base_sha=BASE_SHA,
        issue_title="t",
        issue_body="b",
    )
    with pytest.raises(AdapterContractError, match="wrong task identity"):
        adapter.plan(job)


def test_remote_adapter_failed_job_raises_with_reason(tmp_path):
    launcher = _make_fake_kubectl(
        tmp_path,
        """\
        if [ "$1" = "get" ]; then
          echo '{"status":{"conditions":[
            {"type":"Failed","status":"True","reason":"BackoffLimitExceeded"}]}}'
          exit 0
        fi
        if [ "$1" = "apply" ]; then echo created; exit 0; fi
        if [ "$1" = "logs" ]; then echo "worker: plan failed: boom"; exit 0; fi
        exit 64
        """,
    )
    from agent_platform.domain.models import PlanJobSpec

    adapter = RemotePlanAdapter(launcher, plan_image="worker-p2:p2")
    job = PlanJobSpec(
        task_id=TASK_ID,
        repository="owner/sandbox",
        issue_number=1,
        base_branch="main",
        base_sha=BASE_SHA,
        issue_title="t",
        issue_body="b",
    )
    with pytest.raises(AdapterError, match="BackoffLimitExceeded"):
        adapter.plan(job)


def test_kubectl_child_env_is_allowlisted(tmp_path, monkeypatch):
    """kubectl gets only what it needs (PATH, KUBECONFIG, in-cluster SA
    discovery, ...); orchestrator credentials stay out of its environment."""
    canary = "CANARY-s3-secret-9b1e"
    monkeypatch.setenv("AGENT_PLATFORM_S3_SECRET_ACCESS_KEY", canary)
    monkeypatch.setenv("KUBECONFIG", "/etc/agent/kubeconfig")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    launcher = _make_fake_kubectl(tmp_path, "env\n")
    out = launcher._kubectl_run("version")
    names = {line.split("=", 1)[0] for line in out.splitlines() if "=" in line}
    assert canary not in out
    assert "AGENT_PLATFORM_S3_SECRET_ACCESS_KEY" not in names
    assert "KUBECONFIG=/etc/agent/kubeconfig" in out.splitlines()
    assert "KUBERNETES_SERVICE_HOST=10.0.0.1" in out.splitlines()
    assert "PATH" in names
