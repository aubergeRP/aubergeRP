# Observability & Operations

The **Admin → Operations** dashboard answers one question: *is this instance
healthy, and if not, where is it broken?* It aggregates data AubergeRP already
keeps — it is not a separate monitoring system.

The dashboard always reports a **fixed 24-hour window**; there is no range
selector. Longer-term usage reporting belongs to **Admin → Statistics** (14
days) and, for real retention, to Prometheus.

## Sections

### System

| Metric | Meaning |
|---|---|
| Uptime | Time since the current process started. Resets on restart. |
| Version | `aubergeRP.__version__`. Shows `dev` when running outside a source checkout. |
| Database | Whether the SQLite file is readable, plus its size on disk. |
| Active conversations | Conversations with at least one message in the last 24 h. |
| Sessions | Rows in `channel_sessions` — one per (transport, bot, external user). |

### Telegram

Per configured bot: name, `@username`, character, state, polling vs webhook,
last update received, last message sent, delivery failures, session count and
the last error.

`Runtime` is derived, not stored:

* **Running** — a live asyncio task is serving the bot.
* **Not running** — the bot is *enabled* but has no live task. It crashed, or
  never started. This is the state to look for when a bot goes quiet.
* **Stopped** — the bot is disabled, which is expected.

For webhook bots the *detail* link queries Telegram live and shows the
registered URL, the pending update count and Telegram's own last error. A
growing pending count means Telegram cannot reach your endpoint.

Tokens and webhook secrets are never returned by the API.

### Sessions

Recent sessions across every transport with transport, bot, character,
message count and last user/assistant activity. External user
identifiers are truncated to their last four characters — enough to correlate
rows, not enough to identify a third party.

### LLM

Aggregates from `llm_call_stats`, split by generation type:

* `chat` — a reply to a user message.
* `proactive` — a scheduled/proactive generation.
* `summarization` — the summarization round-trip itself.

Reported per type: generations, failures, average latency and token counts.
Latency is total wall-clock for the generation, including image generation when
the reply triggered one.

Below the aggregates, **Recent generations** lists the last 50 calls. The
*input/output* link on a row shows what was sent to the model and what came
back — see [Retention](#retention): those bodies are held in memory only, for
the last 50 calls since the process started, and never written to disk.

### Memory & context

Per conversation: estimated context size against the configured limit,
context pressure, when the last summary was produced, and summarization
failures. The detail endpoint also returns the stored summary text.

### Proactive schedules

Every schedule instance with its character, conversation, trigger, enabled
state, next run, last run and last outcome (`sent` / `skipped` / `failed` plus
the reason).

### Recent errors

A concise operational error tail — timestamp, component, a redacted summary and
the related bot/conversation/schedule id. Components: `llm`, `image`,
`summarization`, `telegram_polling`, `telegram_webhook`, `telegram_delivery`,
`scheduler`, `proactive`, `background`.

Image-generation failures are the main reason to look here. The chat UI shows
only a short generic message, because connector errors embed provider URLs and
HTTP response bodies; the redacted cause is recorded under the `image`
component with the conversation id.

This is deliberately **not** a log viewer. For full detail, read the server log.

## Which values are estimates

* **Context sizes are always estimates.** Token counting uses a
  ~4-characters-per-token heuristic (`summarization_service.count_prompt_tokens`)
  so that no tokenizer dependency is required. It is less accurate for
  non-English or code-heavy text.
* **Token counts may be estimates.** AubergeRP asks OpenAI-compatible providers
  for real usage via `stream_options.include_usage`. When the provider reports
  it, the exact counts are stored; otherwise the same heuristic is used and the
  API flags the row with `tokens_estimated`. Set
  `stream_usage: false` on a text connector if your provider rejects the field.
* Uptime, latency, counts and outcomes are exact.

## Diagnosing

### A Telegram bot stopped replying

1. Check its **Runtime** state. *Not running* means the task died — look for a
   `telegram_polling` entry in Recent errors and in the server log.
2. Check **Last update received**. If it is stale but the bot is running,
   Telegram is not delivering updates: for webhook bots open the *webhook* link
   and check the pending count and Telegram's last error (usually TLS or an
   unreachable public URL).
3. If updates arrive but nothing is sent, check **Delivery failures** and the
   `telegram_delivery` errors.

### LLM failures

Filter LLM activity to *Failures only*. Each row carries the connector, model
and a redacted error. A high failure rate with near-zero latency usually means
the connector cannot be reached; long latency followed by failure usually means
a timeout or a token-limit rejection.

### Proactive executions

Find the schedule in the Proactive table. `last_execution_status` tells you what
happened last, and the reason column distinguishes a deliberate `skipped`
(`disabled`, `cooldown`, `contextual_skip`) from a real `failed`
(`generation_failed`, `delivery_failed`, `unexpected_error`).

### Summarization / context pressure

Sort the Memory table by pressure. A conversation over the threshold runs one
summarization round-trip — visible as a `summarization` row in the LLM section —
and then reuses the stored summary until the budget is hit again. A *steady
stream* of `summarization` rows for one conversation means summaries are not
being reused; check the admin Summaries screen for a stale or missing chain.
Repeated `summarization` failures mean summaries are being
dropped and oversized prompts sent through anyway; raise `chat.context_window`
if it understates your model, or lower `chat.summarization_threshold`.

## Retention

There is no observability datastore and nothing extra is written to disk.

* **Durable** — LLM activity (`llm_call_stats`) and per-schedule last outcome
  (`schedule_instances`) live in the normal database and persist across
  restarts.
* **In-memory only** — recent errors, proactive execution history, Telegram
  runtime counters (bounded ring buffers, 200 entries each) and the
  request/response bodies of the last 50 LLM calls (20 000 characters per side,
  credentials redacted). **They are cleared when the server restarts**, and no
  prompt or reply text is ever written to disk.

For real history, enable `/metrics` and let Prometheus own retention.

## Prometheus metrics

Disabled by default. Enable with:

```yaml
observability:
  metrics_enabled: true
```

or `AUBERGE_METRICS_ENABLED=1`. The endpoint is served at `/metrics` (not under
`/api`), returns the Prometheus text format, and is **unauthenticated** — keep
it on a private network or behind a reverse proxy.

Exposed families include `auberge_uptime_seconds`, `auberge_build_info`,
`auberge_database_up`, `auberge_conversations_total`, `auberge_sessions_total`,
`auberge_llm_generations{type,status}`, `auberge_llm_latency_ms_avg{type}`,
`auberge_llm_tokens{direction}`, `auberge_telegram_bot_up{bot,mode}`,
`auberge_telegram_delivery_failures{bot}`, `auberge_schedule_executions{status}`
and `auberge_errors{component}`. No label carries a secret or an external user
identifier.

## API

All endpoints require admin authentication (`X-Admin-Token`):

```
GET /api/observability/overview?hours=24
GET /api/observability/telegram
GET /api/observability/telegram/{bot_id}/webhook
GET /api/observability/sessions?transport=&bot_id=&character_id=
GET /api/observability/llm?hours=&generation_type=&conversation_id=&success=
GET /api/observability/memory
GET /api/observability/memory/{conversation_id}
GET /api/observability/schedules?status=&enabled=&character_id=&transport=
GET /api/observability/errors?component=&hours=
```
