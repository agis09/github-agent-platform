"""Patch safety checks: the Publisher re-verifies everything the
orchestrator recorded before any GitHub write.

Checks:
- allowlist: every changed file is within the approval's allowed_paths
- structural: patch must not add submodules, symlinks, or non-files
- binary: patch must not contain binary hunks (GIT binary patch / "Binary
  files ... differ")
- size: patch bytes under the configured limit
"""

import re

from agent_platform.errors import DomainRejectedError

_DIFF_PATH = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)

_DEFAULT_MAX_PATCH_BYTES = 1024 * 1024

_BINARY_MARKER = re.compile(r"^GIT binary patch$", re.MULTILINE)
_BINARY_DIFF = re.compile(r"^Binary files .* differ$", re.MULTILINE)
_SYMLINK_CREATE = re.compile(r"^new file mode 120000$", re.MULTILINE)
_SUBMODULE_ADD = re.compile(r"^\+\Subproject commit [0-9a-f]{40}", re.MULTILINE)
_NEW_FILE_MODE = re.compile(r"^new file mode (\d{6})$", re.MULTILINE)


class PatchRejectedError(DomainRejectedError):
    """The patch violates the publish safety rules."""


def patch_files(patch: str) -> list[str]:
    """Changed paths derived from the patch itself (diff headers) — never
    trusted from the worker's changed_files list."""
    return sorted({m.group(2) for m in _DIFF_PATH.finditer(patch)})


def check_patch(
    patch: str,
    *,
    changed_files: list[str],
    allowed_paths: list[str],
    max_bytes: int = _DEFAULT_MAX_PATCH_BYTES,
) -> None:
    """Raise PatchRejectedError on any violation. No repair, no fallback.

    ``changed_files`` (worker-reported) must agree with the patch headers;
    the allowlist check runs against the PATCH-derived paths.
    """
    derived = patch_files(patch)
    if not derived:
        raise PatchRejectedError("patch changes no files; nothing to publish")
    if sorted(changed_files) != derived:
        msg = (
            f"changed_files disagreement: worker reported {sorted(changed_files)}, "
            f"patch contains {derived}"
        )
        raise PatchRejectedError(msg)
    patch_bytes = patch.encode("utf-8")
    if len(patch_bytes) > max_bytes:
        msg = f"patch is {len(patch_bytes)} bytes, over the {max_bytes} limit"
        raise PatchRejectedError(msg)

    out_of_scope = sorted(
        path
        for path in changed_files
        if not any(
            path == a or path.startswith(a.rstrip("/") + "/")
            for a in allowed_paths
        )
    )
    if out_of_scope:
        msg = f"changed files outside the allowed paths: {out_of_scope}"
        raise PatchRejectedError(msg)

    if _BINARY_MARKER.search(patch) or _BINARY_DIFF.search(patch):
        raise PatchRejectedError("patch contains binary content")
    if _SYMLINK_CREATE.search(patch):
        raise PatchRejectedError("patch creates symlinks")
    if _SUBMODULE_ADD.search(patch):
        raise PatchRejectedError("patch adds submodules")
    for match in _NEW_FILE_MODE.finditer(patch):
        mode = match.group(1)
        if mode not in ("100644", "100755"):
            msg = f"patch creates a file with non-regular mode {mode}"
            raise PatchRejectedError(msg)
