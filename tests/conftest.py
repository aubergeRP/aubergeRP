import os
import tempfile

import pytest

# Ensure modules importing `aubergeRP.main` at collection time don't try writing
# to a host-level path from local config (e.g. /data).
os.environ.setdefault("AUBERGE_DATA_DIR", tempfile.mkdtemp(prefix="aubergeRP-tests-"))


@pytest.fixture(autouse=True)
def disable_admin_auth_for_tests(monkeypatch, tmp_path, request):
    # Keep config-loading unit tests independent from env overrides.
    if request.node.fspath.basename == "test_config.py":
        monkeypatch.delenv("AUBERGE_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("AUBERGE_DATA_DIR", str(tmp_path / "_appdata"))
    monkeypatch.delenv("AUBERGE_HOST", raising=False)
    monkeypatch.delenv("AUBERGE_PORT", raising=False)
    monkeypatch.delenv("AUBERGE_LOG_LEVEL", raising=False)
    monkeypatch.delenv("AUBERGE_USER_NAME", raising=False)
    monkeypatch.delenv("AUBERGE_SENTRY_DSN", raising=False)
    monkeypatch.delenv("AUBERGE_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("AUBERGE_DISABLE_ADMIN_AUTH", "1")
    yield


@pytest.fixture(autouse=True)
def reset_config_singleton():
    from aubergeRP.config import reset_config
    from aubergeRP.database import reset_engine
    reset_config()
    reset_engine()
    yield
    reset_config()
    reset_engine()


@pytest.fixture(autouse=True)
def no_generation_retry_delays(monkeypatch):
    """Remove the retry backoff waits so tests stay fast.

    The number of attempts is unchanged; only the sleeps are zeroed.
    """
    from aubergeRP.utils import retry
    monkeypatch.setattr(retry, "RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(retry, "RETRY_MAX_DELAY", 0.0)
    yield
