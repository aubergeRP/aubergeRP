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
