"""What an alert could not fit, held until someone asks for it.

An alert has to be short enough to read at a glance and specific enough to act
on, and those pull against each other. The resolution here is the same one
`reply_shorten` uses for long replies: post the summary, keep the detail, hand
it over on a reaction.

On disk, not in memory, for two reasons. The alert is posted by the MCP server
process and the reaction arrives in the Discord connector process, so an
in-memory dict is not even visible to the reader. And this deployment deploys
often, so a restart between the alert and the reaction would otherwise strand
the detail with no way to ask for it again.

Keyed by the id of the alert message, capped and expiring, because this fires
on every web fetch and unbounded retention of stripped page content is a
disclosure risk that grows on its own.
"""

import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TTL_HOURS = 48.0
DEFAULT_MAX_FILES = 300
#: Per-record cap. The detail is a sample, never the whole fetched page: the
#: point is to show what class of thing was stripped, and a reader who needs
#: the full document has the audit log.
MAX_PAYLOAD_CHARS = 8000


def _default_dir() -> Path:
    from src.core.base import PROJECT_ROOT, overlay_state_path
    path = overlay_state_path("alert-details")
    return path if path is not None else PROJECT_ROOT / "data" / "alert-details"


class AlertDetailStore:
    """Detail for posted alerts, keyed by their Discord message id."""

    def __init__(self, directory=None, ttl_hours: float = DEFAULT_TTL_HOURS,
                 max_files: int = DEFAULT_MAX_FILES):
        self.dir = Path(directory) if directory else _default_dir()
        self.ttl = ttl_hours * 3600
        self.max_files = max_files

    def _path(self, message_id: str) -> Path:
        safe = re.sub(r"[^0-9A-Za-z_-]", "", str(message_id))[:64]
        return self.dir / f"{safe}.json"

    def put(self, message_id: str, kind: str, detail: str,
            meta: dict | None = None) -> None:
        payload = {
            "kind": kind,
            "detail": detail[:MAX_PAYLOAD_CHARS],
            "truncated": len(detail) > MAX_PAYLOAD_CHARS,
            "meta": meta or {},
            "stored_at": time.time(),
        }
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self._path(message_id)
            path.write_text(json.dumps(payload), encoding="utf-8")
            try:
                path.chmod(0o600)
            except OSError:
                pass
            self._prune()
        except OSError as e:
            logger.warning(f"alert-details: could not store detail: {e}")

    def get(self, message_id: str) -> dict | None:
        """Read detail without consuming it.

        Deliberately not take(): a reveal that destroys the record makes the
        alert unreadable to the second person who looks at it, and expiry
        already bounds retention.
        """
        path = self._path(message_id)
        try:
            if not path.is_file():
                return None
            if time.time() - path.stat().st_mtime > self.ttl:
                path.unlink(missing_ok=True)
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"alert-details: could not read detail: {e}")
            return None

    def _prune(self) -> None:
        try:
            files = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        now = time.time()
        for path in list(files):
            try:
                if now - path.stat().st_mtime > self.ttl:
                    path.unlink(missing_ok=True)
                    files.remove(path)
            except OSError:
                continue
        for path in files[: max(0, len(files) - self.max_files)]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue


def render_detail(record: dict, limit: int = 1800) -> str:
    """The reveal message.

    Everything in `detail` is content the sanitizer flagged, which makes it
    untrusted by definition: it arrived from a web page and it is being shown
    to a human in a chat client that renders markdown and resolves mentions.
    It goes inside a fence with its own backticks broken, and mention syntax
    defused, so that a stripped payload cannot become a live link, a ping, or
    formatting that hides part of itself.
    """
    detail = record.get("detail") or ""
    meta = record.get("meta") or {}
    header_bits = [f"**{record.get('kind', 'detail')}**"]
    if meta.get("tool"):
        header_bits.append(f"tool `{meta['tool']}`")
    if meta.get("agent"):
        header_bits.append(f"agent `{meta['agent']}`")
    header = " · ".join(header_bits)

    body = detail[:limit]
    clipped = record.get("truncated") or len(detail) > limit
    body = body.replace("```", "`​`​`")
    # @everyone/@here and role/user mentions resolve inside a code fence in some
    # clients, so break the syntax rather than trusting the fence alone.
    body = body.replace("@", "@​")
    note = "\n_(truncated)_" if clipped else ""
    return f"{header}\n```\n{body or '(nothing recorded)'}\n```{note}"
