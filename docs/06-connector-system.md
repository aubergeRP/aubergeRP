# Connector System

A **connector** is a pluggable module for a specific external generation backend. The rest of the app never talks to backends directly.

## Available connectors

| Backend ID | Types | Description |
|---|---|---|
| `openai_api` | `text`, `image` | Any OpenAI-compatible API (LocalAI, OpenRouter, OpenAI, vLLM, …) |
| `comfyui` | `image` | Local ComfyUI instance with workflow templates |

## Interfaces

```python
class TextConnector(BaseConnector):
    connector_type = "text"

    async def stream_chat_completion(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Yield text tokens from a streaming chat completion."""

class ImageConnector(BaseConnector):
    connector_type = "image"

    async def generate_image(
        self,
        prompt: str,
        negative_prompt: str = "",
        model: str | None = None,
        size: str | None = None,
    ) -> bytes:
        """Generate an image. Return raw PNG bytes."""

class BaseConnector(ABC):
    async def test_connection(self) -> dict:
        """Return {'connected': bool, 'details': {...}}."""
```

## Config fields per backend

### `openai_api` (text)

| Field | Required | Default | Description |
|---|---|---|---|
| `base_url` | yes | `http://localhost:8080/v1` | API base URL (LocalAI default) |
| `api_key` | no | `""` | API key |
| `model` | yes | `llama3` | Model name |
| `max_tokens` | no | `1024` | Max response tokens |
| `temperature` | no | `0.8` | Temperature |
| `timeout` | no | `120` | Request timeout (s) |

### `openai_api` (image)

| Field | Required | Default |
|---|---|---|
| `base_url` | yes | `https://openrouter.ai/api/v1` |
| `api_key` | yes | — |
| `model` | yes | `google/gemini-2.0-flash-exp:free` |
| `size` | no | `1024x1024` |

### `comfyui` (image)

| Field | Required | Default |
|---|---|---|
| `base_url` | yes | `http://localhost:8188` |
| `workflow` | no | `default` |
| `timeout` | no | `120` |

## Adding a new connector backend

1. Create `aubergeRP/connectors/my_backend.py`.
2. Subclass `TextConnector` or `ImageConnector` from `connectors/base.py`.
3. Set `backend_id = "my_backend"` on the class.
4. Add a Pydantic config model in `aubergeRP/models/connector.py`.
5. Register the backend in `ConnectorManager._get_connector_class()` in `connectors/manager.py`.
6. Add it to `GET /api/connectors/backends` in `routers/connectors.py`.
7. Write tests in `tests/test_my_backend.py` using `respx` to mock HTTP.

## Active connector

`config.yaml:active_connectors.{type}` stores the UUID of the active connector per type. The Admin UI writes this when you click "Activate". One active connector per type at a time.

### Per-task text models

Text generation is split into three roles, resolved by
`ConnectorManager.get_text_connector(role)`:

| Role | Used for | Config key |
|---|---|---|
| `text` | roleplay replies, proactive in-character messages | `active_connectors.text` |
| `text_summarization` | conversation summarization | `active_connectors.text_summarization` |
| `text_utility` | proactive send/skip decision, image prompt building, character card translation | `active_connectors.text_utility` |

The last two are optional overrides: when empty they transparently fall back to
`text`, so a single connector keeps working exactly as before. They are set from
Admin → Configuration → "Advanced settings — one model per task", which lets you
run background tasks on a cheaper or faster model.
