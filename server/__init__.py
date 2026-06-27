"""Papertrail server package — the always-on Docker bridge.

Implements the frozen ``pico-paper.v1`` contract (see ../SCHEMA.md):
ingest webhook events, resolve the current screen per device, and serve it to
the Waveshare Pico over LAN HTTP with ETag / If-None-Match short-circuiting.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
