"""Tests for the optional Prometheus /metrics endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aubergeRP.config import get_config, reset_config
from aubergeRP.main import create_app
from aubergeRP.models.character import CharacterData
from aubergeRP.services.character_service import CharacterService
from aubergeRP.services.conversation_service import ConversationService
from aubergeRP.services.observability_service import get_registry, reset_secrets
from aubergeRP.services.statistics_service import StatisticsService

FAKE_API_KEY = "sk-abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def clean_runtime_state():
    get_registry().reset()
    reset_secrets()
    yield
    get_registry().reset()
    reset_secrets()


@pytest.fixture
def env(tmp_path):
    reset_config()
    get_config().app.data_dir = str(tmp_path)

    char_svc = CharacterService(data_dir=tmp_path)
    conv_svc = ConversationService(data_dir=tmp_path, character_service=char_svc)
    stats = StatisticsService(data_dir=tmp_path)
    char = char_svc.create_character(CharacterData(name="Elara", description="Ranger"))
    conv = conv_svc.create_conversation(char.id)
    return {"dir": tmp_path, "conv": conv, "stats": stats}


def test_metrics_disabled_by_default(env):
    assert get_config().observability.metrics_enabled is False
    with TestClient(create_app()) as client:
        assert client.get("/metrics").status_code == 404


def test_metrics_enabled_returns_prometheus_text(env):
    get_config().observability.metrics_enabled = True
    with TestClient(create_app()) as client:
        resp = client.get("/metrics")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text

    for family in (
        "auberge_build_info",
        "auberge_uptime_seconds",
        "auberge_database_up",
        "auberge_conversations_total",
        "auberge_llm_tokens",
        "auberge_schedule_executions",
        "auberge_errors",
    ):
        assert f"# HELP {family} " in body
        assert f"# TYPE {family} " in body


def test_metrics_reflect_recorded_activity(env):
    get_config().observability.metrics_enabled = True
    env["stats"].record_text_call(
        conversation_id=env["conv"].id,
        connector_id="c1",
        connector_name="Main",
        connector_backend="openai_api",
        request_tokens=100,
        response_tokens=40,
        response_time_ms=300,
        success=True,
        generation_type="chat",
    )
    get_registry().record_error("llm", "something broke")
    get_registry().record_execution(schedule_id="s1", status="sent")

    with TestClient(create_app()) as client:
        body = client.get("/metrics").text

    assert 'auberge_llm_generations{type="chat",status="success"} 1' in body
    assert 'auberge_llm_tokens{direction="in"} 100' in body
    assert 'auberge_llm_tokens{direction="out"} 40' in body
    assert 'auberge_schedule_executions{status="sent"} 1' in body
    assert 'auberge_errors{component="llm"} 1' in body


def test_metrics_env_override(env, monkeypatch):
    monkeypatch.setenv("AUBERGE_METRICS_ENABLED", "1")
    reset_config()
    get_config().app.data_dir = str(env["dir"])
    assert get_config().observability.metrics_enabled is True


def test_metrics_never_expose_secrets(env):
    get_config().observability.metrics_enabled = True
    get_registry().record_error("llm", f"Authorization: Bearer {FAKE_API_KEY}")

    with TestClient(create_app()) as client:
        body = client.get("/metrics").text

    # Error *messages* are not exported at all — only per-component counts.
    assert FAKE_API_KEY not in body
    assert "Bearer" not in body
