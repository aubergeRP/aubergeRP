# AGENTS.md — Context for AI coding agents

## Project at a glance

aubergeRP is a **self-hostable roleplay engine** built with:

- **Backend:** Python 3.12 + FastAPI + SQLModel (SQLite)
- **Frontend:** Vanilla HTML/JS/CSS — no framework, no build step, no bundler
- **Connectors:** pluggable modules for text (LLM) and image generation
- **Transports:** Web (REST + SSE) and Telegram bots — both consume the same
  transport-agnostic `ChatService.generate_reply()` pipeline
- **Proactive engine:** character-card schedules + an LLM decision step that can
  make a character message the user first (`ProactiveScheduler`)

Architecture docs are in `docs/`, read in numeric order. Spec files are ground truth for intended behaviour.

## Repository layout

```
aubergeRP/
  main.py            FastAPI app factory, startup wiring
  config.py          YAML config loading (config.yaml, see config.example.yaml)
  database.py        engine/session helpers      db_models.py  SQLModel tables
  event_bus.py       in-process pub/sub          scheduler.py  background jobs
  routers/           thin HTTP layer (one file per resource)
  services/          all business logic lives here
  connectors/        base.py + manager.py + one file per backend
  models/            pydantic schemas (API contracts)
  migrations/        numbered mNNN_*.py schema migrations
  prompts/           *.txt — single source of truth for every LLM prompt
frontend/            index.html + js/ css/ admin/ (served statically)
docs/                numbered architecture docs; 03-backend-api.md is generated
tests/               pytest suite            tests/e2e/  node+playwright tests
scripts/             generate_api_docs.py (used by `make doc`)
docker/              compose files + GPU profiles for the LocalAI stack
```

## Instructions

- Don't assume. Surface confusion and tradeoffs.
- Minimum code that solves the problem. Nothing speculative.
- Touch only what you must. Don't refactor unrelated code.
- Success = tests pass + lint clean. Don't declare done until verified.
- Add unit tests for new features and bug fixes.
- Add E2E tests for heavy user-facing features (`make test-e2e`).
- Do not create Markdown files without asking first.
- **Routers are thin** — business logic goes in `services/`.
- **Connectors are isolated** — one file per backend, registered in `connectors/manager.py`.
- **Every DB schema change** needs a new numbered migration in `migrations/`.
- **Atomic file writes** — write-to-temp + `os.rename`.
- **No new dependencies** unless strictly necessary.
- Keep it simple and maintainable: this project is maintained on limited free
  time and must stay manageable for years.
- Conventional Commits for commit messages; branch from `main`.

## Running tests

```bash
pip install -r requirements-dev.txt          # once for local development

make test tests/test_api_chat.py             # single file  ← default choice
make test tests/test_chat_service.py tests/test_api_chat.py   # several files
make test                                    # FULL suite — ~10 minutes
```

**Run only the tests covering what you touched.** `make test tests/<file>.py`
takes seconds; the full `make test` takes about **ten minutes**, so run it only
when the change is broad (touches many modules, startup wiring, migrations, or
shared infrastructure). Mapping is straightforward: a service/router change maps
to `tests/test_<name>.py`.

`make test-e2e` runs the browser tests in `tests/e2e/` (requires node +
playwright); run it only when the change is user-facing in the web UI.

Tests use `pytest-asyncio` + `respx` for mocking httpx calls — no real network
calls. Fixtures create a temp-dir SQLite DB — see `tests/conftest.py`.

## Prompts

Every LLM prompt lives exclusively as a `.txt` file in `aubergeRP/prompts/`.
The `.txt` file is the single source of truth — admin-editable at runtime and
committed to git as the factory default. **Do NOT embed prompt text inside
Python source code.**

Rules:
- When adding a new prompt key, create `aubergeRP/prompts/<key>.txt` with the
  prompt text and add a matching entry in `PROMPT_META` inside
  `prompt_service.py` (label + description only — no prompt text in Python).
- When editing the default text of an existing prompt, update only the `.txt`
  file.
- `PROMPT_DEFAULTS` in `prompt_service.py` is populated automatically from the
  `.txt` files at server start-up; do not edit it manually.

## Other useful commands

```bash
make run          # dev server, hot-reload, http://localhost:8123
make lint         # ruff check + mypy
make lint-fix     # ruff --fix --unsafe-fixes
make doc          # regenerate docs/03-backend-api.md from source
make docker       # app-only container stack (see `make help` for GPU profiles)
```

## post-actions

After your job is done, make sure `make lint` returns no error or fix them.
Also, always run `make doc` to update the documentation.
