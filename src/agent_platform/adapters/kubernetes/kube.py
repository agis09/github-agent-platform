"""KubeJobLauncher: renders a Job template and drives one Job.

Implementation notes:
- Templates: deploy/jobs/{plan,execute,review}-job.yaml rendered with
  {{PLACEHOLDER}} substitution — no templating dependency.
- Control plane access via the kubectl binary. The launcher subprocesses
  kubectl with fixed arguments (no shell); secret values never appear in
  argv or in the rendered manifest (the API key is a secretKeyRef).
- wait() polls until SUCCEEDED / FAILED / timeout. failure reason is taken
  from the Job's Failed condition (DeadlineExceeded, BackoffLimitExceeded).
- collect() extracts the PlanResult from the pod log markers written by the
  worker entrypoint (completed pods reject exec/cp).
"""

import json
import os
import re
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from agent_platform.adapters.kubernetes.models import (
    JobHandle,
    JobKind,
    JobLaunchSpec,
    JobPhase,
    JobStatus,
)
from agent_platform.config import Settings
from agent_platform.egress import EgressConfig, EgressLevel
from agent_platform.errors import AdapterError, AdapterTimeoutError

_LOG_MARKER_BEGIN = "PLAN_RESULT_BEGIN"
_LOG_MARKER_END = "PLAN_RESULT_END"
_PATCH_BEGIN = "PATCH_BEGIN"
_PATCH_END = "PATCH_END"

_TASK_ID_SAFE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,54}[a-z0-9])?$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Secret holding the LLM API key (key "api-key"); created by the terraform
# module. Override with AGENT_PLATFORM_K8S_LLM_SECRET.
DEFAULT_LLM_SECRET = "llm-api-key"  # noqa: S105 — a Secret object name, not a value

_PREFIX = {JobKind.PLAN: "plan", JobKind.EXECUTE: "execute", JobKind.REVIEW: "review"}

# Placeholders that may render empty; their whole template line is removed.
_OPTIONAL_PLACEHOLDERS = (
    "EXTRA_VOLUMES", "EXTRA_VOLUME_MOUNTS", "EXTRA_INIT_CONTAINERS", "EGRESS_PROXY_ENV",
)


def yaml_dquote(value: str) -> str:
    """Escape a string for a YAML double-quoted scalar (multi-line safe)."""
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
    return "".join(out)


class KubeJobLauncher:
    """JobLauncher implementation for any Kubernetes cluster (kubectl)."""

    # Review payload extras set by RemoteReviewAdapter just before launch;
    # read by _render_review. Typed as a class attr for mypy.
    _review_extras: dict[str, str] | None = None

    def __init__(
        self,
        *,
        namespace: str,
        kubeconfig: str | None = None,
        kubectl_path: str = "kubectl",
        template_dir: Path | str | None = None,
        egress: EgressConfig | None = None,
        search_gateway_url: str = "",
        agent_env: Mapping[str, str] | None = None,
    ) -> None:
        self._namespace = namespace
        # Execute-job egress level and search provider. Both come from the
        # deployment's infra settings, never from the task.
        self._egress = egress or EgressConfig(level=EgressLevel.NONE)
        self._search_gateway_url = search_gateway_url
        self._kubeconfig = kubeconfig
        self._kubectl = kubectl_path
        # Agent runtime settings forwarded to every Job (the orchestrator's
        # own environment by default). Validated here so a bad value fails
        # at launch, not inside the sandbox.
        self._agent_source = dict(os.environ if agent_env is None else agent_env)
        self._agent = Settings.from_environ(self._agent_source)
        self._llm_secret = self._agent_source.get(
            "AGENT_PLATFORM_K8S_LLM_SECRET", DEFAULT_LLM_SECRET
        )
        if not _ENV_NAME.fullmatch(self._agent.agent_api_key_env):
            msg = (
                "AGENT_PLATFORM_AGENT_API_KEY_ENV must be an environment variable "
                f"name, got {self._agent.agent_api_key_env!r}"
            )
            raise AdapterError(msg)
        resolved = (
            Path(template_dir)
            if template_dir
            else Path(__file__).resolve().parents[4] / "deploy" / "jobs"
        )
        for prefix in _PREFIX.values():
            if not (resolved / f"{prefix}-job.yaml").exists():
                msg = f"job template not found: {resolved / f'{prefix}-job.yaml'}"
                raise AdapterError(msg)
        self._template_dir = resolved

    # --- JobLauncher ---

    def launch(self, job: JobLaunchSpec) -> JobHandle:
        job_name = self._job_name(job)
        manifest = self._render(job, job_name)
        # Idempotent relaunch: an existing finished job with the same name is
        # deleted first; a running one is a duplicate-launch conflict.
        self._delete_if_finished(job_name)
        self._kubectl_run("apply", "-f", "-", input=manifest)
        return JobHandle(job_name=job_name, namespace=self._namespace)

    def wait(self, handle: JobHandle, *, timeout_seconds: float) -> JobStatus:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = self._job_status(handle)
            if status.phase in (JobPhase.SUCCEEDED, JobPhase.FAILED):
                return status
            time.sleep(2.0)
        msg = f"job {handle.job_name} not finished within {timeout_seconds}s"
        raise AdapterTimeoutError(msg)

    def collect(self, handle: JobHandle) -> bytes:
        logs = self._kubectl_run("logs", f"job/{handle.job_name}")
        begin, end = (
            (_LOG_MARKER_BEGIN, _LOG_MARKER_END)
            if not handle.job_name.startswith("execute-")
            else ("EXECUTION_RESULT_BEGIN", "EXECUTION_RESULT_END")
        )
        if handle.job_name.startswith("review-"):
            begin, end = "REVIEW_RESULT_BEGIN", "REVIEW_RESULT_END"
        result = _extract_result(logs, begin=begin, end=end)
        if result is None:
            msg = (
                f"job {handle.job_name} produced no {begin}/{end} block in its log"
            )
            raise AdapterError(msg)
        if handle.job_name.startswith("execute-"):
            patch = _extract_result(logs, begin=_PATCH_BEGIN, end=_PATCH_END)
            if patch is not None:
                # Append the patch after a separator line so the JSON parse
                # on the adapter side still sees a pure JSON object first.
                return (result + "\n---PATCH---\n" + patch).encode("utf-8")
        return result.encode("utf-8")

    # --- internals ---

    @staticmethod
    def _job_name(job: JobLaunchSpec) -> str:
        name = f"{_PREFIX[job.kind]}-{job.task_id}"
        if not _TASK_ID_SAFE.fullmatch(name):
            msg = f"derived job name is not k8s-safe: {name!r}"
            raise AdapterError(msg)
        return name

    def _render(self, job: JobLaunchSpec, job_name: str) -> str:
        if job.kind == JobKind.EXECUTE:
            values = self._execute_values(job)
        elif job.kind == JobKind.REVIEW:
            values = self._review_values(job)
        else:
            values = self._plan_values(job)
        values.update(
            NAMESPACE=self._namespace,
            TASK_ID=job.task_id,
            ACTIVE_DEADLINE_SECONDS=str(job.deadline_seconds),
            TTL_SECONDS=str(job.ttl_seconds),
            WORKER_IMAGE=job.plan_image,
            AGENT_ENV=self._agent_env_entries(),
        )
        template = (self._template_dir / f"{_PREFIX[job.kind]}-job.yaml").read_text()
        return _fill(template, values, optional=_OPTIONAL_PLACEHOLDERS)

    def _plan_values(self, job: JobLaunchSpec) -> dict[str, str]:
        for raw in (job.issue_title, job.issue_body):
            if not raw:
                msg = "issue title/body must not be empty for a plan job"
                raise AdapterError(msg)
        return {
            "REPOSITORY": job.repository,
            "ISSUE_NUMBER": str(job.issue_number),
            "BASE_BRANCH": job.base_branch,
            "BASE_SHA": job.base_sha,
            # Env values are rendered inside double-quoted YAML scalars;
            # multi-line issue bodies survive verbatim.
            "ISSUE_TITLE": yaml_dquote(job.issue_title),
            "ISSUE_BODY": yaml_dquote(job.issue_body),
        }

    def _execute_values(self, job: JobLaunchSpec) -> dict[str, str]:
        source = self._agent_source
        values = {
            "REPOSITORY": job.repository,
            "PLAN_HASH": job.plan_hash,
            "BASE_BRANCH": job.base_branch,
            "BASE_SHA": job.base_sha,
            "ALLOWED_PATHS": yaml_dquote("\n".join(job.allowed_paths)),
            "TEST_COMMANDS": yaml_dquote("\n".join(job.test_commands)),
            "SEARCH_FORBIDDEN": yaml_dquote(
                json.dumps(job.search_forbidden_tokens, separators=(",", ":"))
            ),
            "COMMAND_TIMEOUT": source.get("AGENT_PLATFORM_EXECUTE_COMMAND_TIMEOUT", "120"),
            "MAX_OUTPUT": source.get("AGENT_PLATFORM_EXECUTE_MAX_OUTPUT", str(1024 * 1024)),
            "EGRESS_LEVEL": self._egress.level.value,
            # Search is opt-in per deployment: without a gateway it is disabled.
            "SEARCH_PROVIDER": "gateway" if self._search_gateway_url else "none",
            "SEARCH_GATEWAY_URL": self._search_gateway_url,
            "EGRESS_PROXY_ENV": self._egress_proxy_env(),
            "PROMPT": yaml_dquote(job.prompt) if job.prompt else "",
            "S3_PUT_RESULT_URL": self._s3_presigned_put(
                task_id=job.task_id, plan_hash=job.plan_hash,
                artifact="execution_result.json",
            ),
            "S3_PUT_PATCH_URL": self._s3_presigned_put(
                task_id=job.task_id, plan_hash=job.plan_hash, artifact="patch.diff",
            ),
            "EXTRA_VOLUMES": "",
            "EXTRA_VOLUME_MOUNTS": "",
            "EXTRA_INIT_CONTAINERS": "",
        }
        # Snapshot: when a git-bundle ConfigMap is given, an init container
        # unpacks it into /workspace/repo (git needs a writable .git, hence
        # the workspace emptyDir rather than a ConfigMap mount).
        if job.snapshot_configmap:
            values["EXTRA_VOLUMES"] = (
                "- name: repo-snapshot\n"
                "          configMap:\n"
                f"            name: {job.snapshot_configmap}"
            )
            values["EXTRA_VOLUME_MOUNTS"] = (
                "- name: repo-snapshot\n"
                "              mountPath: /snapshot\n"
                "              readOnly: true"
            )
            values["EXTRA_INIT_CONTAINERS"] = (
                "- name: unpack-snapshot\n"
                f"          image: {job.plan_image}\n"
                "          command:\n"
                "            - /bin/sh\n"
                "            - -ec\n"
                "            - git clone /snapshot/snapshot.bundle /workspace/repo"
                f" && cd /workspace/repo && git checkout {job.base_sha}\n"
                "          securityContext:\n"
                "            allowPrivilegeEscalation: false\n"
                "            readOnlyRootFilesystem: true\n"
                "            capabilities:\n"
                '              drop: ["ALL"]\n'
                "          volumeMounts:\n"
                "            - name: workspace\n"
                "              mountPath: /workspace\n"
                "            - name: repo-snapshot\n"
                "              mountPath: /snapshot\n"
                "              readOnly: true"
            )
        return values

    def _review_values(self, job: JobLaunchSpec) -> dict[str, str]:
        extras = self._review_extras or {}
        return {
            "REPOSITORY": job.repository,
            "PLAN_HASH": job.plan_hash,
            "BASE_SHA": job.base_sha,
            "HEAD_SHA": extras.get("HEAD_SHA", job.base_sha),
            "ISSUE_TITLE": yaml_dquote(extras.get("ISSUE_TITLE", "")),
            "ISSUE_BODY": yaml_dquote(extras.get("ISSUE_BODY", "")),
            "PLAN_JSON": yaml_dquote(extras.get("PLAN_JSON", "{}")),
            "REVIEW_DIFF": yaml_dquote(extras.get("REVIEW_DIFF", "")),
            "REVIEW_TESTS": yaml_dquote(extras.get("REVIEW_TESTS", "")),
        }

    def _agent_env_entries(self) -> str:
        """Container env entries for the agent runtime: the non-secret
        settings as values, the API key as a secretKeyRef (the value never
        appears in the manifest)."""
        agent = self._agent
        pairs = [
            ("AGENT_PLATFORM_AGENT_RUNTIME", agent.agent_runtime),
            ("AGENT_PLATFORM_AGENT_MODEL", agent.agent_model or ""),
            ("AGENT_PLATFORM_AGENT_BASE_URL", agent.agent_base_url or ""),
            ("AGENT_PLATFORM_AGENT_API_KEY_ENV", agent.agent_api_key_env),
            ("AGENT_PLATFORM_AGENT_TIMEOUT_SECONDS", f"{agent.agent_timeout_seconds:g}"),
        ]
        if agent.agent_auth_scheme != "api-key":
            pairs.append(("AGENT_PLATFORM_AGENT_AUTH_SCHEME", agent.agent_auth_scheme))
        if agent.agent_max_output_tokens is not None:
            pairs.append(
                ("AGENT_PLATFORM_AGENT_MAX_OUTPUT_TOKENS", str(agent.agent_max_output_tokens))
            )
        indent = " " * 12
        lines = [
            f'- name: {name}\n{indent}  value: "{yaml_dquote(value)}"'
            for name, value in pairs
        ]
        lines.append(
            f"- name: {agent.agent_api_key_env}\n"
            f"{indent}  valueFrom:\n"
            f"{indent}    secretKeyRef:\n"
            f"{indent}      name: {self._llm_secret}\n"
            f"{indent}      key: api-key"
        )
        return f"\n{indent}".join(lines)

    def _egress_proxy_env(self) -> str:
        """HTTPS_PROXY/NO_PROXY env entries for the allowlist level, or ""
        (the template line is then removed). The model endpoint stays
        direct: its NetworkPolicy already allows it and the proxy ACL does
        not."""
        if self._egress.level is not EgressLevel.ALLOWLIST:
            return ""
        no_proxy = [self._agent.llm_host, "localhost", "127.0.0.1", ".svc", ".cluster.local"]
        pairs = [
            ("HTTPS_PROXY", self._egress.proxy_url),
            ("https_proxy", self._egress.proxy_url),
            ("HTTP_PROXY", self._egress.proxy_url),
            ("http_proxy", self._egress.proxy_url),
            ("NO_PROXY", ",".join(no_proxy)),
            ("no_proxy", ",".join(no_proxy)),
        ]
        indent = " " * 12
        lines = [
            f'- name: {name}\n{indent}  value: "{yaml_dquote(value)}"'
            for name, value in pairs
        ]
        return f"\n{indent}".join(lines)

    @staticmethod
    def _s3_presigned_put(*, task_id: str, plan_hash: str, artifact: str) -> str:
        """Presigned PUT URL for one worker artifact. Empty when S3
        is not configured (offline/dev) — the worker falls back to log
        markers. Short-lived and scoped to a single object key."""
        endpoint = os.environ.get("AGENT_PLATFORM_S3_ENDPOINT", "")
        bucket = os.environ.get("AGENT_PLATFORM_S3_BUCKET", "")
        if not endpoint or not bucket:
            return ""
        from agent_platform.adapters.artifacts.s3 import S3Config, presign_put_url

        config = S3Config(
            endpoint_url=endpoint,
            bucket=bucket,
            access_key_id=os.environ["AGENT_PLATFORM_S3_ACCESS_KEY_ID"],
            secret_access_key=os.environ["AGENT_PLATFORM_S3_SECRET_ACCESS_KEY"],
            region=os.environ.get("AGENT_PLATFORM_S3_REGION", "us-east-1"),
        )
        plan_hex = plan_hash.removeprefix("sha256:")
        key = f"artifacts/{task_id}/{plan_hex}/{artifact}"
        return presign_put_url(config=config, key=key, expires_seconds=1800)

    def _delete_if_finished(self, job_name: str) -> None:
        raw = self._kubectl_run(
            "get", f"job/{job_name}", "-o", "json", check=False
        )
        if not raw.strip():
            return
        data = json.loads(raw)
        conditions = data.get("status", {}).get("conditions") or []
        finished = any(
            c.get("status") == "True" and c.get("type") in ("Complete", "Failed")
            for c in conditions
        )
        if finished:
            self._kubectl_run(
                "delete", f"job/{job_name}", "--ignore-not-found"
            )
            return
        msg = (
            f"job {job_name} is still active; duplicate launch refused "
            "(same task must not run concurrently)"
        )
        raise AdapterError(msg)

    def _job_status(self, handle: JobHandle) -> JobStatus:
        raw = self._kubectl_run("get", f"job/{handle.job_name}", "-o", "json")
        data = json.loads(raw)
        conditions = data.get("status", {}).get("conditions") or []
        for cond in conditions:
            if cond.get("status") != "True":
                continue
            if cond.get("type") == "Complete":
                return JobStatus(phase=JobPhase.SUCCEEDED)
            if cond.get("type") == "Failed":
                reason = str(cond.get("reason", ""))
                tail = ""
                if reason != "DeadlineExceeded":
                    # Worker stderr carries the adapter's typed error.
                    tail = self._kubectl_run(
                        "logs", f"job/{handle.job_name}", check=False
                    )[-800:]
                return JobStatus(phase=JobPhase.FAILED, reason=reason, log_tail=tail)
        return JobStatus(phase=JobPhase.RUNNING)

    def _kubectl_run(
        self, *args: str, check: bool = True, input: str | None = None
    ) -> str:
        cmd = [self._kubectl]
        if self._kubeconfig:
            cmd += ["--kubeconfig", self._kubeconfig]
        cmd += ["--namespace", self._namespace, *args]
        try:
            proc = subprocess.run(  # noqa: S603 — fixed kubectl path, no shell
                cmd,
                input=input,
                capture_output=True,
                text=True,
                check=False,
                env=_kubectl_env(os.environ),
                timeout=120,
            )
        except FileNotFoundError as exc:
            msg = f"kubectl not found: {self._kubectl}"
            raise AdapterError(msg) from exc
        except subprocess.TimeoutExpired as exc:
            msg = f"kubectl {' '.join(args[:2])} timed out"
            raise AdapterTimeoutError(msg) from exc
        if check and proc.returncode != 0:
            msg = f"kubectl {' '.join(args[:2])} failed: {proc.stderr.strip()[:400]}"
            raise AdapterError(msg)
        return proc.stdout


# Variables kubectl needs: binary/credential-plugin lookup, kubeconfig and
# cache location, in-cluster service account discovery, proxies. Everything
# else (S3 / Publisher / LLM credentials in the orchestrator's environment)
# stays out of the child process.
_KUBECTL_ENV_ALLOWLIST = (
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "KUBECONFIG",
    "KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT",
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "NO_PROXY", "no_proxy",
)


def _kubectl_env(source: Mapping[str, str]) -> dict[str, str]:
    return {k: source[k] for k in _KUBECTL_ENV_ALLOWLIST if k in source}


def _fill(template: str, values: Mapping[str, str], *, optional: tuple[str, ...]) -> str:
    text = template
    for key in optional:
        if not values.get(key):
            text = "\n".join(
                line for line in text.split("\n") if line.strip() != "{{" + key + "}}"
            )
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    leftovers = [
        line for line in text.splitlines() if "{{" in line and not line.lstrip().startswith("#")
    ]
    if leftovers:
        msg = f"unrendered placeholders in job template: {leftovers[:3]}"
        raise AdapterError(msg)
    return text


def _extract_result(logs: str, *, begin: str = _LOG_MARKER_BEGIN,
                    end: str = _LOG_MARKER_END) -> str | None:
    lines = logs.splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines) if line.strip() == begin
        )
        stop = next(
            i for i, line in enumerate(lines) if line.strip() == end
        )
    except StopIteration:
        return None
    if stop <= start:
        return None
    return "\n".join(lines[start + 1 : stop]) + "\n"
