"""Prometheus metrics exposition.

Disabled by default; enable with ``observability.metrics_enabled: true`` in
config.yaml (or ``AUBERGE_METRICS_ENABLED=1``).  The endpoint is served at the
application root — not under ``/api`` — following Prometheus convention, and is
unauthenticated, so it is meant to be reachable only from a private network.

The text is rendered by hand from the same :class:`ObservabilityService` used
by the admin dashboard: no extra dependency, and a single source of truth.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from ..services.observability_service import ObservabilityService, get_registry, uptime_seconds

router = APIRouter(tags=["metrics"])

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class _Renderer:
    """Accumulates Prometheus text-format lines."""

    def __init__(self) -> None:
        self._lines: list[str] = []

    def family(self, name: str, help_text: str, metric_type: str) -> None:
        self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} {metric_type}")

    def sample(self, name: str, value: float, **labels: str) -> None:
        if labels:
            rendered = ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels.items())
            self._lines.append(f"{name}{{{rendered}}} {value}")
        else:
            self._lines.append(f"{name} {value}")

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


def render_metrics(data_dir: str) -> str:
    """Render the full metrics exposition for the installation at *data_dir*."""
    from .. import __version__

    service = ObservabilityService(data_dir=data_dir)
    overview = service.get_overview(hours=24)
    system = overview["system"]
    out = _Renderer()

    out.family("auberge_build_info", "Build information.", "gauge")
    out.sample("auberge_build_info", 1, version=str(__version__))

    out.family("auberge_uptime_seconds", "Seconds since the process started.", "gauge")
    out.sample("auberge_uptime_seconds", round(uptime_seconds(), 1))

    out.family("auberge_database_up", "1 when the database is readable.", "gauge")
    out.sample("auberge_database_up", 1 if system["database_ok"] else 0)

    out.family("auberge_database_size_bytes", "Size of the SQLite database file.", "gauge")
    out.sample("auberge_database_size_bytes", system["database_size_bytes"])

    out.family("auberge_conversations_total", "Number of stored conversations.", "gauge")
    out.sample("auberge_conversations_total", system["conversations"])

    out.family("auberge_messages_total", "Number of stored messages.", "gauge")
    out.sample("auberge_messages_total", system["messages"])

    out.family("auberge_sessions_total", "Number of transport sessions.", "gauge")
    out.sample("auberge_sessions_total", system["sessions"])

    out.family(
        "auberge_conversations_active",
        "Conversations with activity in the last 24 hours.",
        "gauge",
    )
    out.sample("auberge_conversations_active", system["active_conversations"])

    # ── LLM ──────────────────────────────────────────────────────────────
    llm = overview["llm"]
    out.family(
        "auberge_llm_generations",
        "LLM generations in the last 24 hours, by type and status.",
        "gauge",
    )
    for gen_type, stats in llm["by_type"].items():
        out.sample("auberge_llm_generations", stats["succeeded"], type=gen_type, status="success")
        out.sample("auberge_llm_generations", stats["failed"], type=gen_type, status="failure")

    out.family(
        "auberge_llm_latency_ms_avg",
        "Average LLM generation latency over the last 24 hours, by type.",
        "gauge",
    )
    for gen_type, stats in llm["by_type"].items():
        out.sample("auberge_llm_latency_ms_avg", stats["avg_latency_ms"], type=gen_type)

    out.family(
        "auberge_llm_tokens",
        "LLM tokens over the last 24 hours (estimated when the provider does "
        "not report usage).",
        "gauge",
    )
    out.sample("auberge_llm_tokens", llm["tokens_in"], direction="in")
    out.sample("auberge_llm_tokens", llm["tokens_out"], direction="out")

    # ── Telegram ─────────────────────────────────────────────────────────
    bots = service.get_telegram_bots()
    out.family("auberge_telegram_bot_up", "1 when a configured Telegram bot is running.", "gauge")
    for bot in bots:
        out.sample(
            "auberge_telegram_bot_up",
            1 if bot["runtime_state"] == "running" else 0,
            bot=bot["name"],
            mode=bot["update_mode"],
        )

    out.family(
        "auberge_telegram_delivery_failures",
        "Telegram delivery failures observed since process start.",
        "gauge",
    )
    for bot in bots:
        out.sample("auberge_telegram_delivery_failures", bot["delivery_failures"], bot=bot["name"])

    out.family(
        "auberge_telegram_messages_sent",
        "Telegram messages sent since process start.",
        "gauge",
    )
    for bot in bots:
        out.sample("auberge_telegram_messages_sent", bot["messages_sent"], bot=bot["name"])

    # ── Proactive ────────────────────────────────────────────────────────
    proactive = overview["proactive"]
    out.family("auberge_schedules", "Configured proactive schedule instances.", "gauge")
    out.sample("auberge_schedules", proactive["enabled"], state="enabled")
    out.sample("auberge_schedules", proactive["disabled"], state="disabled")

    out.family(
        "auberge_schedule_executions",
        "Proactive executions observed since process start, by outcome.",
        "gauge",
    )
    for status, count in proactive["execution_history"].items():
        out.sample("auberge_schedule_executions", count, status=status)

    # ── Memory ───────────────────────────────────────────────────────────
    memory = overview["memory"]
    out.family(
        "auberge_summaries_generated",
        "Successful summarizations in the last 24 hours.",
        "gauge",
    )
    out.sample("auberge_summaries_generated", memory["summaries_generated"])
    out.family(
        "auberge_summarization_failures",
        "Failed summarizations in the last 24 hours.",
        "gauge",
    )
    out.sample("auberge_summarization_failures", memory["summarization_failures"])

    # ── Errors ───────────────────────────────────────────────────────────
    out.family(
        "auberge_errors",
        "Operational errors currently held in the in-memory buffer, by component.",
        "gauge",
    )
    for component, count in get_registry().error_counts().items():
        out.sample("auberge_errors", count, component=component)

    return out.render()


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    from ..config import get_config

    config = get_config()
    if not config.observability.metrics_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    return Response(content=render_metrics(config.app.data_dir), media_type=CONTENT_TYPE)
