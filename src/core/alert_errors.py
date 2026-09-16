"""Log failures without serializing exception values, locals or request bodies."""

import logging
import traceback

from src.core.alert_channels import AlertError

logger = logging.getLogger(__name__)


def failure_reason(error):
    if isinstance(error, AlertError):
        return str(error)
    status = getattr(error, "status", None)
    if status == 403:
        return "Discord denied access. The bot needs permission to manage channels and webhooks in that server."
    if status == 404:
        return "Discord could not find the server, channel or webhook. Check the selected server and bot membership."
    if status is not None and isinstance(status, int):
        return f"Discord returned HTTP {status}. The result is unconfirmed; check setup status before retrying."
    if isinstance(error, TimeoutError):
        return "A service timed out. The result is unconfirmed; check setup status before retrying."
    if isinstance(error, OSError):
        return "A local file or network connection was unavailable. The technical details are in the engine log."
    return "An internal setup error occurred. The exception type and traceback are in the engine log."


def log_failure(error, operation):
    # format_exception/logger.exception would include arbitrary exception values
    # (including URLs, tokens and vendor bodies). Keep stack locations and types.
    frames = traceback.extract_tb(error.__traceback__)
    stack = "\n".join(f"  File {frame.filename}, line {frame.lineno}, in {frame.name}" for frame in frames)
    logger.error("Alert %s failed: %s\nTraceback (most recent call last):\n%s", operation, type(error).__name__, stack)
