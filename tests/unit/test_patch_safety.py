"""Unit: patch structural safety checks (allowlist, binary, symlink,
submodule, size)."""

import pytest

from agent_platform.publisher.git_ops import PatchRejectedError, check_patch

ALLOW = ["docs/usage.md"]


def _patch(*body: str, new_mode: str = "100644") -> str:
    header = (
        f"diff --git a/docs/usage.md b/docs/usage.md\n"
        f"new file mode {new_mode}\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/docs/usage.md\n"
    )
    return header + "\n".join(body)


def test_clean_patch_passes():
    check_patch(
        _patch("@@ -0,0 +1,2 @@", "+line one", "+line two"),
        changed_files=["docs/usage.md"],
        allowed_paths=ALLOW,
    )


def test_out_of_scope_file_rejected():
    with pytest.raises(PatchRejectedError, match="outside the allowed paths"):
        check_patch(
            "diff --git a/src/x.py b/src/x.py\n+code",
            changed_files=["src/x.py"],
            allowed_paths=ALLOW,
        )


def test_binary_patch_rejected():
    patch = _patch("@@ -0,0 +1 @@", "+x") + "\nGIT binary patch\nliteral 10\n"
    with pytest.raises(PatchRejectedError, match="binary"):
        check_patch(patch, changed_files=["docs/usage.md"], allowed_paths=ALLOW)


def test_symlink_rejected():
    patch = _patch("@@ -0,0 +1 @@", "+/etc/passwd", new_mode="120000")
    with pytest.raises(PatchRejectedError, match="symlinks"):
        check_patch(patch, changed_files=["docs/usage.md"], allowed_paths=ALLOW)


def test_submodule_rejected():
    patch = (
        "diff --git a/vendor/lib b/vendor/lib\n"
        "new file mode 160000\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/vendor/lib\n"
        "@@ -0,0 +1 @@\n"
        "+Subproject commit " + "a" * 40 + "\n"
    )
    with pytest.raises(PatchRejectedError, match="submodule"):
        check_patch(patch, changed_files=["vendor/lib"], allowed_paths=["vendor/lib"])


def test_oversize_rejected():
    patch = _patch(*["+x" * 10] * 10)
    with pytest.raises(PatchRejectedError, match="over the"):
        check_patch(
            patch, changed_files=["docs/usage.md"], allowed_paths=ALLOW,
            max_bytes=64,
        )


def test_empty_changes_rejected():
    with pytest.raises(PatchRejectedError, match="changes no files"):
        check_patch("", changed_files=[], allowed_paths=ALLOW)
