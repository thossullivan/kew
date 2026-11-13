"""Keep completed execution evidence when command resource cleanup fails."""

import logging
from collections.abc import Callable

_logger = logging.getLogger(__name__)


def finish_command_cleanup(cleanup: Callable[[], None], *, command: str) -> None:
    """Log cleanup failures without replacing a result or an active exception."""
    try:
        cleanup()
    except Exception:
        _logger.exception("%s checkout cleanup failed", command)
