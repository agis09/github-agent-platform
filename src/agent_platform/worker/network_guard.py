"""Wait until the sandbox's NetworkPolicy is in force before the agent runs.

Some CNIs (k3s's embedded kube-router among them) program a new pod's
policy chains a moment AFTER its containers start; measured on k3s, egress
was unrestricted for about one second. The workers call wait_for_egress_policy()
before starting the agent, so the agent never runs inside that window.

Probe: the Kubernetes API Service (KUBERNETES_SERVICE_HOST:PORT, injected
into every pod even without a service account token). The baseline policy
never allows agent Jobs to reach it at any egress level, so a successful
TCP connect means "not enforced yet". The probe only opens and closes a
TCP connection; nothing is sent.

Fail-closed: if the probe is still reachable after the timeout, the worker
stops without running the agent. Outside Kubernetes (no
KUBERNETES_SERVICE_HOST, e.g. local runs and tests) the guard is a no-op.

Env:
  AGENT_PLATFORM_NETWORK_GUARD_SECONDS  timeout (default 30; 0 disables)
"""

import os
import socket
import time
from collections.abc import Callable, Mapping

from agent_platform.errors import AgentPlatformError

DEFAULT_TIMEOUT_SECONDS = 30.0


class EgressPolicyNotEnforcedError(AgentPlatformError):
    """The sandbox egress policy did not take effect in time."""


def _reachable(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_egress_policy(
    env: Mapping[str, str] | None = None,
    *,
    probe: Callable[[str, int, float], bool] = _reachable,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Block until the probe target is unreachable. Returns a one-line
    status for the worker log; raises EgressPolicyNotEnforcedError on
    timeout."""
    source = os.environ if env is None else env
    host = source.get("KUBERNETES_SERVICE_HOST", "")
    if not host:
        return "network guard: skipped (not running in Kubernetes)"
    raw_timeout = source.get("AGENT_PLATFORM_NETWORK_GUARD_SECONDS", "")
    try:
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
    except ValueError as exc:
        msg = f"AGENT_PLATFORM_NETWORK_GUARD_SECONDS must be a number, got {raw_timeout!r}"
        raise EgressPolicyNotEnforcedError(msg) from exc
    if timeout <= 0:
        return "network guard: disabled (AGENT_PLATFORM_NETWORK_GUARD_SECONDS=0)"
    port = int(source.get("KUBERNETES_SERVICE_PORT", "443"))

    started = clock()
    while True:
        if not probe(host, port, 1.0):
            return f"network guard: egress policy in force after {clock() - started:.1f}s"
        if clock() - started >= timeout:
            msg = (
                f"egress policy not in force after {timeout:.0f}s: the Kubernetes API "
                f"({host}:{port}) is still reachable from the sandbox. Refusing to run "
                "the agent. Check that the cluster's CNI enforces NetworkPolicy."
            )
            raise EgressPolicyNotEnforcedError(msg)
        sleep(0.5)


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "EgressPolicyNotEnforcedError", "wait_for_egress_policy"]
