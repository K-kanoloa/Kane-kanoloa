"""Shared validation for Kane Connector Protocol traces."""

from .protocol import ConformanceError, check_trace

__all__ = ["ConformanceError", "check_trace"]
