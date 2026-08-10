# Configuration

## config.yaml

Copy `config.example.yaml` to `config.yaml`. Settings can also be changed from the Admin UI.

```yaml
app:
  host: "0.0.0.0"
  port: 8123
  log_level: "INFO"      # DEBUG | INFO | WARNING | ERROR
  data_dir: "data"

active_connectors:
  text: ""               # set by Admin UI
  image: ""

user:
  name: "User"           # {{user}} macro replacement

scheduler:
  enabled: false
  interval_seconds: 86400
  cleanup_older_than_days: 30

chat:
  context_window: 4096
  summarization_threshold: 0.75
  ooc_protection: true

observability:
  metrics_enabled: false   # expose Prometheus metrics at /metrics

gui:
  custom_css: ""
  custom_header_html: ""
  custom_footer_html: ""
```

## GUI customization

`gui.custom_css` injects CSS into every page.

`gui.custom_header_html` and `gui.custom_footer_html` are injected as **raw HTML** — they are not sanitized. Anyone with admin access can use these fields to execute arbitrary JavaScript in every visitor's browser. Treat your admin password accordingly, and do not expose the admin panel to untrusted users.

## Environment variable overrides

| Variable | Config key |
|---|---|
| `AUBERGE_DATA_DIR` | `app.data_dir` |
| `AUBERGE_HOST` | `app.host` |
| `AUBERGE_PORT` | `app.port` |
| `AUBERGE_LOG_LEVEL` | `app.log_level` |
| `AUBERGE_USER_NAME` | `user.name` |
| `AUBERGE_SENTRY_DSN` | `app.sentry_dsn` |
| `AUBERGE_ADMIN_PASSWORD_HASH` | `app.admin_password_hash` |
| `AUBERGE_LLM_API_URL` | Auto-provision text connector on startup (OpenAI-compatible base URL) |
| `AUBERGE_LLM_MODEL` | Text model name (e.g. `qwen3:27b`) |
| `AUBERGE_LLM_CONTEXT_WINDOW` | Context window of the text model in tokens (default: `4096`) |
| `AUBERGE_LLM_MAX_TOKENS` | Max tokens to generate per reply (default: `1024`) |
| `AUBERGE_IMG_API_URL` | Auto-provision image connector on startup |
| `AUBERGE_IMG_MODEL` | Image model name |
| `AUBERGE_METRICS_ENABLED` | `observability.metrics_enabled` (`1`/`true`/`yes`/`on`) |
| `AUBERGE_DISABLE_ADMIN_AUTH` | Set to `1` to bypass admin authentication — **dev/testing only, never use in production** |

## Admin password

Generated on first startup and printed to the console. To reuse across restarts, copy the hash into `config.yaml:app.admin_password_hash` or set `AUBERGE_ADMIN_PASSWORD_HASH`.
