# Project Structure

```
aubergeRP/
├── aubergeRP/               # Python backend package
│   ├── main.py              # FastAPI app, startup
│   ├── config.py            # Config loading (YAML + env overrides)
│   ├── database.py          # SQLite engine + session
│   ├── db_models.py         # SQLModel table definitions
│   ├── event_bus.py         # In-process SSE event bus
│   ├── scheduler.py         # Background media-cleanup scheduler
│   ├── connectors/          # Connector implementations
│   │   ├── base.py          # Abstract base classes
│   │   ├── manager.py       # ConnectorManager
│   │   ├── openai_text.py
│   │   ├── openai_image.py
│   │   └── comfyui.py
│   ├── migrations/          # Numbered SQLite migrations (auto-applied at startup)
│   ├── models/              # Pydantic request/response models
│   ├── prompts/             # Every LLM prompt, one .txt per key (source of truth)
│   ├── routers/             # FastAPI route handlers (thin — delegate to services)
│   │   ├── chat.py, characters.py, conversations.py, connectors.py, …
│   │   ├── telegram.py      # Bot CRUD + webhook endpoint
│   │   ├── timezone.py      # Per-session IANA timezone
│   │   ├── schedules.py     # Runtime schedule instances
│   │   ├── observability.py # Operations dashboard payloads
│   │   └── metrics.py       # Optional Prometheus /metrics
│   ├── services/            # Business logic
│   │   ├── chat_service.py              # RP generation pipeline (transport-agnostic)
│   │   ├── summarization_service.py     # History compression (token counting + LLM call)
│   │   ├── summary_service.py           # Persisted, incremental summaries
│   │   ├── telegram_bot_service.py      # Bot config CRUD
│   │   ├── telegram_runtime_manager.py  # Runs bots (polling / webhook)
│   │   ├── channel_session_service.py   # external user → conversation mapping
│   │   ├── timezone_service.py          # Transport-neutral IANA timezones
│   │   ├── schedule_instance_service.py # Card schedules → runtime instances
│   │   ├── proactive_scheduler_service.py # Proactive behavior engine
│   │   ├── delivery_service.py          # Transport-neutral message delivery
│   │   └── observability_service.py     # Runtime registries + aggregation
│   ├── plugins/             # Plugin system skeleton
│   └── utils/               # Shared helpers
├── frontend/                # Static HTML/JS/CSS
│   ├── index.html           # Chat UI
│   ├── admin/index.html     # Admin UI
│   ├── js/                  # JavaScript modules
│   └── vendor/              # Vendored JS libs (marked.js, …)
├── tests/                   # Pytest test suite
├── docs/                    # Developer documentation (this folder)
├── docker/                  # Docker stack
│   ├── docker-compose.yml
│   └── profiles/            # Hardware profiles (rtx3090.yml, …)
├── config.example.yaml      # Example config — copy to config.yaml
├── Makefile
├── Dockerfile
├── requirements.txt         # Runtime Python dependencies
└── requirements-dev.txt     # Test/lint/dev-only Python dependencies
```

## Architecture rules

1. **Routers are thin.** No business logic — delegate to services.
2. **Services own all logic.** No imports from `routers/`.
3. **Connectors are isolated.** One file per backend. Register in `connectors/manager.py`.
4. **Every schema change needs a migration.** Add `aubergeRP/migrations/m{NNN}_{slug}.py`.

## Adding a connector backend

See [06-connector-system.md](06-connector-system.md) for the step-by-step guide.
