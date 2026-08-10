# Technology Stack

## Backend

| Component | Choice |
|---|---|
| Language | Python 3.12+ |
| Framework | FastAPI |
| Database | SQLite via SQLModel |
| Async HTTP client | httpx |
| SSE | sse-starlette |
| Image metadata | Pillow |
| Config | PyYAML |
| ComfyUI integration | websockets |
| Telegram bots | aiogram 3 |
| Timezones | `zoneinfo` (stdlib) |
| Error tracking | sentry-sdk (optional) |

## Frontend

- Vanilla HTML + JavaScript — no framework, no build step.
- `marked.js` (vendored) for Markdown rendering.
- All JS/CSS vendored locally — no CDN loads.

## Dev tools

| Tool | Purpose |
|---|---|
| `pytest` + `pytest-asyncio` | Testing |
| `ruff` | Linting and formatting |
| `mypy` | Static type checking |
| `respx` | Mock httpx in tests |

## Makefile targets

| Target | Description |
|---|---|
| `make run` | Dev server with hot-reload |
| `make test` | Run test suite |
| `make lint` | ruff + mypy |
| `make lint-fix` | Auto-fix lint issues |
| `make doc` | Regenerate `docs/03-backend-api.md` |
| `make docker` | Start app-only Docker stack |
| `make docker gpu=rtx3090` | Start local GPU stack |
| `make stop` / `make clean` / `make logs` | Docker management (`gpu=...` optional) |
