"""Shared HTTP error helpers for the router layer."""

from __future__ import annotations

import logging

from fastapi import HTTPException

CONFIG_NOT_WRITABLE = "config.yaml is not writable"


def config_write_error(exc: OSError) -> HTTPException:
    """Turn a failed config.yaml write into an explicit, readable API error."""
    logging.getLogger(__name__).warning("Cannot write config.yaml: %s", exc)
    return HTTPException(status_code=500, detail=CONFIG_NOT_WRITABLE)
