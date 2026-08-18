"""Startup validation of AUBERGE_ADMIN_PASSWORD_HASH."""

import logging

import pytest

from aubergeRP.config import Config
from aubergeRP.main import _init_admin_password, _looks_like_sha256_hash
from aubergeRP.utils.auth import hash_password


@pytest.mark.parametrize(
    "value,expected",
    [
        (hash_password("secret"), True),
        (hash_password("secret").upper(), True),
        ("my-plain-password", False),
        ("a" * 63, False),
        ("z" * 64, False),
        ("", False),
    ],
)
def test_looks_like_sha256_hash(value: str, expected: bool) -> None:
    assert _looks_like_sha256_hash(value) is expected


def test_valid_hash_logs_no_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    digest = hash_password("secret")
    monkeypatch.setenv("AUBERGE_ADMIN_PASSWORD_HASH", digest)
    config = Config()
    with caplog.at_level(logging.WARNING):
        _init_admin_password(config)
    assert config.app.admin_password_hash == digest
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_suspicious_hash_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("AUBERGE_ADMIN_PASSWORD_HASH", "my-plain-password")
    config = Config()
    with caplog.at_level(logging.WARNING):
        _init_admin_password(config)
    # The value is still used as-is — the check only warns.
    assert config.app.admin_password_hash == "my-plain-password"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("does not look like a SHA-256 hash" in m for m in warnings)
