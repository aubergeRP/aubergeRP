"""Tests for the periodic connector health checks (see scheduler.py)."""
from __future__ import annotations

import json

import pytest

from aubergeRP.config import get_config, reset_config
from aubergeRP.routers.connectors import _TestResultsStore
from aubergeRP.scheduler import Scheduler, check_connectors_health


@pytest.fixture
def config(tmp_path):
    reset_config()
    cfg = get_config()
    cfg.app.data_dir = str(tmp_path)
    return cfg


class _FakeManager:
    def __init__(self, results):
        self._results = results
        self.calls = []

    async def test_connector(self, connector_id):
        self.calls.append(connector_id)
        result = self._results[connector_id]
        if isinstance(result, Exception):
            raise result
        return result


def _patch_manager(monkeypatch, manager):
    import aubergeRP.routers.connectors as connectors_router
    monkeypatch.setattr(connectors_router, "get_connector_manager", lambda: manager)


def test_legacy_bool_entries_still_readable(config, tmp_path):
    (tmp_path / "connector_test_results.json").write_text(
        json.dumps({"legacy": True, "legacy-off": False}), encoding="utf-8"
    )
    store = _TestResultsStore()
    assert store.get("legacy") is True
    assert store.get("legacy-off") is False
    assert store.get_checked_at("legacy") is None


def test_get_does_not_reread_unchanged_file(config, tmp_path, monkeypatch):
    store = _TestResultsStore()
    store.set("abc", True)

    path = tmp_path / "connector_test_results.json"
    reads = []
    original = type(path).read_text

    def counting_read_text(self, *args, **kwargs):
        reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", counting_read_text)
    for _ in range(5):
        assert store.get("abc") is True
    assert reads == [], "unchanged file must not be re-read on every get()"


def test_get_picks_up_external_change(config, tmp_path):
    store = _TestResultsStore()
    store.set("abc", True)

    # Another process rewrites the sidecar file.
    path = tmp_path / "connector_test_results.json"
    path.write_text(
        json.dumps({"abc": {"connected": False, "checked_at": "2026-01-01T00:00:00+00:00"}}),
        encoding="utf-8",
    )
    import os
    os.utime(path, (0, 0))

    assert store.get("abc") is False


@pytest.mark.asyncio
async def test_check_connectors_health_persists_results(config, monkeypatch):
    config.active_connectors.text = "text-1"
    config.active_connectors.image = "image-1"
    manager = _FakeManager({
        "text-1": {"connected": True},
        "image-1": {"connected": False},
    })
    _patch_manager(monkeypatch, manager)

    results = await check_connectors_health(config)

    assert results == {"text-1": True, "image-1": False}
    store = _TestResultsStore()
    assert store.get("text-1") is True
    assert store.get("image-1") is False
    assert store.get_checked_at("text-1") is not None


@pytest.mark.asyncio
async def test_check_connectors_health_records_failure_as_disconnected(config, monkeypatch):
    config.active_connectors.text = "text-1"
    config.active_connectors.image = ""
    _patch_manager(monkeypatch, _FakeManager({"text-1": RuntimeError("boom")}))

    results = await check_connectors_health(config)

    assert results == {"text-1": False}
    assert _TestResultsStore().get("text-1") is False


@pytest.mark.asyncio
async def test_check_connectors_health_drops_deleted_connector(config, monkeypatch):
    config.active_connectors.text = "gone"
    config.active_connectors.image = ""
    store = _TestResultsStore()
    store.set("gone", True)
    _patch_manager(monkeypatch, _FakeManager({"gone": KeyError("gone")}))

    results = await check_connectors_health(config)

    assert results == {}
    assert _TestResultsStore().get("gone") is None


@pytest.mark.asyncio
async def test_scheduler_runs_health_checks_when_cleanup_disabled(config, monkeypatch):
    import asyncio

    config.scheduler.enabled = False
    config.scheduler.health_check_enabled = True
    config.scheduler.health_check_interval_seconds = 1
    config.active_connectors.text = "text-1"
    config.active_connectors.image = ""
    manager = _FakeManager({"text-1": {"connected": True}})
    _patch_manager(monkeypatch, manager)

    scheduler = Scheduler(config)
    scheduler.start()
    try:
        for _ in range(300):
            await asyncio.sleep(0.01)
            if manager.calls:
                break
    finally:
        scheduler.stop()

    assert manager.calls == ["text-1"]


def test_scheduler_health_checks_can_be_disabled(config):
    config.scheduler.enabled = False
    config.scheduler.health_check_enabled = False
    scheduler = Scheduler(config)
    scheduler.start()
    assert scheduler._health_task is None
    scheduler.stop()
