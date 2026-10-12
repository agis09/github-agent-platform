"""JobLauncher protocol.

Launch boundary for one-shot Kubernetes Jobs. The launcher owns Job
lifecycle; results come back through the ArtifactStore — the launcher never
carries plan payloads itself.
"""

from typing import Protocol

from agent_platform.adapters.kubernetes.models import (
    JobHandle,
    JobLaunchSpec,
    JobStatus,
)


class JobLauncher(Protocol):
    def launch(self, job: JobLaunchSpec) -> JobHandle: ...

    def wait(self, handle: JobHandle, *, timeout_seconds: float) -> JobStatus: ...

    def collect(self, handle: JobHandle) -> bytes:
        """Return the completed job's result payload (PlanResult JSON)."""
