# API Reference

> Auto-generated — run `make doc` to update.

**Base URL:** `http://localhost:8123`  
**Interactive docs (Redoc):** [`/api-docs`](http://localhost:8123/api-docs)


## Admin

### `POST /api/admin/login`

Admin Login

Authenticate to admin panel with password.


**Request body:**

| Field | Type | Required |
|---|---|---|
| `password` | string | yes |

**Responses:** `200` Successful Response · `429` Too many login attempts · `422` Validation Error

### `POST /api/admin/logout`

Admin Logout

Logout from admin panel.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error


## Characters

### `POST /api/characters/import`

Import Character


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-admin-token` | header | string | no |  |

**Responses:** `201` Successful Response · `422` Validation Error

### `GET /api/characters/`

List Characters


**Responses:** `200` Successful Response

### `POST /api/characters/`

Create Character


**Request body:**

| Field | Type | Required |
|---|---|---|
| `name` | string | yes |
| `description` | string | yes |
| `personality` | string | no |
| `first_mes` | string | no |
| `mes_example` | string | no |
| `scenario` | string | no |
| `system_prompt` | string | no |
| `post_history_instructions` | string | no |
| `creator` | string | no |
| `creator_notes` | string | no |
| `character_version` | string | no |
| `tags` | array[string] | no |
| `extensions` | object | no |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-admin-token` | header | string | no |  |

**Responses:** `201` Successful Response · `422` Validation Error

### `GET /api/characters/{character_id}`

Get Character


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `character_id` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `PUT /api/characters/{character_id}`

Update Character


**Request body:**

| Field | Type | Required |
|---|---|---|
| `name` | string | yes |
| `description` | string | yes |
| `personality` | string | no |
| `first_mes` | string | no |
| `mes_example` | string | no |
| `scenario` | string | no |
| `system_prompt` | string | no |
| `post_history_instructions` | string | no |
| `creator` | string | no |
| `creator_notes` | string | no |
| `character_version` | string | no |
| `tags` | array[string] | no |
| `extensions` | object | no |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `character_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `DELETE /api/characters/{character_id}`

Delete Character


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `character_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `204` Successful Response · `422` Validation Error

### `GET /api/characters/{character_id}/avatar`

Get Avatar


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `character_id` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/characters/{character_id}/avatar`

Upload Avatar


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `character_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/characters/{character_id}/export/json`

Export Json


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `character_id` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/characters/{character_id}/export/png`

Export Png


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `character_id` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/characters/{character_id}/translate`

Translate Character Endpoint

Create a translated copy of a character using the active text connector.


**Request body:**

| Field | Type | Required |
|---|---|---|
| `language` | string | yes |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `character_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `201` Successful Response · `422` Validation Error

### `POST /api/characters/{character_id}/duplicate`

Duplicate Character


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `character_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `201` Successful Response · `422` Validation Error


## Chat

### `POST /api/chat/{conversation_id}/message`

Chat


**Request body:**

| Field | Type | Required |
|---|---|---|
| `content` | string | yes |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `conversation_id` | path | string | yes |  |
| `x-session-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/chat/{conversation_id}/events`

Chat Events

Long-lived SSE endpoint for multi-browser event delivery.

Other browser tabs sharing the same session token subscribe here and
receive every event published during chat, without having to be the tab
that sent the message.  The connection is kept open with periodic
keepalive comments so that EventSource auto-reconnect is not triggered.

The session token is passed as the ``session_token`` query parameter
(instead of a header) because the browser ``EventSource`` API does not
support custom request headers.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `conversation_id` | path | string | yes |  |
| `session_token` | query | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/chat/{conversation_id}/generate-image`

Generate Scene Image

Generate an image of the current scene from the conversation context.

This endpoint triggers image generation using the active image connector,
with a prompt built automatically from the recent conversation history via
the active text connector.  It is called when the user clicks the
"Generate scene image" button in the frontend.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `conversation_id` | path | string | yes |  |
| `x-session-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/chat/{conversation_id}/retry-image`

Retry Image

Retry generation of a single image with the given prompt and generation_id.

This endpoint is used when an image generation fails and the user clicks
the "Retry" button. It generates just the image without sending the entire
message through the chat flow again.


**Request body:**

| Field | Type | Required |
|---|---|---|
| `prompt` | string | yes |
| `generation_id` | string | yes |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `conversation_id` | path | string | yes |  |
| `x-session-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error


## Config

### `GET /api/config/`

Get Config Endpoint


**Responses:** `200` Successful Response

### `PUT /api/config/`

Update Config


**Request body:**

| Field | Type | Required |
|---|---|---|
| `app` | AppConfigResponse | null | no |
| `user` | UserConfigResponse | null | no |
| `active_connectors` | ActiveConnectorsResponse | null | no |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `PATCH /api/config/`

Patch Config


**Request body:**

| Field | Type | Required |
|---|---|---|
| `app` | AppConfigPatch | null | no |
| `user` | UserConfigPatch | null | no |
| `active_connectors` | ActiveConnectorsPatch | null | no |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/config/gui`

Get Gui Config


**Responses:** `200` Successful Response

### `PUT /api/config/gui`

Update Gui Config


**Request body:**

| Field | Type | Required |
|---|---|---|
| `custom_css` | string | no |
| `custom_header_html` | string | no |
| `custom_footer_html` | string | no |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error


## Connectors

### `GET /api/connectors/backends`

List Backends


**Responses:** `200` Successful Response

### `GET /api/connectors/comfyui-workflows`

List Comfyui Workflows

List available ComfyUI workflow template names.


**Responses:** `200` Successful Response

### `GET /api/connectors/`

List Connectors


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `type` | query | string | null | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/connectors/`

Create Connector


**Request body:**

| Field | Type | Required |
|---|---|---|
| `name` | string | yes |
| `type` | string | yes |
| `backend` | string | yes |
| `config` | object | no |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-admin-token` | header | string | no |  |

**Responses:** `201` Successful Response · `422` Validation Error

### `GET /api/connectors/{connector_id}`

Get Connector


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `connector_id` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `PUT /api/connectors/{connector_id}`

Update Connector


**Request body:**

| Field | Type | Required |
|---|---|---|
| `name` | string | yes |
| `type` | string | yes |
| `backend` | string | yes |
| `config` | object | no |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `connector_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `DELETE /api/connectors/{connector_id}`

Delete Connector


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `connector_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `204` Successful Response · `422` Validation Error

### `POST /api/connectors/{connector_id}/test`

Test Connector


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `connector_id` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/connectors/{connector_id}/test-chat`

Test Connector Chat

Test a text connector with a sample message and parameters.

This endpoint tests the connector by sending a sample message with the
specified sampling parameters and returns a sample of the response.
Useful for validating that parameters like temperature, top_p, etc. are
accepted by the connector.


**Request body:**

| Field | Type | Required |
|---|---|---|
| `message` | string | yes |
| `temperature` | number | null | no |
| `top_p` | number | null | no |
| `presence_penalty` | number | null | no |
| `frequency_penalty` | number | null | no |
| `extra_body` | object | null | no |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `connector_id` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/connectors/{connector_id}/activate`

Activate Connector


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `connector_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error


## Conversations

### `GET /api/conversations/`

List Conversations


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `character_id` | query | string | null | no |  |
| `x-session-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/conversations/`

Create Conversation


**Request body:**

| Field | Type | Required |
|---|---|---|
| `character_id` | string | yes |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-session-token` | header | string | no |  |

**Responses:** `201` Successful Response · `422` Validation Error

### `GET /api/conversations/{conversation_id}`

Get Conversation


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `conversation_id` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `DELETE /api/conversations/{conversation_id}`

Delete Conversation


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `conversation_id` | path | string | yes |  |
| `x-session-token` | header | string | no |  |

**Responses:** `204` Successful Response · `422` Validation Error


## Health

### `GET /api/health/`

Health


**Responses:** `200` Successful Response


## Images

### `GET /api/images/{session_token}/{image_id}`

Get Image


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `session_token` | path | string | yes |  |
| `image_id` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/images/cleanup`

Cleanup Old Images

Delete images older than *older_than_days* days from the data directory.


**Request body:**

| Field | Type | Required |
|---|---|---|
| `older_than_days` | integer | no |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error


## Media

### `GET /api/media/`

List Media


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `page` | query | integer | no | Page number (1-based) |
| `per_page` | query | integer | no | Items per page |
| `media_type` | query | string | null | no | Filter by media type (image, video, audio) |

**Responses:** `200` Successful Response · `422` Validation Error

### `DELETE /api/media/{media_id}`

Delete Media


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `media_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `204` Successful Response · `422` Validation Error


## Observability

### `GET /api/observability/overview`

Get Overview

Headline health numbers for every dashboard section.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `hours` | query | integer | no |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/observability/telegram`

Get Telegram

Configuration + runtime state of every configured Telegram bot.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/observability/telegram/{bot_id}/webhook`

Get Telegram Webhook

Live webhook information as reported by Telegram.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `bot_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/observability/sessions`

Get Sessions

Recent/active sessions across every transport.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `transport` | query | string | no |  |
| `bot_id` | query | string | no |  |
| `character_id` | query | string | no |  |
| `limit` | query | integer | no |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/observability/llm`

Get Llm

LLM generation aggregates and the most recent calls.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `hours` | query | integer | no |  |
| `generation_type` | query | string | no |  |
| `conversation_id` | query | string | no |  |
| `success` | query | boolean | null | no |  |
| `limit` | query | integer | no |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/observability/memory`

Get Memory

Estimated context pressure and summarization state per conversation.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `limit` | query | integer | no |  |
| `conversation_id` | query | string | no |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/observability/memory/{conversation_id}`

Get Memory Detail

Context detail for one conversation, including its stored summary.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `conversation_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/observability/schedules`

Get Schedules

Proactive schedule instances with their recent execution history.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `status` | query | string | no |  |
| `enabled` | query | boolean | null | no |  |
| `character_id` | query | string | no |  |
| `transport` | query | string | no |  |
| `limit` | query | integer | no |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/observability/errors`

Get Errors

Recent operational errors, newest first, already redacted.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `component` | query | string | no |  |
| `hours` | query | integer | no |  |
| `limit` | query | integer | no |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error


## Prompts

### `GET /api/prompts/`

Get All Prompts


**Responses:** `200` Successful Response

### `GET /api/prompts/{key}`

Get One Prompt


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `key` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `PUT /api/prompts/{key}`

Update Prompt


**Request body:**

| Field | Type | Required |
|---|---|---|
| `content` | string | yes |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `key` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `DELETE /api/prompts/{key}`

Reset Prompt Endpoint


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `key` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error


## Schedules

### `GET /api/schedules/instances/character/{character_id}`

List For Character


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `character_id` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/schedules/instances/conversation/{conversation_id}`

List For Conversation


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `conversation_id` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/schedules/instances/{instance_id}`

Get Instance


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `instance_id` | path | string | yes |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `DELETE /api/schedules/instances/{instance_id}`

Delete Instance


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `instance_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `204` Successful Response · `422` Validation Error

### `POST /api/schedules/instances`

Create Instance


**Request body:**

| Field | Type | Required |
|---|---|---|
| `schedule_def` | ScheduleDefinition | yes |
| `character_id` | string | yes |
| `conversation_id` | string | yes |
| `channel` | string | yes |
| `channel_instance_id` | string | yes |
| `external_user_id` | string | yes |
| `external_chat_id` | string | no |
| `timezone` | string | no |
| `origin` | string | no |
| `decision_mode` | string | no |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-admin-token` | header | string | no |  |

**Responses:** `201` Successful Response · `422` Validation Error

### `PATCH /api/schedules/instances/{instance_id}/enabled`

Set Enabled


**Request body:**

| Field | Type | Required |
|---|---|---|
| `enabled` | boolean | yes |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `instance_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error


## Statistics

### `GET /api/statistics/`

Get Statistics


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `days` | query | integer | no |  |
| `top` | query | integer | no |  |

**Responses:** `200` Successful Response · `422` Validation Error


## Telegram

### `GET /api/telegram/bots/`

List Bots


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/telegram/bots/`

Create Bot


**Request body:**

| Field | Type | Required |
|---|---|---|
| `name` | string | yes |
| `token` | string | yes |
| `character_id` | string | yes |
| `enabled` | boolean | no |
| `dialogue_only` | boolean | no |
| `update_mode` | string | no |
| `webhook_url` | string | no |
| `webhook_secret` | string | no |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-admin-token` | header | string | no |  |

**Responses:** `201` Successful Response · `422` Validation Error

### `GET /api/telegram/bots/{bot_id}`

Get Bot


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `bot_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `PATCH /api/telegram/bots/{bot_id}`

Update Bot


**Request body:**

| Field | Type | Required |
|---|---|---|
| `name` | string | null | no |
| `token` | string | null | no |
| `character_id` | string | null | no |
| `enabled` | boolean | null | no |
| `dialogue_only` | boolean | null | no |
| `update_mode` | string | null | no |
| `webhook_url` | string | null | no |
| `webhook_secret` | string | null | no |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `bot_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `DELETE /api/telegram/bots/{bot_id}`

Delete Bot


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `bot_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `204` Successful Response · `422` Validation Error

### `POST /api/telegram/bots/{bot_id}/enable`

Enable Bot


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `bot_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/telegram/bots/{bot_id}/disable`

Disable Bot


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `bot_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/telegram/bots/{bot_id}/test`

Test Bot


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `bot_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `POST /api/telegram/webhook/{bot_id}`

Receive Webhook

Receive updates pushed by Telegram in webhook mode.

Called by Telegram itself, so it is not behind the admin token: the
``X-Telegram-Bot-Api-Secret-Token`` header is the authentication.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `bot_id` | path | string | yes |  |
| `x-telegram-bot-api-secret-token` | header | string | null | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `GET /api/telegram/bots/{bot_id}/status`

Bot Status


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `bot_id` | path | string | yes |  |
| `x-admin-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error


## Timezone

### `GET /api/timezone/`

Get Timezone

Return the IANA timezone stored for this web session, or null.


**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-session-token` | header | string | no |  |

**Responses:** `200` Successful Response · `422` Validation Error

### `PUT /api/timezone/`

Set Timezone

Validate and persist an IANA timezone for this web session.


**Request body:**

| Field | Type | Required |
|---|---|---|
| `timezone` | string | yes |

**Parameters:**

| Name | In | Type | Required | Description |
|---|---|---|---|---|
| `x-session-token` | header | string | yes |  |

**Responses:** `200` Successful Response · `401` Missing session token · `422` Validation Error
