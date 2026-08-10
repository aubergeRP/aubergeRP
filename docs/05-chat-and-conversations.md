# Chat and Conversations

## Message lifecycle

1. `POST /api/chat/{conversation_id}/message` with `{"content": "..."}`.
2. Backend builds the LLM prompt (see Prompt structure below).
3. Active text connector streams tokens → `token` SSE events to the client.
4. If the LLM emits `[IMG: <prompt>]`, the backend:
   - Strips the marker from the forwarded stream.
   - Calls the active image connector.
   - Emits `image_start` → `image_complete` (or `image_failed`) on the same SSE stream.
5. On completion: persists user + assistant messages in SQLite, emits `done`.
6. On fatal error: emits `error`; nothing is saved.

## Internal generation API (transport-agnostic)

`ChatService` also exposes an internal Python API that does not depend on FastAPI or SSE:

```python
result = await chat_service.generate_reply(
    conversation_id=conversation.id,
    content="Hello",
)
print(result.text)
```

This path uses the same RP pipeline as Web/SSE (prompt construction, history,
summarization, connector call, and persistence). The HTTP/SSE router remains a
consumer of the same generation engine.

## Transports

| Transport | Inbound | Outbound | Server-initiated push |
|---|---|---|---|
| Web | `POST /api/chat/{id}/message` | SSE stream | No — proactive messages are persisted and shown on next refresh |
| Telegram | aiogram (long-polling **or** webhook) | Bot API `sendMessage` | Yes |

A Telegram user is mapped to an AubergeRP conversation by
`ChannelSessionService`, keyed on `(channel, channel_instance_id, external_user_id)`
— for Telegram, `channel_instance_id` is the bot id. Bots support the commands
`/start`, `/reset`, `/status` and `/timezone`.

Webhook mode is configured per bot (`update_mode`, `webhook_url`,
`webhook_secret`); the runtime manager registers the webhook with Telegram on
enable and deregisters it on disable. Telegram is told to POST updates to
`{webhook_url}/api/telegram/webhook/{bot_id}`, which
`TelegramRuntimeManager.dispatch_update()` feeds into the same handlers as
polling mode.

That route (`POST /api/telegram/webhook/{bot_id}`) is public — Telegram calls it,
so it is not behind the admin token. Authentication is the
`X-Telegram-Bot-Api-Secret-Token` header, compared against the bot's stored
`webhook_secret` when one is set. The update is dispatched in the background and
the route answers `{"ok": true}` right away, so a slow generation never causes
Telegram to retry the update. Unknown bot → 404, disabled bot → 409, bad secret
→ 403.

See the Telegram section of [03-backend-api.md](03-backend-api.md) for bot CRUD.

## Dialogue-only mode

A Telegram bot can be flagged `dialogue_only`. The `dialogue_only_instruction`
prompt is then appended to the system prompt, and the character replies with
only what it would actually type in a messaging app — no narration, actions,
scene descriptions or stage directions. Web conversations are unaffected.

## Timezones

Each user has an optional IANA timezone stored per
`(channel, channel_instance_id, external_user_id)` — the browser reports it
automatically for web sessions, and Telegram users set it with `/timezone
Europe/Paris`. It is used to resolve character-card schedules and to fill
`{{local_time}}` in proactive prompts.

## Proactive messages

Characters can initiate a conversation, either from a schedule declared in the
character card or from the proactive behavior engine (which asks the LLM,
via the `proactive_decision` prompt, whether contacting the user right now is
natural, and lets it skip). Generation reuses `generate_reply()` with the
`proactive_event` injection; `DeliveryService` handles the transport. Full
details: [07-character-card-schedules.md](07-character-card-schedules.md).

## SSE events

| Event | Data |
|---|---|
| `token` | `{"content": "..."}` |
| `image_start` | `{"generation_id": "..."}` |
| `image_complete` | `{"generation_id": "...", "image_url": "..."}` |
| `image_failed` | `{"generation_id": "...", "detail": "..."}` |
| `done` | `{"message_id": "...", "full_content": "...", "images": [...]}` |
| `error` | `{"detail": "..."}` |

## Prompt structure

```
[system]  effective system prompt + image-marker instruction
          + description, personality, scenario, mes_example
[assistant] first_mes (with macros resolved)
[user]    message 1
[assistant] response 1
...
[system]  post_history_instructions (if any)
[user]    new user message
```

When the prompt approaches `chat.context_window * chat.summarization_threshold` tokens, older messages are automatically compressed into a summary.

## Image marker

The LLM triggers image generation by writing:

```
[IMG: short English description of the image]
```

The backend appends this instruction to every system prompt:

> When the user explicitly requests a visual (e.g. "show me", "send a picture"), emit an inline marker `[IMG: <short English description>]`. Do NOT emit markers unless the user asked for one. Keep the description concrete and under 200 characters. Continue your narration normally after the marker.

The marker never reaches the frontend or the stored message. Max 3 markers per assistant message.

## OOC protection

When `chat.ooc_protection: true` (default), the backend detects common jailbreak/break-character patterns and injects a system-level guardrail message before the user turn.
