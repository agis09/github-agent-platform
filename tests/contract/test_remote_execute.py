"""Contract: RemoteExecuteAdapter + KubeJobLauncher execute path (fake
kubectl, offline). Mirrors test_remote_plan.py for the execute side."""

import json
import stat
import textwrap
from pathlib import Path

import pytest

from agent_platform.adapters.agent.remote_execute import RemoteExecuteAdapter
from agent_platform.adapters.kubernetes.kube import KubeJobLauncher
from agent_platform.adapters.kubernetes.models import JobKind, JobLaunchSpec
from agent_platform.domain.models import ExecuteJobSpec
from agent_platform.errors import AdapterContractError, AdapterError

BASE_SHA = "c" * 40
PLAN_HASH = "sha256:" + "d" * 64
TASK_ID = "task-abc123456789"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _job_spec(**overrides: object) -> ExecuteJobSpec:
    values = {
        "schema_version": "1",
        "task_id": TASK_ID,
        "repository": "owner/sandbox",
        "plan_hash": PLAN_HASH,
        "base_branch": "main",
        "base_sha": BASE_SHA,
        "allowed_paths": ["docs/usage.md"],
        "test_commands": ["pytest -q"],
        "plan_ref": "artifact://" + "d" * 64,
    }
    values.update(overrides)
    return ExecuteJobSpec(**values)  # type: ignore[arg-type]


def _result_json() -> str:
    return json.dumps(
        {
            "schema_version": "1",
            "task_id": TASK_ID,
            "plan_hash": PLAN_HASH,
            "base_sha": BASE_SHA,
            "status": "succeeded",
            "changed_files": ["docs/usage.md"],
            "commands": [],
            "tests": [],
            "violations": [],
            "pr_body_draft": "draft",
        }
    )


def _make_launcher(tmp_path: Path, script: str) -> KubeJobLauncher:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    kubectl = bin_dir / "kubectl"
    prelude = (
        "for arg do\n"
        "  case $arg in apply|get|logs|delete|create) break;; esac\n"
        "  shift\n"
        "done\n"
        'if [ "$1" = "delete" ]; then exit 0; fi\n'
        'if [ "$1" = "apply" ]; then echo created; exit 0; fi\n'
    )
    kubectl.write_text("#!/bin/sh\n" + prelude + textwrap.dedent(script))
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
    return KubeJobLauncher(
        namespace="agent-platform",
        kubectl_path=str(kubectl),
        template_dir=REPO_ROOT / "deploy" / "jobs",
    )


def test_execute_adapter_happy_path(tmp_path):
    launcher = _make_launcher(
        tmp_path,
        f"""\\
        if [ "$1" = "get" ]; then
          echo '{{"status":{{"conditions":[{{"type":"Complete","status":"True"}}]}}}}'
          exit 0
        fi
        if [ "$1" = "logs" ]; then
          echo "EXECUTION_RESULT_BEGIN"
          echo '{_result_json()}'
          echo "EXECUTION_RESULT_END"
          exit 0
        fi
        exit 64
        """,
    )
    adapter = RemoteExecuteAdapter(launcher, plan_image="worker-p2:p2")
    result = adapter.execute(_job_spec())
    assert result.status == "succeeded"
    assert result.changed_files == ["docs/usage.md"]


def test_execute_adapter_failed_job_raises(tmp_path):
    launcher = _make_launcher(
        tmp_path,
        """\\
        if [ "$1" = "get" ]; then
          echo '{"status":{"conditions":[
            {"type":"Failed","status":"True","reason":"BackoffLimitExceeded"}]}}'
          exit 0
        fi
        if [ "$1" = "logs" ]; then echo "runner: boom"; exit 0; fi
        exit 64
        """,
    )
    adapter = RemoteExecuteAdapter(launcher, plan_image="worker-p2:p2")
    with pytest.raises(AdapterError, match="BackoffLimitExceeded"):
        adapter.execute(_job_spec())


def test_execute_adapter_identity_mismatch_rejected(tmp_path):
    payload = json.loads(_result_json())
    payload["task_id"] = "task-other"
    launcher = _make_launcher(
        tmp_path,
        f"""\\
        if [ "$1" = "get" ]; then
          echo '{{"status":{{"conditions":[{{"type":"Complete","status":"True"}}]}}}}'
          exit 0
        fi
        if [ "$1" = "logs" ]; then
          echo "EXECUTION_RESULT_BEGIN"; echo '{json.dumps(payload)}'; echo "EXECUTION_RESULT_END"
          exit 0
        fi
        exit 64
        """,
    )
    adapter = RemoteExecuteAdapter(launcher, plan_image="worker-p2:p2")
    with pytest.raises(AdapterContractError, match="wrong identity"):
        adapter.execute(_job_spec())


def test_execute_adapter_invalid_json_rejected(tmp_path):
    launcher = _make_launcher(
        tmp_path,
        """\\
        if [ "$1" = "get" ]; then
          echo '{"status":{"conditions":[{"type":"Complete","status":"True"}]}}'
          exit 0
        fi
        if [ "$1" = "logs" ]; then
          echo "EXECUTION_RESULT_BEGIN"; echo "not json"; echo "EXECUTION_RESULT_END"
          exit 0
        fi
        exit 64
        """,
    )
    adapter = RemoteExecuteAdapter(launcher, plan_image="worker-p2:p2")
    with pytest.raises(AdapterContractError, match="invalid JSON"):
        adapter.execute(_job_spec())


def test_execute_launcher_renders_execute_template(tmp_path):
    captured: dict[str, str] = {}
    launcher = _make_launcher(tmp_path, "exit 0")
    original = launcher._kubectl_run

    def spy(*args, **kwargs):
        if args[0] == "apply":
            captured["manifest"] = kwargs.get("input", "")
            return "created"
        return original(*args, **kwargs)

    launcher._kubectl_run = spy  # type: ignore[method-assign]
    launcher.launch(
        JobLaunchSpec(
            task_id=TASK_ID,
            repository="owner/sandbox",
            issue_number=1,
            base_branch="main",
            base_sha=BASE_SHA,
            issue_title="execute:" + PLAN_HASH,
            issue_body="allowed=docs/usage.md",
            plan_image="worker-p2:p2",
            plan_hash=PLAN_HASH,
            allowed_paths=["docs/usage.md"],
            test_commands=["pytest -q"],
            kind=JobKind.EXECUTE,
        )
    )
    manifest = captured["manifest"]
    assert "agent_platform.worker.execute" in manifest
    assert "EXEC_PLAN_HASH" in manifest
    assert PLAN_HASH in manifest
    assert "runAsUser: 10001" in manifest
    assert 'drop: ["ALL"]' in manifest


# --- egress level and search provider in the execute Job ---

_PROXY = "http://egress-proxy.agent-platform.svc.cluster.local:4750"


def _render_execute(tmp_path, monkeypatch, *, egress=None, search_gateway_url="",
                    agent_base_url="https://llm.example.invalid/v1") -> str:
    launcher = KubeJobLauncher(
        namespace="agent-platform",
        kubectl_path=str(tmp_path / "unused-kubectl"),
        template_dir=REPO_ROOT / "deploy" / "jobs",
        egress=egress,
        search_gateway_url=search_gateway_url,
        agent_env={"AGENT_PLATFORM_AGENT_BASE_URL": agent_base_url},
    )
    return launcher._render(
        JobLaunchSpec(
            task_id=TASK_ID, repository="owner/sandbox", issue_number=1,
            base_branch="main", base_sha=BASE_SHA, issue_title="t", issue_body="b",
            plan_image="worker-p2:p2", plan_hash=PLAN_HASH,
            allowed_paths=["docs/usage.md"], kind=JobKind.EXECUTE,
        ),
        f"execute-{TASK_ID}",
    )


def _unrendered(manifest: str) -> list[str]:
    return [line for line in manifest.splitlines()
            if "{{" in line and not line.lstrip().startswith("#")]


def _allowlist() -> "object":
    from agent_platform.egress import EgressConfig, EgressLevel

    return EgressConfig(level=EgressLevel.ALLOWLIST, proxy_url=_PROXY,
                        allowed_hosts=("pypi.org", "files.pythonhosted.org"))


def test_execute_default_is_egress_none_without_proxy_env(tmp_path, monkeypatch):
    manifest = _render_execute(tmp_path, monkeypatch)
    assert 'agent-platform.egress: "none"' in manifest
    assert "HTTPS_PROXY" not in manifest
    assert not _unrendered(manifest)
    # Search is disabled without a gateway (opt-in per deployment).
    assert 'agent-platform.search: "none"' in manifest
    assert '- name: SEARCH_PROVIDER\n              value: "none"' in manifest


def test_execute_allowlist_routes_through_the_proxy(tmp_path, monkeypatch):
    manifest = _render_execute(tmp_path, monkeypatch, egress=_allowlist())
    assert 'agent-platform.egress: "allowlist"' in manifest
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        assert f'            - name: {name}\n              value: "{_PROXY}"' in manifest
    # The model endpoint (and in-cluster services) bypass the proxy.
    no_proxy = "llm.example.invalid,localhost,127.0.0.1,.svc,.cluster.local"
    assert f'            - name: NO_PROXY\n              value: "{no_proxy}"' in manifest
    assert not _unrendered(manifest)


def test_execute_full_has_no_proxy_env(tmp_path, monkeypatch):
    from agent_platform.egress import EgressConfig, EgressLevel

    manifest = _render_execute(tmp_path, monkeypatch,
                               egress=EgressConfig(level=EgressLevel.FULL))
    assert 'agent-platform.egress: "full"' in manifest
    assert "HTTPS_PROXY" not in manifest


def test_execute_allowlist_without_base_url_bypasses_the_vendor_api(tmp_path, monkeypatch):
    """Without a custom endpoint the runtime's vendor API host must bypass
    the proxy (the proxy ACL does not list it)."""
    manifest = _render_execute(tmp_path, monkeypatch, egress=_allowlist(), agent_base_url="")
    assert 'value: "api.openai.com,localhost,' in manifest


def test_execute_search_gateway_selects_the_gateway_provider(tmp_path, monkeypatch):
    url = "http://search-gateway.agent-platform.svc.cluster.local:8080"
    manifest = _render_execute(tmp_path, monkeypatch, search_gateway_url=url)
    assert 'agent-platform.search: "gateway"' in manifest
    assert '- name: SEARCH_PROVIDER\n              value: "gateway"' in manifest
    assert f'- name: SEARCH_GATEWAY_URL\n              value: "{url}"' in manifest


def test_agent_env_forwards_runtime_settings_and_key_by_reference(tmp_path):
    launcher = KubeJobLauncher(
        namespace="agent-platform",
        kubectl_path=str(tmp_path / "unused-kubectl"),
        template_dir=REPO_ROOT / "deploy" / "jobs",
        agent_env={
            "AGENT_PLATFORM_AGENT_RUNTIME": "claude-code",
            "AGENT_PLATFORM_AGENT_BASE_URL": "https://api.ai.sakura.ad.jp",
            "AGENT_PLATFORM_AGENT_MODEL": "gpt-oss-120b",
            "AGENT_PLATFORM_AGENT_AUTH_SCHEME": "bearer",
            "LLM_API_KEY": "sk-should-never-render",
        },
    )
    manifest = launcher._render(
        JobLaunchSpec(
            task_id=TASK_ID, repository="owner/sandbox", issue_number=1,
            base_branch="main", base_sha=BASE_SHA, issue_title="t", issue_body="b",
            plan_image="worker-p2:p2", kind=JobKind.PLAN,
        ),
        f"plan-{TASK_ID}",
    )
    assert '- name: AGENT_PLATFORM_AGENT_RUNTIME\n              value: "claude-code"' in manifest
    assert '- name: AGENT_PLATFORM_AGENT_AUTH_SCHEME\n              value: "bearer"' in manifest
    assert "name: llm-api-key" in manifest
    assert "sk-should-never-render" not in manifest
    assert "namespace: agent-platform" in manifest
