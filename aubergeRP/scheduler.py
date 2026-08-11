"""Background scheduler for periodic media cleanup.

The scheduler runs an ``asyncio`` task in the background if enabled via
config.  It is started/stopped by ``main.py`` on app startup/shutdown.

Manual cleanup is also available through the
``POST /api/images/cleanup`` endpoint (see :mod:`aubergeRP.routers.images`).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import Config

logger = logging.getLogger(__name__)


def cleanup_images(data_dir: str | Path, older_than_days: int) -> int:
    """Delete PNG images older than *older_than_days* days.

    Walks all sub-directories of ``{data_dir}/images/`` and removes files
    whose modification time is older than the threshold.

    Returns the number of files deleted.
    """
    import time

    base = Path(data_dir) / "images"
    if not base.exists():
        return 0

    cutoff = time.time() - older_than_days * 86400
    deleted = 0
    for img_file in base.rglob("*.png"):
        try:
            if img_file.stat().st_mtime < cutoff:
                img_file.unlink()
                deleted += 1
        except OSError:
            pass
    return deleted


async def check_connectors_health(config: Config) -> dict[str, bool]:
    """Test every active connector and persist the result.

    Returns a mapping of connector ID → connected flag for the connectors that
    were actually tested.
    """
    from .routers.connectors import _last_test_results, get_connector_manager

    manager = get_connector_manager()
    results: dict[str, bool] = {}
    ids = [config.active_connectors.text, config.active_connectors.image]
    for connector_id in ids:
        if not connector_id:
            continue
        try:
            result = await manager.test_connector(connector_id)
            connected = bool(result.get("connected", False))
        except KeyError:
            # Connector was deleted meanwhile — drop any stale result.
            _last_test_results.pop(connector_id, None)
            continue
        except Exception:
            logger.warning("Health check failed for connector '%s'", connector_id)
            connected = False
        _last_test_results.set(connector_id, connected)
        results[connector_id] = connected
    return results


class Scheduler:
    """Simple asyncio-based background scheduler."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._health_task: asyncio.Task | None = None  # type: ignore[type-arg]

    def start(self) -> None:
        if self._config.scheduler.enabled:
            self._task = asyncio.create_task(self._run())
            logger.info(
                "Background scheduler started (interval=%ds, cleanup_older_than=%dd)",
                self._config.scheduler.interval_seconds,
                self._config.scheduler.cleanup_older_than_days,
            )
        if self._config.scheduler.health_check_enabled:
            self._health_task = asyncio.create_task(self._run_health_checks())
            logger.info(
                "Connector health checks started (interval=%ds)",
                self._config.scheduler.health_check_interval_seconds,
            )

    def stop(self) -> None:
        for attr in ("_task", "_health_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                setattr(self, attr, None)

    async def _run_health_checks(self) -> None:
        interval = self._config.scheduler.health_check_interval_seconds
        # Give the app a moment to finish booting before the first check.
        await asyncio.sleep(min(5, interval))
        while True:
            try:
                await check_connectors_health(self._config)
            except Exception:
                logger.exception("Scheduler: error during connector health check")
            await asyncio.sleep(interval)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._config.scheduler.interval_seconds)
            try:
                n = cleanup_images(
                    self._config.app.data_dir,
                    self._config.scheduler.cleanup_older_than_days,
                )
                if n:
                    logger.info("Scheduler: deleted %d old image(s)", n)
            except Exception:
                logger.exception("Scheduler: error during cleanup")
