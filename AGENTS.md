# AGENTS.md — Context for AI coding agents

## Project at a glance

aubergeRP is a **self-hostable roleplay engine** built with:

- **Backend:** Python 3.12 + FastAPI + SQLModel (SQLite)
- **Frontend:** Vanilla HTML/JS/CSS — no framework, no build step
- **Connectors:** pluggable modules for text (LLM) and image generation
- **Transports:** Web (REST + SSE) and Telegram bots — both consume the same
  transport-agnostic `ChatService.generate_reply()` pipeline
- **Proactive engine:** character-card schedules + an LLM decision step that can
  make a character message the user first (`ProactiveScheduler`)

Architecture docs are in `docs/`, read in numeric order. Spec files are ground truth for intended behaviour.

## Instructions

- Don't assume. Surface confusion and tradeoffs.
- Minimum code that solves the problem. Nothing speculative.
- Touch only what you must. Don't refactor unrelated code.
- Success = tests pass + lint clean. Don't declare done until verified.
- Add unit tests for new features and bug fixes.
- Add E2E tests for heavy user-facing features (`make test-e2e`).
- Do not create Markdown files without asking first.

## Running tests

```bash
pip install -r requirements-dev.txt          # once for local development

make test                                    # full test suite
make test tests/test_api_chat.py             # single file

make test-e2e                                # browser tests (requires node + playwright)
make lint                                    # ruff check + mypy
```

Tests use `pytest-asyncio` + `respx` for mocking httpx calls. Fixtures create
a temp-dir SQLite DB — see `tests/conftest.py`.

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

## post-actions

After your job is done, make sure `make lint` returns no error or fix them.
Also, always run `make doc` to update the documentation. 
