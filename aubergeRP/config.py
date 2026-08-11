from __future__ import annotations

import contextlib
import logging
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# HTML sanitization helpers
# ---------------------------------------------------------------------------

_MAX_HTML_LEN = 65_536  # 64 KiB — generous limit for any custom header/footer

# Simple regex for stripping javascript: pseudo-protocol in href (no DOTALL,
# no alternation with overlapping patterns → no ReDoS risk).
_JAVASCRIPT_HREF_RE = re.compile(r"javascript\s*:", re.IGNORECASE)


class _JSSanitizer(HTMLParser):
    """Walk the HTML tree and drop <script> tags and inline event handlers.

    Using HTMLParser avoids the ReDoS risk that comes with regex-based
    HTML parsing while correctly handling edge cases like whitespace or
    extra attributes in closing tags.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._output: list[str] = []
        self._in_script: bool = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self._in_script = True
            return
        # Rebuild opening tag, dropping event-handler attributes and
        # javascript: hrefs.
        safe_attrs: list[str] = []
        for name, value in attrs:
            if name.lower().startswith("on"):
                continue
            if name.lower() == "href" and value and _JAVASCRIPT_HREF_RE.search(value):
                safe_attrs.append('href="#"')
                continue
            if value is None:
                safe_attrs.append(name)
            else:
                safe_attrs.append(f'{name}="{value}"')
        attr_str = (" " + " ".join(safe_attrs)) if safe_attrs else ""
        self._output.append(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._in_script = False
            return
        self._output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._in_script:
            self._output.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._in_script:
            self._output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._in_script:
            self._output.append(f"&#{name};")

    def get_output(self) -> str:
        return "".join(self._output)


def _strip_js(html: str) -> str:
    """Remove <script> blocks, inline event handlers and javascript: hrefs.

    Uses Python's HTMLParser to avoid regex-based ReDoS vulnerabilities.
    Applies a maximum length cap before parsing to bound worst-case time.
    """
    if len(html) > _MAX_HTML_LEN:
        html = html[:_MAX_HTML_LEN]
    sanitizer = _JSSanitizer()
    sanitizer.feed(html)
    return sanitizer.get_output()


class AppConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8123
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    data_dir: str = "data"
    sentry_dsn: str = ""
    admin_password_hash: str = ""
    # Optional explicit signing secret for admin JWTs.
    # If empty, startup derives a stable secret from app.admin_password_hash.
    admin_jwt_secret: str = ""
    admin_token_ttl_seconds: int = 86400

    @field_validator("admin_token_ttl_seconds")
    @classmethod
    def validate_admin_token_ttl_seconds(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("admin_token_ttl_seconds must be > 0")
        return v


class ActiveConnectorsConfig(BaseModel):
    text: str = ""
    image: str = ""
    # Optional per-task text connectors. Empty means "same as `text`".
    text_summarization: str = ""
    text_utility: str = ""


class UserConfig(BaseModel):
    name: str = "User"


class SchedulerConfig(BaseModel):
    """Background media-cleanup scheduler settings."""

    enabled: bool = False
    # How often to run the cleanup (in seconds). Default: 24 h.
    interval_seconds: int = 86400
    # Delete images older than this many days. Default: 30.
    cleanup_older_than_days: int = 30
    # Periodically test active connectors in the background so /api/health
    # reflects a real liveness signal without user-triggered tests.
    health_check_enabled: bool = True
    # How often to test the active connectors (in seconds). Default: 5 min.
    health_check_interval_seconds: int = 300

class ChatConfig(BaseModel):
    """Global settings that influence AI quality behaviour."""

    # Estimated size of the model's context window in tokens.
    context_window: int = 4096
    # Summarize conversation history when this fraction of the context window is consumed.
    summarization_threshold: float = 0.75
    # Enable out-of-character (OOC) detection and guardrail injection.
    ooc_protection: bool = True


class ObservabilityConfig(BaseModel):
    """Operational observability settings."""

    # Expose Prometheus metrics on /metrics.  Disabled by default because the
    # endpoint is unauthenticated and meant to sit behind a private network.
    metrics_enabled: bool = False


class GuiConfig(BaseModel):
    """GUI customization settings."""

    # Arbitrary CSS injected into every page inside a <style> tag.
    custom_css: str = ""
    # Raw HTML inserted just after the opening <header> tag.
    custom_header_html: str = ""
    # Raw HTML inserted just before the closing </footer> tag.
    custom_footer_html: str = ""

    @field_validator("custom_header_html", "custom_footer_html", mode="before")
    @classmethod
    def sanitize_html(cls, v: object) -> object:
        if isinstance(v, str):
            return _strip_js(v)
        return v


class Config(BaseModel):
    app: AppConfig = AppConfig()
    active_connectors: ActiveConnectorsConfig = ActiveConnectorsConfig()
    user: UserConfig = UserConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    chat: ChatConfig = ChatConfig()
    observability: ObservabilityConfig = ObservabilityConfig()
    gui: GuiConfig = GuiConfig()

    @field_validator("app", mode="before")
    @classmethod
    def validate_app(cls, v: object) -> object:
        return v or {}

    @field_validator("active_connectors", mode="before")
    @classmethod
    def validate_active_connectors(cls, v: object) -> object:
        return v or {}

    @field_validator("user", mode="before")
    @classmethod
    def validate_user(cls, v: object) -> object:
        return v or {}

    @field_validator("scheduler", mode="before")
    @classmethod
    def validate_scheduler(cls, v: object) -> object:
        return v or {}

    @field_validator("chat", mode="before")
    @classmethod
    def validate_chat(cls, v: object) -> object:
        return v or {}

    @field_validator("observability", mode="before")
    @classmethod
    def validate_observability(cls, v: object) -> object:
        return v or {}

    @field_validator("gui", mode="before")
    @classmethod
    def validate_gui(cls, v: object) -> object:
        return v or {}

def _apply_env_overrides(config: Config) -> Config:
    """Override config values with environment variables if set.

    Supported variables:
        AUBERGE_DATA_DIR           → config.app.data_dir
        AUBERGE_HOST               → config.app.host
        AUBERGE_PORT               → config.app.port  (integer)
        AUBERGE_LOG_LEVEL          → config.app.log_level
        AUBERGE_USER_NAME          → config.user.name
        AUBERGE_SENTRY_DSN         → config.app.sentry_dsn
        AUBERGE_ADMIN_PASSWORD_HASH → config.app.admin_password_hash
        AUBERGE_ADMIN_JWT_SECRET   → config.app.admin_jwt_secret
        AUBERGE_ADMIN_TOKEN_TTL_SECONDS → config.app.admin_token_ttl_seconds
        AUBERGE_METRICS_ENABLED    → config.observability.metrics_enabled
    """
    if val := os.environ.get("AUBERGE_DATA_DIR"):
        config.app.data_dir = val
    if val := os.environ.get("AUBERGE_HOST"):
        config.app.host = val
    if val := os.environ.get("AUBERGE_PORT"):
        config.app.port = int(val)
    if val := os.environ.get("AUBERGE_LOG_LEVEL"):
        config.app.log_level = val  # type: ignore[assignment]
    if val := os.environ.get("AUBERGE_USER_NAME"):
        config.user.name = val
    if val := os.environ.get("AUBERGE_SENTRY_DSN"):
        config.app.sentry_dsn = val
    if val := os.environ.get("AUBERGE_ADMIN_PASSWORD_HASH"):
        config.app.admin_password_hash = val
    if val := os.environ.get("AUBERGE_ADMIN_JWT_SECRET"):
        config.app.admin_jwt_secret = val
    if val := os.environ.get("AUBERGE_ADMIN_TOKEN_TTL_SECONDS"):
        config.app.admin_token_ttl_seconds = int(val)
    if val := os.environ.get("AUBERGE_METRICS_ENABLED"):
        config.observability.metrics_enabled = val.strip().lower() in {"1", "true", "yes", "on"}
    return config

def load_config(path: str | Path = "config.yaml") -> Config:
    log = logging.getLogger(__name__)
    config_path = Path(path).resolve()
    if not config_path.exists():
        log.warning("config.yaml not found at %s — using defaults", config_path)
        return _apply_env_overrides(Config())
    with config_path.open() as f:
        raw = yaml.safe_load(f) or {}
    config = _apply_env_overrides(Config(**raw))
    log.info(
        "Config loaded from %s | data_dir=%s active_text=%s active_image=%s",
        config_path,
        config.app.data_dir,
        config.active_connectors.text or "(none)",
        config.active_connectors.image or "(none)",
    )
    return config


_config: Config | None = None
_config_path: Path = Path("config.yaml")
_config_mtime: float = 0.0


def get_config() -> Config:
    """Return the current configuration, reloading from disk if the file changed.

    In development (``uvicorn --reload``) the process is not restarted on
    ``config.yaml`` changes, so we detect modifications via the file's mtime
    and reload transparently.  In production the overhead is negligible (a
    single ``stat()`` call per request).
    """
    global _config, _config_mtime
    if _config is None:
        _config = load_config(_config_path)
        with contextlib.suppress(OSError):
            _config_mtime = _config_path.resolve().stat().st_mtime
        return _config

    # Check if the file has been modified since last load.
    try:
        current_mtime = _config_path.resolve().stat().st_mtime
    except OSError:
        return _config

    if current_mtime != _config_mtime:
        logging.getLogger(__name__).info(
            "config.yaml changed on disk — reloading configuration"
        )
        _config = load_config(_config_path)
        _config_mtime = current_mtime

    return _config


def reset_config() -> None:
    global _config, _config_mtime
    _config = None
    _config_mtime = 0.0
