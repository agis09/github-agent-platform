"""Log redaction: patterns, env scanning, formatter integration with args
expansion and exception text."""

import logging

from agent_platform.redaction import (
    RedactingFormatter,
    SecretRegistry,
    configure_logging,
    redact_text,
)


def test_github_pat_pattern():
    text = "failed to fetch with ghp_ABCDEFGHIJKLMNOPQRSTUV1234 from api"
    assert "[REDACTED]" in redact_text(text)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUV1234" not in redact_text(text)


def test_bearer_pattern():
    text = "Authorization: Bearer abc123.def456-ghi"
    out = redact_text(text)
    assert "abc123.def456-ghi" not in out


def test_private_key_pattern():
    text = "-----BEGIN PRIVATE KEY-----\nMIIEv\n-----END PRIVATE KEY-----\nrest"
    assert "MIIEv" not in redact_text(text)


def test_aws_key_pattern():
    assert "AKIA" not in redact_text("key=AKIAIOSFODNN7EXAMPLE stuff")


def test_key_value_pattern():
    assert "hunter2" not in redact_text("password = hunter2")
    assert "s3cr3t" not in redact_text("api_key:s3cr3tv4lu3")


def test_env_registry_values_redacted():
    registry = SecretRegistry.from_environ(
        {"GITHUB_TOKEN": "ghp_Canary0123456789", "HOME": "/Users/x"}
    )
    assert "[REDACTED]" in registry.redact("token is ghp_Canary0123456789 ok")
    # non-secret env values are not registered
    assert "/Users/x" in registry.redact("home is /Users/x ok")


def test_registry_redacts_unknown_shape_via_registration():
    registry = SecretRegistry()
    registry.register("MyOrg-CustomToken-12345678")
    assert "MyOrg-CustomToken-12345678" not in registry.redact("leak MyOrg-CustomToken-12345678")


def test_short_values_not_registered():
    registry = SecretRegistry.from_environ({"SECRET": "abc"})
    assert "abc" in registry.redact("abc kept")  # too short to be a secret


def test_longer_registered_value_redacted_first():
    registry = SecretRegistry()
    registry.register("short-token")
    registry.register("prefix-short-token-suffix")
    out = registry.redact("value prefix-short-token-suffix end")
    assert "prefix-short-token-suffix" not in out


def test_formatter_redacts_args_expansion():
    registry = SecretRegistry()
    registry.register("ghp_Canary0123456789")
    formatter = RedactingFormatter(registry, fmt="%(levelname)s: %(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="calling with token %s",
        args=("ghp_Canary0123456789",),
        exc_info=None,
    )
    assert "ghp_Canary0123456789" not in formatter.format(record)


def test_formatter_redacts_exception_text():
    registry = SecretRegistry()
    registry.register("sk-CanaryCanaryCanary12")
    formatter = RedactingFormatter(registry, fmt="%(levelname)s: %(message)s")
    try:
        raise RuntimeError("bad key sk-CanaryCanaryCanary12")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    assert "sk-CanaryCanaryCanary12" not in formatter.format(record)


def test_configure_logging_wires_handler():
    import io

    stream = io.StringIO()
    configure_logging("INFO", env={"GITHUB_TOKEN": "ghp_Canary0123456789"}, stream=stream)
    logging.getLogger("test-redaction").info("token ghp_Canary0123456789 leaked")
    assert "ghp_Canary0123456789" not in stream.getvalue()
    assert "[REDACTED]" in stream.getvalue()
