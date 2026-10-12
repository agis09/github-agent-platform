"""The workers' wait for the sandbox egress policy (worker/network_guard.py)."""

import pytest

from agent_platform.worker.network_guard import (
    EgressPolicyNotEnforcedError,
    wait_for_egress_policy,
)

K8S = {"KUBERNETES_SERVICE_HOST": "10.43.0.1", "KUBERNETES_SERVICE_PORT": "443"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_noop_outside_kubernetes():
    calls = []
    status = wait_for_egress_policy({}, probe=lambda *a: calls.append(a) or True)
    assert "skipped" in status
    assert calls == []


def test_returns_at_once_when_already_blocked():
    clock = FakeClock()
    status = wait_for_egress_policy(K8S, probe=lambda *a: False, sleep=clock.sleep, clock=clock)
    assert "in force after 0.0s" in status


def test_waits_until_the_policy_takes_effect():
    # Reachable for the first ~1s after start (the k3s window), then blocked.
    clock = FakeClock()
    seen = []

    def probe(host, port, timeout):
        seen.append((host, port))
        return clock.now < 1.0

    status = wait_for_egress_policy(K8S, probe=probe, sleep=clock.sleep, clock=clock)
    assert "in force after 1.0s" in status
    assert seen[0] == ("10.43.0.1", 443)
    assert len(seen) == 3


def test_fails_closed_when_never_enforced():
    clock = FakeClock()
    env = {**K8S, "AGENT_PLATFORM_NETWORK_GUARD_SECONDS": "5"}
    with pytest.raises(EgressPolicyNotEnforcedError, match="Refusing to run the agent"):
        wait_for_egress_policy(env, probe=lambda *a: True, sleep=clock.sleep, clock=clock)
    assert clock.now >= 5


def test_zero_disables():
    status = wait_for_egress_policy(
        {**K8S, "AGENT_PLATFORM_NETWORK_GUARD_SECONDS": "0"}, probe=lambda *a: True
    )
    assert "disabled" in status


def test_bad_timeout_fails_loudly():
    with pytest.raises(EgressPolicyNotEnforcedError, match="must be a number"):
        wait_for_egress_policy({**K8S, "AGENT_PLATFORM_NETWORK_GUARD_SECONDS": "soon"})
