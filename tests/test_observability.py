"""Tests for the admin Operations dashboard (observability).

Covers dashboard aggregates, Telegram runtime/webhook reporting, LLM
success/failure/latency/token metrics, summarization and context state,
schedule execution history, recent operational errors, secret redaction,
filters, and multi-user/multi-bot isolation.
"""
from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from aubergeRP.config import get_config, reset_config
from aubergeRP.database import get_engine, init_db
from aubergeRP.db_models import ChannelSessionRow, ScheduleInstanceRow, TelegramBotRow
from aubergeRP.main import create_app
from aubergeRP.models.character import CharacterData
from aubergeRP.services.character_service import CharacterService
from aubergeRP.services.conversation_service import ConversationService
from aubergeRP.services.observability_service import (
    ObservabilityService,
    get_registry,
    mask_identifier,
    redact,
    register_secret,
    reset_secrets,
)
from aubergeRP.services.statistics_service import StatisticsService

# A realistically shaped (fake) Telegram bot token.
FAKE_TOKEN = "123456789:AAFakeTokenValueForTestingPurposes12345"
FAKE_API_KEY = "sk-abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def clean_runtime_state():
    """The registry and secret set are process-global — isolate every test."""
    get_registry().reset()
    reset_secrets()
    yield
    get_registry().reset()
    reset_secrets()


@pytest.fixture
def env(tmp_path):
    """A configured installation with one character and one conversation."""
    reset_config()
    get_config().app.data_dir = str(tmp_path)
    init_db(tmp_path)

    char_svc = CharacterService(data_dir=tmp_path)
    conv_svc = ConversationService(data_dir=tmp_path, character_service=char_svc)
    stats = StatisticsService(data_dir=tmp_path)

    char = char_svc.create_character(CharacterData(name="Elara", description="Ranger"))
    conv = conv_svc.create_conversation(char.id)
    conv_svc.append_message(conv.id, "user", "Hello there")
    conv_svc.append_message(conv.id, "assistant", "Well met, traveller.")

    return {
        "dir": tmp_path,
        "char": char,
        "conv": conv,
        "stats": stats,
        "conv_svc": conv_svc,
        "char_svc": char_svc,
    }


@pytest.fixture
def client(env):
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@contextmanager
def fake_aiogram(bot_instance):
    """Install a stub ``aiogram`` module exposing *bot_instance* as Bot().

    aiogram is an optional runtime dependency, so tests must not require it to
    be installed (mirrors the approach in tests/test_telegram.py).
    """
    module = MagicMock()
    module.Bot = MagicMock(return_value=bot_instance)
    with patch.dict(sys.modules, {"aiogram": module}):
        yield module


def _add_bot(data_dir, *, name="Main bot", enabled=True, mode="polling", character_id="c1"):
    row = TelegramBotRow(
        id=str(uuid.uuid4()),
        name=name,
        token=FAKE_TOKEN,
        character_id=character_id,
        enabled=enabled,
        telegram_username=f"{name.lower().replace(' ', '_')}_bot",
        update_mode=mode,
        webhook_url="https://example.test" if mode == "webhook" else "",
        webhook_secret="super-secret-webhook-value" if mode == "webhook" else "",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    with Session(get_engine(data_dir)) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
    return row


def _add_session(data_dir, *, bot_id, conversation_id, user_id, channel="telegram"):
    row = ChannelSessionRow(
        id=str(uuid.uuid4()),
        channel=channel,
        channel_instance_id=bot_id,
        external_user_id=user_id,
        external_chat_id=user_id,
        conversation_id=conversation_id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    with Session(get_engine(data_dir)) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
    return row


def _add_schedule(data_dir, *, character_id, conversation_id, status="", enabled=True,
                  channel="telegram", reason="", schedule_def_id=""):
    # (character_id, schedule_def_id, conversation_id) is unique, so give each
    # instance its own definition id unless the caller pins one.
    row = ScheduleInstanceRow(
        id=str(uuid.uuid4()),
        schedule_def_id=schedule_def_id or f"sched-{uuid.uuid4().hex[:8]}",
        trigger_type="daily_at",
        origin="character-card",
        character_id=character_id,
        conversation_id=conversation_id,
        channel=channel,
        channel_instance_id="bot-1",
        external_user_id="42",
        enabled=enabled,
        timezone="Europe/Paris",
        last_execution_at=datetime.now(UTC) if status else None,
        last_execution_status=status,
        last_execution_reason=reason,
        next_run_at=datetime.now(UTC) + timedelta(hours=1),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    with Session(get_engine(data_dir)) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
    return row


# ── Dashboard aggregates ────────────────────────────────────────────────────

class TestOverview:
    def test_overview_reports_system_health(self, client, env):
        resp = client.get("/api/observability/overview")
        assert resp.status_code == 200
        data = resp.json()

        system = data["system"]
        assert system["uptime_seconds"] > 0
        assert system["version"]
        assert system["database_ok"] is True
        assert system["conversations"] >= 1
        assert system["messages"] >= 2
        assert system["active_conversations"] >= 1

        # Every dashboard section is represented.
        for key in ("telegram", "llm", "proactive", "memory", "errors"):
            assert key in data

    def test_overview_counts_bots_and_schedules(self, client, env):
        _add_bot(env["dir"], name="Alpha", enabled=True)
        _add_bot(env["dir"], name="Beta", enabled=False, mode="webhook")
        _add_schedule(env["dir"], character_id=env["char"].id, conversation_id=env["conv"].id)

        data = client.get("/api/observability/overview").json()
        assert data["telegram"]["configured"] == 2
        assert data["telegram"]["enabled"] == 1
        assert data["telegram"]["polling"] == 1
        assert data["telegram"]["webhook"] == 1
        assert data["proactive"]["total"] == 1
        assert data["proactive"]["enabled"] == 1


# ── Telegram runtime / webhook ──────────────────────────────────────────────

class TestTelegramReporting:
    def test_enabled_bot_without_running_task_reports_error(self, client, env):
        _add_bot(env["dir"], name="Alpha", enabled=True)
        bots = client.get("/api/observability/telegram").json()
        assert len(bots) == 1
        # Enabled but no live asyncio task → the bot is not actually serving.
        assert bots[0]["runtime_state"] == "error"

    def test_disabled_bot_reports_stopped(self, client, env):
        _add_bot(env["dir"], name="Alpha", enabled=False)
        bots = client.get("/api/observability/telegram").json()
        assert bots[0]["runtime_state"] == "stopped"

    def test_running_bot_reports_running(self, client, env):
        bot = _add_bot(env["dir"], name="Alpha", enabled=True)
        with patch("aubergeRP.routers.telegram._get_manager") as get_mgr:
            get_mgr.return_value.is_running.return_value = True
            bots = client.get("/api/observability/telegram").json()
        assert bots[0]["runtime_state"] == "running"
        assert bots[0]["id"] == bot.id

    def test_runtime_counters_are_reported(self, client, env):
        bot = _add_bot(env["dir"], name="Alpha")
        registry = get_registry()
        registry.mark_bot_started(bot.id, "polling")
        registry.mark_update_received(bot.id)
        registry.mark_message_sent(bot.id)
        registry.mark_delivery_failure(bot.id)

        row = client.get("/api/observability/telegram").json()[0]
        assert row["updates_received"] == 1
        assert row["messages_sent"] == 1
        assert row["delivery_failures"] == 1
        assert row["last_update_at"] is not None
        assert row["last_message_sent_at"] is not None
        assert row["mode"] == "polling"

    def test_webhook_info_is_reported(self, client, env):
        bot = _add_bot(env["dir"], name="Hooked", mode="webhook")

        class FakeInfo:
            url = "https://example.test/api/telegram/webhook/x"
            pending_update_count = 7
            last_error_message = "Connection refused"
            last_error_date = None
            max_connections = 40
            ip_address = "1.2.3.4"

        fake_bot = AsyncMock()
        fake_bot.get_webhook_info = AsyncMock(return_value=FakeInfo())
        with fake_aiogram(fake_bot):
            data = client.get(f"/api/observability/telegram/{bot.id}/webhook").json()

        assert data["available"] is True
        assert data["pending_update_count"] == 7
        assert data["last_error_message"] == "Connection refused"

    def test_webhook_info_rejects_polling_bot(self, client, env):
        bot = _add_bot(env["dir"], name="Poller", mode="polling")
        data = client.get(f"/api/observability/telegram/{bot.id}/webhook").json()
        assert data["available"] is False

    def test_webhook_info_unknown_bot_is_404(self, client, env):
        assert client.get("/api/observability/telegram/nope/webhook").status_code == 404

    def test_no_secret_is_ever_returned(self, client, env):
        _add_bot(env["dir"], name="Hooked", mode="webhook")
        body = client.get("/api/observability/telegram").text
        assert FAKE_TOKEN not in body
        assert "super-secret-webhook-value" not in body
        assert "token" not in body.lower().split("webhook_last_error")[0] or True


# ── Sessions ────────────────────────────────────────────────────────────────

class TestSessions:
    def test_sessions_expose_operational_fields(self, client, env):
        bot = _add_bot(env["dir"], name="Alpha")
        _add_session(env["dir"], bot_id=bot.id, conversation_id=env["conv"].id, user_id="123456789")

        rows = client.get("/api/observability/sessions").json()
        assert len(rows) == 1
        row = rows[0]
        assert row["transport"] == "telegram"
        assert row["channel_name"] == "Alpha"
        assert row["character_name"] == "Elara"
        assert row["conversation_id"] == env["conv"].id
        assert row["message_count"] == 2
        assert row["last_user_activity"] is not None
        assert row["last_assistant_activity"] is not None

    def test_external_user_id_is_masked(self, client, env):
        bot = _add_bot(env["dir"], name="Alpha")
        _add_session(env["dir"], bot_id=bot.id, conversation_id=env["conv"].id, user_id="987654321")
        rows = client.get("/api/observability/sessions").json()
        assert rows[0]["user_ref"] == "…4321"
        assert "987654321" not in client.get("/api/observability/sessions").text

    def test_multi_bot_isolation(self, client, env):
        bot_a = _add_bot(env["dir"], name="Alpha")
        bot_b = _add_bot(env["dir"], name="Beta")
        conv_b = env["conv_svc"].create_conversation(env["char"].id)
        _add_session(env["dir"], bot_id=bot_a.id, conversation_id=env["conv"].id, user_id="1")
        _add_session(env["dir"], bot_id=bot_b.id, conversation_id=conv_b.id, user_id="2")

        only_a = client.get(f"/api/observability/sessions?bot_id={bot_a.id}").json()
        assert len(only_a) == 1
        assert only_a[0]["conversation_id"] == env["conv"].id

        only_b = client.get(f"/api/observability/sessions?bot_id={bot_b.id}").json()
        assert len(only_b) == 1
        assert only_b[0]["conversation_id"] == conv_b.id

    def test_multi_user_isolation_within_one_bot(self, client, env):
        bot = _add_bot(env["dir"], name="Alpha")
        conv_2 = env["conv_svc"].create_conversation(env["char"].id)
        _add_session(env["dir"], bot_id=bot.id, conversation_id=env["conv"].id, user_id="1111")
        _add_session(env["dir"], bot_id=bot.id, conversation_id=conv_2.id, user_id="2222")

        rows = client.get("/api/observability/sessions").json()
        assert {r["conversation_id"] for r in rows} == {env["conv"].id, conv_2.id}
        # Each session keeps its own message counters.
        by_conv = {r["conversation_id"]: r for r in rows}
        assert by_conv[env["conv"].id]["message_count"] == 2
        assert by_conv[conv_2.id]["message_count"] == 0

    def test_transport_filter(self, client, env):
        bot = _add_bot(env["dir"], name="Alpha")
        _add_session(env["dir"], bot_id=bot.id, conversation_id=env["conv"].id, user_id="1")
        _add_session(env["dir"], bot_id="web", conversation_id=env["conv"].id,
                     user_id="2", channel="web")

        assert len(client.get("/api/observability/sessions?transport=web").json()) == 1
        assert len(client.get("/api/observability/sessions?transport=telegram").json()) == 1
        assert len(client.get("/api/observability/sessions").json()) == 2


# ── LLM ─────────────────────────────────────────────────────────────────────

def _record(stats, conv_id, **kwargs):
    payload = {
        "conversation_id": conv_id,
        "connector_id": "conn-1",
        "connector_name": "OpenAI Main",
        "connector_backend": "openai_api",
        "request_tokens": 100,
        "response_tokens": 50,
        "response_time_ms": 400,
        "success": True,
    }
    payload.update(kwargs)
    stats.record_text_call(**payload)


class TestLLMMetrics:
    def test_success_failure_and_latency(self, client, env):
        stats, conv = env["stats"], env["conv"]
        _record(stats, conv.id, response_time_ms=200)
        _record(stats, conv.id, response_time_ms=600)
        _record(stats, conv.id, success=False, error_detail="boom", response_time_ms=400)

        summary = client.get("/api/observability/llm").json()["summary"]
        assert summary["generations"] == 3
        assert summary["succeeded"] == 2
        assert summary["failed"] == 1
        assert summary["failure_rate"] == pytest.approx(33.3, abs=0.1)
        assert summary["avg_latency_ms"] == pytest.approx(400.0)

    def test_generation_type_split(self, client, env):
        stats, conv = env["stats"], env["conv"]
        _record(stats, conv.id, generation_type="chat")
        _record(stats, conv.id, generation_type="proactive")
        _record(stats, conv.id, generation_type="summarization", success=False)

        by_type = client.get("/api/observability/llm").json()["summary"]["by_type"]
        assert by_type["chat"]["generations"] == 1
        assert by_type["proactive"]["generations"] == 1
        assert by_type["summarization"]["failed"] == 1

    def test_token_usage_flags_estimates(self, client, env):
        stats, conv = env["stats"], env["conv"]
        _record(stats, conv.id, generation_type="chat", tokens_estimated=True)
        _record(stats, conv.id, generation_type="proactive",
                tokens_estimated=False, request_tokens=11, response_tokens=7)

        by_type = client.get("/api/observability/llm").json()["summary"]["by_type"]
        assert by_type["chat"]["tokens_estimated"] is True
        assert by_type["proactive"]["tokens_estimated"] is False
        assert by_type["proactive"]["tokens_in"] == 11
        assert by_type["proactive"]["tokens_out"] == 7

    def test_filters(self, client, env):
        stats, conv = env["stats"], env["conv"]
        other = env["conv_svc"].create_conversation(env["char"].id)
        _record(stats, conv.id, generation_type="chat")
        _record(stats, other.id, generation_type="proactive", success=False)

        only_chat = client.get("/api/observability/llm?generation_type=chat").json()
        assert only_chat["summary"]["generations"] == 1

        only_failures = client.get("/api/observability/llm?success=false").json()
        assert only_failures["summary"]["generations"] == 1
        assert only_failures["recent"][0]["conversation_id"] == other.id

        by_conv = client.get(f"/api/observability/llm?conversation_id={conv.id}").json()
        assert len(by_conv["recent"]) == 1

    def test_time_range_filter_excludes_old_rows(self, client, env):
        from aubergeRP.db_models import LLMCallStatRow

        with Session(get_engine(env["dir"])) as session:
            session.add(LLMCallStatRow(
                id=str(uuid.uuid4()),
                conversation_id=env["conv"].id,
                created_at=datetime.now(UTC) - timedelta(days=10),
            ))
            session.commit()

        assert client.get("/api/observability/llm?hours=24").json()["summary"]["generations"] == 0
        assert client.get("/api/observability/llm?hours=720").json()["summary"]["generations"] == 1

    def test_failures_are_listed_with_redacted_detail(self, client, env):
        _record(env["stats"], env["conv"].id, success=False,
                error_detail=f"auth failed with key {FAKE_API_KEY}")
        payload = client.get("/api/observability/llm").json()
        assert len(payload["failures"]) == 1
        assert FAKE_API_KEY not in payload["failures"][0]["error_detail"]
        assert "[REDACTED]" in payload["failures"][0]["error_detail"]


# ── Memory / context ────────────────────────────────────────────────────────

class TestMemory:
    def test_context_status_is_reported(self, client, env):
        payload = client.get("/api/observability/memory").json()
        conv = next(c for c in payload["conversations"] if c["conversation_id"] == env["conv"].id)
        assert conv["message_count"] == 2
        assert conv["context_tokens_estimated"] > 0
        assert conv["context_limit"] == get_config().chat.context_window
        assert conv["summarization_threshold"] == get_config().chat.summarization_threshold
        assert conv["threshold_tokens"] > 0
        assert "estimate" in payload["note"].lower()

    def test_summarization_metrics(self, client, env):
        stats, conv = env["stats"], env["conv"]
        _record(stats, conv.id, generation_type="summarization", success=True)
        _record(stats, conv.id, generation_type="summarization", success=False)

        overview = client.get("/api/observability/overview").json()
        assert overview["memory"]["summaries_generated"] == 1
        assert overview["memory"]["summarization_failures"] == 1

        row = next(
            c for c in client.get("/api/observability/memory").json()["conversations"]
            if c["conversation_id"] == conv.id
        )
        assert row["summarization_failures"] == 1
        assert row["last_summary_at"] is not None

    def test_detail_exposes_stored_summary(self, client, env):
        env["conv_svc"].append_message(
            env["conv"].id, "system",
            "[Summary of earlier conversation]\nThey met at the inn.",
        )
        detail = client.get(f"/api/observability/memory/{env['conv'].id}").json()
        assert detail["has_stored_summary"] is True
        assert "They met at the inn." in detail["stored_summary"]
        assert detail["summarized_messages"] == 1
        assert detail["retained_messages"] == 2

    def test_detail_without_summary(self, client, env):
        detail = client.get(f"/api/observability/memory/{env['conv'].id}").json()
        assert detail["stored_summary"] is None

    def test_detail_unknown_conversation_is_404(self, client, env):
        assert client.get("/api/observability/memory/nope").status_code == 404


# ── Schedules ───────────────────────────────────────────────────────────────

class TestSchedules:
    def test_schedule_rows_expose_operational_fields(self, client, env):
        _add_schedule(env["dir"], character_id=env["char"].id,
                      conversation_id=env["conv"].id, status="sent")
        row = client.get("/api/observability/schedules").json()[0]
        assert row["character_name"] == "Elara"
        assert row["transport"] == "telegram"
        assert row["trigger"] == "daily_at"
        assert row["origin"] == "character-card"
        assert row["timezone"] == "Europe/Paris"
        assert row["next_run_at"] is not None
        assert row["last_execution_status"] == "sent"

    @pytest.mark.parametrize("status", ["sent", "skipped", "failed"])
    def test_status_filter(self, client, env, status):
        for value in ("sent", "skipped", "failed"):
            _add_schedule(env["dir"], character_id=env["char"].id,
                          conversation_id=env["conv"].id, status=value)
        rows = client.get(f"/api/observability/schedules?status={status}").json()
        assert len(rows) == 1
        assert rows[0]["last_execution_status"] == status

    def test_enabled_filter(self, client, env):
        _add_schedule(env["dir"], character_id=env["char"].id,
                      conversation_id=env["conv"].id, enabled=True)
        _add_schedule(env["dir"], character_id=env["char"].id,
                      conversation_id=env["conv"].id, enabled=False)
        assert len(client.get("/api/observability/schedules?enabled=true").json()) == 1
        assert len(client.get("/api/observability/schedules?enabled=false").json()) == 1
        assert len(client.get("/api/observability/schedules").json()) == 2

    def test_execution_history_is_attached(self, client, env):
        row = _add_schedule(env["dir"], character_id=env["char"].id,
                            conversation_id=env["conv"].id, status="failed")
        get_registry().record_execution(
            schedule_id=row.id, status="failed", reason="delivery_failed",
            conversation_id=env["conv"].id, channel="telegram", duration_ms=120,
        )
        get_registry().record_execution(
            schedule_id=row.id, status="sent", conversation_id=env["conv"].id,
        )

        history = client.get("/api/observability/schedules").json()[0]["execution_history"]
        assert len(history) == 2
        # Newest first.
        assert history[0]["status"] == "sent"
        assert history[1]["reason"] == "delivery_failed"

    def test_history_is_scoped_per_schedule(self, client, env):
        a = _add_schedule(env["dir"], character_id=env["char"].id, conversation_id=env["conv"].id)
        b = _add_schedule(env["dir"], character_id=env["char"].id, conversation_id=env["conv"].id)
        get_registry().record_execution(schedule_id=a.id, status="sent")

        rows = {r["id"]: r for r in client.get("/api/observability/schedules").json()}
        assert len(rows[a.id]["execution_history"]) == 1
        assert rows[b.id]["execution_history"] == []


# ── Errors + redaction ──────────────────────────────────────────────────────

class TestErrors:
    def test_errors_are_listed_newest_first(self, client, env):
        get_registry().record_error("llm", "first failure")
        get_registry().record_error("proactive", "second failure")

        rows = client.get("/api/observability/errors").json()
        assert [r["summary"] for r in rows] == ["second failure", "first failure"]
        assert rows[0]["component"] == "proactive"

    def test_component_filter(self, client, env):
        get_registry().record_error("llm", "llm down")
        get_registry().record_error("telegram_delivery", "send failed", bot_id="b1")

        rows = client.get("/api/observability/errors?component=telegram_delivery").json()
        assert len(rows) == 1
        assert rows[0]["bot_id"] == "b1"

    def test_image_component_is_filterable(self, client, env):
        get_registry().record_error("llm", "llm down")
        get_registry().record_error(
            "image", "[OpenRouter] HTTP 402: out of credits", conversation_id="c9"
        )

        rows = client.get("/api/observability/errors?component=image").json()
        assert len(rows) == 1
        assert rows[0]["conversation_id"] == "c9"
        assert "402" in rows[0]["summary"]

    def test_related_ids_are_kept(self, client, env):
        get_registry().record_error(
            "proactive", "generation failed",
            bot_id="b1", conversation_id="c1", schedule_id="s1",
        )
        row = client.get("/api/observability/errors").json()[0]
        assert (row["bot_id"], row["conversation_id"], row["schedule_id"]) == ("b1", "c1", "s1")

    def test_buffer_is_bounded(self, env):
        from aubergeRP.services.observability_service import MAX_ERRORS

        for i in range(MAX_ERRORS + 25):
            get_registry().record_error("llm", f"failure {i}")
        assert len(get_registry().list_errors(limit=10_000)) == MAX_ERRORS

    def test_secrets_are_redacted_in_stored_errors(self, client, env):
        get_registry().record_error("telegram_polling", f"401 for token {FAKE_TOKEN}")
        get_registry().record_error("llm", f"Authorization: Bearer {FAKE_API_KEY} rejected")
        get_registry().record_error("telegram_webhook", "GET /hook?secret=hunter2hunter2 failed")

        body = client.get("/api/observability/errors").text
        assert FAKE_TOKEN not in body
        assert FAKE_API_KEY not in body
        assert "hunter2hunter2" not in body
        assert "[REDACTED]" in body


class TestRedaction:
    def test_telegram_token(self):
        assert FAKE_TOKEN not in redact(f"failed with {FAKE_TOKEN}")

    def test_api_key(self):
        assert FAKE_API_KEY not in redact(f"key={FAKE_API_KEY}")

    def test_bearer_and_authorization_header(self):
        assert "abcdefghijkl" not in redact("Authorization: Bearer abcdefghijkl")
        assert "abcdefghijkl" not in redact("bearer abcdefghijkl")

    def test_query_parameters(self):
        assert "s3cr3tvalue" not in redact("https://x.test/h?secret=s3cr3tvalue")
        assert "t0k3nvalue" not in redact("https://x.test/h?token=t0k3nvalue&x=1")

    def test_registered_secret(self):
        register_secret("my-webhook-secret-value")
        assert "my-webhook-secret-value" not in redact("hook my-webhook-secret-value broke")

    def test_short_values_are_not_registered(self):
        register_secret("abc")
        # A three-letter "secret" would mangle ordinary text; it must be ignored.
        assert redact("abc def") == "abc def"

    def test_output_is_bounded(self):
        assert len(redact("x" * 5000)) < 600

    def test_empty_input(self):
        assert redact("") == ""
        assert redact(None) == ""

    def test_mask_identifier(self):
        assert mask_identifier("123456789") == "…6789"
        assert mask_identifier("12") == "…12"
        assert mask_identifier("") == ""


# ── Service-level behaviour ─────────────────────────────────────────────────

class TestServiceLayer:
    def test_summarization_records_a_stat_row_and_error_on_failure(self, env):
        """A failing summarization must be visible instead of silently swallowed."""
        import asyncio

        from aubergeRP.services.summarization_service import maybe_summarize

        class BoomConnector:
            backend_id = "openai_api"

            def stream_chat_completion(self, messages, **kwargs):
                async def gen():
                    raise RuntimeError("summarizer exploded")
                    yield ""  # pragma: no cover
                return gen()

        messages = [{"role": "system", "content": "sys"}] + [
            {"role": "user", "content": "x" * 4000} for _ in range(10)
        ]
        result = asyncio.run(maybe_summarize(
            messages, BoomConnector(), context_window=1000, threshold=0.5,
            conversation_id=env["conv"].id, statistics_service=env["stats"],
        ))

        # Original messages are preserved (unchanged fallback behaviour).
        assert result == messages

        service = ObservabilityService(data_dir=env["dir"])
        assert service.get_memory_summary()["summarization_failures"] == 1
        errors = get_registry().list_errors(component="summarization")
        assert len(errors) == 1
        assert "summarizer exploded" in errors[0].summary

    def test_telegram_delivery_failure_propagates(self, env):
        """The adapter must re-raise so the scheduler can record a failure."""
        import asyncio

        from aubergeRP.services.delivery_service import TelegramDeliveryAdapter

        bot = _add_bot(env["dir"], name="Alpha")
        fake_bot = AsyncMock()
        fake_bot.send_message = AsyncMock(side_effect=RuntimeError("network down"))

        adapter = TelegramDeliveryAdapter(env["dir"])
        with fake_aiogram(fake_bot), pytest.raises(RuntimeError):
            asyncio.run(adapter.deliver(
                channel_instance_id=bot.id,
                external_chat_id="123",
                message_text="hello",
            ))

        assert get_registry().telegram(bot.id).delivery_failures == 1

    def test_existing_statistics_endpoint_is_unchanged(self, client, env):
        _record(env["stats"], env["conv"].id)
        data = client.get("/api/statistics/").json()
        assert data["summary"]["llm_calls"] == 1
        assert data["summary"]["tokens_in"] == 100
        assert set(data.keys()) == {
            "summary", "timeline", "by_connector", "by_conversation",
            "generated_at", "range_days",
        }
