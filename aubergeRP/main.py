from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import stat
from collections.abc import AsyncGenerator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Config, get_config
from .routers import admin as admin_router
from .routers import characters as characters_router
from .routers import chat as chat_router
from .routers import config as config_router
from .routers import connectors as connectors_router
from .routers import conversations as conversations_router
from .routers import health as health_router
from .routers import images as images_router
from .routers import media as media_router
from .routers import prompts as prompts_router
from .routers import schedules as schedules_router
from .routers import statistics as statistics_router
from .routers import telegram as telegram_router
from .routers import timezone as timezone_router
from .services.example_seed_service import seed_example_characters

_BUILTIN_WORKFLOWS_DIR = Path(__file__).parent / "comfyui_workflows"
logger = logging.getLogger(__name__)

# Explicit list of headers allowed with credentialed CORS requests.
# The wildcard "*" is rejected by browsers when credentials are included.
_CORS_ALLOW_HEADERS = "Content-Type, Authorization, X-Admin-Token, X-Session-Token"


class FrontendStaticFiles(StaticFiles):
    """Serve frontend files with explicit cache revalidation headers.

    Starlette already provides ETag/Last-Modified and handles conditional
    requests (304). We add Cache-Control directives so browsers always
    revalidate HTML/JS/CSS after updates.
    """

    async def get_response(self, path: str, scope: MutableMapping[str, Any]) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in {200, 304}:
            content_type = response.headers.get("content-type", "").lower()
            if "text/css" in content_type or "javascript" in content_type:
                response.headers["Cache-Control"] = "public, no-cache, must-revalidate"
            elif "text/html" in content_type:
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def _init_data_dirs(data_dir: str) -> None:
    base = Path(data_dir)
    for subdir in [
        "connectors", "avatars",
        "images",
        "comfyui_workflows",
    ]:
        (base / subdir).mkdir(parents=True, exist_ok=True)

    # Restrict connectors/ to the owner only — it contains API keys in plain text.
    connectors_dir = base / "connectors"
    try:
        connectors_dir.chmod(0o700)
    except OSError as exc:
        logger.warning(
            "Could not restrict permissions on connectors directory (%s): %s",
            connectors_dir,
            exc,
        )

    # Warn if the directory is still readable by group or others (e.g. when
    # running inside Docker as a bind-mount with permissive host perms).
    try:
        mode = connectors_dir.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            logger.warning(
                "SECURITY: connectors directory %s has group/other permissions "
                "(mode %s). API keys stored inside may be accessible to other "
                "users on this system. Run: chmod 700 %s",
                connectors_dir,
                oct(stat.S_IMODE(mode)),
                connectors_dir,
            )
    except OSError:
        pass

    # Seed built-in workflow templates into the user data dir (never overwrite)
    user_wf_dir = base / "comfyui_workflows"
    if _BUILTIN_WORKFLOWS_DIR.exists():
        for src in _BUILTIN_WORKFLOWS_DIR.glob("*.json"):
            dst = user_wf_dir / src.name
            if not dst.exists():
                shutil.copy2(src, dst)


def _init_sentry(dsn: str) -> None:
    """Initialise Sentry SDK if a DSN is configured."""
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=dsn,
            integrations=[StarletteIntegration(), FastApiIntegration()],
            traces_sample_rate=0.1,
            send_default_pii=False,
        )
    except ImportError:
        pass  # sentry-sdk is an optional dependency


_REDOC_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>aubergeRP — API Reference</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>body { margin: 0; padding: 0; }</style>
</head>
<body>
  <redoc spec-url='/openapi.json'></redoc>
  <script src="/vendor/redoc.standalone.js"></script>
</body>
</html>
"""


def _autoprovision_connectors(config: Config, data_dir: str) -> None:
    """Create and activate connectors from env vars if not already present.

    Env vars (all optional):
        AUBERGE_LLM_API_URL        OpenAI-compatible base URL  (e.g. http://ollama:11434/v1)
        AUBERGE_LLM_MODEL          Model name                  (e.g. qwen3.6-27b:q4km)
        AUBERGE_LLM_CONTEXT_WINDOW Model context window in tokens (default: 4096)
        AUBERGE_LLM_MAX_TOKENS     Max tokens to generate per reply (default: 1024)
        AUBERGE_LLM_TEMPERATURE    Sampling temperature (optional)
        AUBERGE_LLM_TOP_P          top_p nucleus sampling (optional)
        AUBERGE_LLM_TOP_K          top_k sampling (optional)
        AUBERGE_LLM_REPEAT_PENALTY repeat_penalty (optional)
        AUBERGE_IMG_API_URL        Image API base URL
        AUBERGE_IMG_MODEL          Image model name
    """
    from .connectors.manager import ConnectorManager
    from .models.connector import ConnectorCreate, ConnectorType

    llm_context_window = int(os.environ.get("AUBERGE_LLM_CONTEXT_WINDOW", "4096").strip())
    llm_max_tokens = int(os.environ.get("AUBERGE_LLM_MAX_TOKENS", "1024").strip())

    _llm_temperature = os.environ.get("AUBERGE_LLM_TEMPERATURE", "").strip()
    _llm_top_p = os.environ.get("AUBERGE_LLM_TOP_P", "").strip()
    _llm_top_k = os.environ.get("AUBERGE_LLM_TOP_K", "").strip()
    _llm_repeat_penalty = os.environ.get("AUBERGE_LLM_REPEAT_PENALTY", "").strip()

    llm_extra: dict[str, object] = {}
    if _llm_temperature:
        llm_extra["temperature"] = float(_llm_temperature)
    if _llm_top_p:
        llm_extra["top_p"] = float(_llm_top_p)
    if _llm_top_k:
        llm_extra["top_k"] = int(_llm_top_k)
    if _llm_repeat_penalty:
        llm_extra["repeat_penalty"] = float(_llm_repeat_penalty)

    specs: list[tuple[ConnectorType, str, str]] = [
        ("text",  os.environ.get("AUBERGE_LLM_API_URL", "").strip(), os.environ.get("AUBERGE_LLM_MODEL", "").strip()),
        ("image", os.environ.get("AUBERGE_IMG_API_URL", "").strip(), os.environ.get("AUBERGE_IMG_MODEL", "").strip()),
    ]
    if not any(url and model for _, url, model in specs):
        return

    manager = ConnectorManager(data_dir=data_dir, config=config)

    for conn_type, url, model in specs:
        if not url or not model:
            continue
        existing = [
            c for c in manager.list_connectors(conn_type)
            if c.config.get("base_url") == url and c.config.get("model") == model
        ]
        if existing:
            inst = existing[0]
            if not manager.is_active(inst.id):
                logger.info("Auto-provision: activating existing %s connector '%s'", conn_type, model)
                try:
                    manager.set_active(inst.id)
                except OSError:
                    logger.warning("Auto-provision: config.yaml is read-only, active connector not persisted")
            else:
                logger.info("Auto-provision: %s connector '%s' already active", conn_type, model)
            continue
        logger.info("Auto-provision: creating %s connector '%s' @ %s", conn_type, model, url)
        if conn_type == "text":
            extra = {"context_window": llm_context_window, "max_tokens": llm_max_tokens, **llm_extra}
        else:
            extra = {}
        inst = manager.create_connector(ConnectorCreate(
            name=model,
            type=conn_type,
            backend="openai_api",
            config={"base_url": url, "model": model, "api_key": "", **extra},
        ))
        try:
            manager.set_active(inst.id)
        except OSError:
            logger.warning("Auto-provision: config.yaml is read-only, active connector not persisted")


def _init_admin_password(config: Config) -> None:
    """Initialize admin password.

    If a hash is already set (via env var or config), use it as-is.
    Otherwise generate a new random password on every startup and display it
    in stdout — it is never persisted, so a new one appears at each restart.
    To get a stable password across restarts, set AUBERGE_ADMIN_PASSWORD_HASH.
    """
    from .utils.auth import generate_random_password, hash_password

    env_hash = os.environ.get("AUBERGE_ADMIN_PASSWORD_HASH", "").strip()
    if env_hash:
        config.app.admin_password_hash = env_hash
        return

    plain_password = generate_random_password()
    config.app.admin_password_hash = hash_password(plain_password)
    logger.info("=" * 70)
    logger.info("ADMIN PASSWORD (generated at startup, changes at each restart)")
    logger.info("Password: %s", plain_password)
    logger.info("Set AUBERGE_ADMIN_PASSWORD_HASH to make it permanent.")
    logger.info("=" * 70)


def _init_admin_jwt_secret(config: Config) -> None:
    """Initialize admin JWT signing secret.

    Priority order:
    1. AUBERGE_ADMIN_JWT_SECRET env var
    2. config.app.admin_jwt_secret (from config.yaml)
    3. secret derived from app.admin_password_hash
    4. generate a new random process-local secret
    """
    env_secret = os.environ.get("AUBERGE_ADMIN_JWT_SECRET", "").strip()
    if env_secret:
        config.app.admin_jwt_secret = env_secret
        return

    if config.app.admin_jwt_secret.strip():
        return

    admin_hash = config.app.admin_password_hash.strip()
    if admin_hash:
        config.app.admin_jwt_secret = hashlib.sha256(
            f"aubergeRP-admin-jwt:{admin_hash}".encode()
        ).hexdigest()
        return

    config.app.admin_jwt_secret = secrets.token_urlsafe(48)
    logger.warning(
        "Admin JWT secret generated in-memory because no explicit secret or "
        "admin password hash is configured; tokens will not survive restarts."
    )


def create_app() -> FastAPI:
    config = get_config()
    logging.basicConfig(level=getattr(logging, config.app.log_level, logging.INFO))
    logger.info(
        "Starting aubergeRP | data_dir=%s port=%s log_level=%s",
        Path(config.app.data_dir).resolve(),
        config.app.port,
        config.app.log_level,
    )

    # ── Warn loudly if the admin auth bypass is active ──────────────────────
    if os.environ.get("AUBERGE_DISABLE_ADMIN_AUTH", "").strip() == "1":
        logger.warning("=" * 70)
        logger.warning("WARNING: AUBERGE_DISABLE_ADMIN_AUTH=1 is set.")
        logger.warning("Admin authentication is COMPLETELY DISABLED.")
        logger.warning("Anyone can access the admin panel without a password.")
        logger.warning("NEVER use this setting in a production deployment.")
        logger.warning("=" * 70)
    _init_data_dirs(config.app.data_dir)
    _init_sentry(config.app.sentry_dsn)
    _init_admin_password(config)
    _init_admin_jwt_secret(config)
    _autoprovision_connectors(config, config.app.data_dir)

    # Initialise SQLite database and run migrations
    from .database import init_db
    init_db(config.app.data_dir)

    try:
        seed_example_characters(config.app.data_dir)
    except Exception:
        logger.exception("Example character seeding failed at startup")

    from .scheduler import Scheduler
    scheduler = Scheduler(config)

    from .services.proactive_scheduler_service import ProactiveScheduler
    proactive_scheduler = ProactiveScheduler(config.app.data_dir)

    from .services.telegram_runtime_manager import TelegramRuntimeManager
    telegram_manager = TelegramRuntimeManager(config.app.data_dir)
    # Make available to routers via module-level attribute
    import aubergeRP.main as _self  # noqa: PLC0415
    _self._telegram_manager = telegram_manager  # type: ignore[attr-defined]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        scheduler.start()
        proactive_scheduler.start()
        await telegram_manager.start_enabled_bots()
        yield
        await telegram_manager.stop_all()
        proactive_scheduler.stop()
        scheduler.stop()

    app = FastAPI(
        title="aubergeRP",
        version=__version__,
        description="A lightweight roleplay frontend with pluggable connectors",
        lifespan=lifespan,
        # Disable default docs paths — we serve our own at /api-docs
        docs_url=None,
        redoc_url=None,
    )

    # ── CORS auto-detection middleware ──────────────────────────────────────
    # Reads the Host header from each request and adds it as an allowed origin
    # so that browsers on the same machine always pass CORS checks.
    # When credentials are included the Allow-Headers value must be an explicit
    # list — the wildcard "*" is rejected by browsers in that case.

    @app.middleware("http")
    async def cors_auto_detect(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Handle CORS preflight without forwarding to the route handlers.
        if request.method == "OPTIONS":
            response = Response(status_code=204)
        else:
            response = await call_next(request)

        host = request.headers.get("host", "")
        origin = request.headers.get("origin", "")
        if origin and host:
            try:
                # Compare the origin's netloc (host+port) against the Host header
                # to avoid substring-match false positives (e.g. evil.host.com vs host.com).
                origin_netloc = urlparse(origin).netloc
                if origin_netloc == host:
                    response.headers["Access-Control-Allow-Origin"] = origin
                    response.headers["Access-Control-Allow-Credentials"] = "true"
                    response.headers["Access-Control-Allow-Methods"] = (
                        "GET,POST,PUT,PATCH,DELETE,OPTIONS"
                    )
                    response.headers["Access-Control-Allow-Headers"] = _CORS_ALLOW_HEADERS
            except Exception:
                pass
        return response

    # ── Security headers middleware ──────────────────────────────────────────
    @app.middleware("http")
    async def security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "media-src 'self' data: blob:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'self'",
        )
        return response

    app.include_router(characters_router.router, prefix="/api")
    app.include_router(conversations_router.router, prefix="/api")
    app.include_router(chat_router.router, prefix="/api")
    app.include_router(admin_router.router, prefix="/api")
    app.include_router(connectors_router.router, prefix="/api")
    app.include_router(config_router.router, prefix="/api")
    app.include_router(images_router.router, prefix="/api")
    app.include_router(media_router.router, prefix="/api")
    app.include_router(health_router.router, prefix="/api")
    app.include_router(statistics_router.router, prefix="/api")
    app.include_router(prompts_router.router, prefix="/api")
    app.include_router(telegram_router.router, prefix="/api")
    app.include_router(timezone_router.router, prefix="/api")
    app.include_router(schedules_router.router, prefix="/api")

    # ── API reference (Redoc) ───────────────────────────────────────────────
    @app.get("/api-docs", include_in_schema=False, response_class=HTMLResponse)
    async def api_docs() -> HTMLResponse:
        return HTMLResponse(content=_REDOC_HTML)

    frontend = Path("frontend")
    if frontend.exists():
        app.mount("/", FrontendStaticFiles(directory=str(frontend), html=True), name="frontend")

    return app


app = create_app()
