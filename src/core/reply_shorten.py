"""Post the point, keep the rest one tap away.

Measured across 1136 turns in turns.jsonl: the median agent reply is 1918
characters and 46% are over 2000. The reply contract shipped on 2026-08-21 to
ask agents to be brief; on the 11 turns since it moved the median to 1423 and
the p90 barely at all. A rule asks the model to choose brevity on every turn,
and it loses to whatever the model finds interesting.

So this is code, not another rule. Over the threshold, the reply is cut at a
structural boundary, the head is posted with a footer that says exactly how
much was held back, and the rest arrives on 🔍 or "more". Nothing is
summarised and nothing is lost: the cut is deterministic, so a badly placed cut
is the agent burying its conclusion, which is visible immediately and fixable
by the agent rather than hidden by a compressor.

An LLM compressor was the other candidate and was measured too: 5.0 to 7.8
seconds per call, because this deployment's only provider shells out to the
Claude Code CLI and pays process startup every time. Five seconds on every
substantial reply is worse than the problem.
"""

import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 700
# Never cut so early that the head cannot carry a conclusion, and never leave a
# scrap behind: below these the whole thing is posted as-is. Both scale down
# with the threshold, or a deployment that configures a tight threshold gets no
# shortening at all and no indication why.
MIN_HEAD = 200
MIN_TAIL = 200

# The marker that tells the reader a cut happened, what it costs to expand, and
# both ways to do it. A cut with no marker is the same failure class as a
# feature that never announces itself.
FOOTER = "━ shortened · react 🔍 or say \"more\" for the rest ({chars:,} chars{sections})"

# Boundaries to cut at, best first. A blank line before a markdown heading is
# the strongest signal an agent gives that a new section starts.
_BOUNDARIES = (
    re.compile(r"\n\n(?=#{1,6} )"),      # blank line before a heading
    re.compile(r"\n(?=#{1,6} )"),        # a heading
    re.compile(r"\n\n(?=\*\*[^*\n]+\*\*)"),  # blank line before a bold lead-in
    re.compile(r"\n\n"),                 # any paragraph break
)

_HEADING = re.compile(r"^#{1,6} ", re.M)
_ENDS_ON_HEADING = re.compile(r"(^|\n)#{1,6} [^\n]*$")
_FENCE = re.compile(r"^```", re.M)


def _fence_safe(text: str) -> bool:
    """True when the text does not end inside an unclosed code fence."""
    return len(_FENCE.findall(text)) % 2 == 0


def split_reply(content: str, threshold: int = DEFAULT_THRESHOLD) -> tuple[str, str] | None:
    """Return (head, rest), or None when the reply should be posted whole.

    The cut is taken at the LAST boundary that still fits under the threshold,
    so the head is as complete as it can be rather than as short as possible.
    A cut inside a code fence is never taken: half a fenced block renders as
    broken markdown and reads as a bug rather than as a shortened message.
    """
    if not content or len(content) <= threshold:
        return None

    min_head = min(MIN_HEAD, max(1, threshold // 2))
    min_tail = min(MIN_TAIL, max(1, threshold // 2))
    for pattern in _BOUNDARIES:
        candidates = []
        for m in pattern.finditer(content):
            if m.start() < min_head:
                continue
            if m.start() > threshold:
                break
            if not _fence_safe(content[:m.start()]):
                continue
            if _ENDS_ON_HEADING.search(content[:m.start()].rstrip()):
                # A head that ends on a bare heading promises a section and
                # delivers nothing, which looks like the message was truncated
                # by accident rather than shortened on purpose.
                continue
            candidates.append(m.start())
        # Latest first, so the head is as complete as it can be. Falling back
        # through the earlier candidates matters: the last one often leaves a
        # scrap of a tail, and abandoning the whole pattern there would drop to
        # a worse class of boundary or to sending the wall whole.
        for cut in reversed(candidates):
            head, rest = content[:cut].rstrip(), content[cut:].strip()
            if len(rest) >= min_tail:
                return head, rest

    # No usable boundary. A hard cut mid-sentence is worse than a long message,
    # so the reply goes out whole and the agent wears it.
    logger.debug("reply-shorten: no structural boundary under the threshold, "
                 "sending whole")
    return None


def footer(rest: str) -> str:
    """The line that makes the cut visible and reversible."""
    n = len(_HEADING.findall(rest))
    sections = f", {n} sections" if n > 1 else (", 1 section" if n == 1 else "")
    return FOOTER.format(chars=len(rest), sections=sections)


class OverflowStore:
    """The held-back remainders, on disk.

    On disk rather than in memory because this deployment deploys often, and a
    restart between the short message and the tap on 🔍 would otherwise strand
    the rest with no way to ask for it again. Keyed by the id of the message
    the reader is looking at.
    """

    def __init__(self, directory: str | Path, ttl_hours: float = 72.0,
                 max_files: int = 500):
        self.dir = Path(directory)
        self.ttl = ttl_hours * 3600
        self.max_files = max_files

    def _path(self, message_id: str) -> Path:
        safe = re.sub(r"[^0-9A-Za-z_-]", "", str(message_id))[:64]
        return self.dir / f"{safe}.md"

    def put(self, message_id: str, rest: str, channel_id: str | None = None) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._path(message_id).write_text(rest, encoding="utf-8")
            if channel_id:
                # A second key by channel, so "more" works without the reader
                # having to point at a particular message.
                (self.dir / f"channel-{channel_id}.txt").write_text(
                    str(message_id), encoding="utf-8")
            self._prune()
        except OSError as e:
            logger.warning(f"reply-shorten: could not store the remainder: {e}")

    def take(self, message_id: str) -> str | None:
        """Read and remove a remainder. Returns None when there is none."""
        path = self._path(message_id)
        try:
            if not path.is_file():
                return None
            text = path.read_text(encoding="utf-8")
            path.unlink(missing_ok=True)
            return text
        except OSError as e:
            logger.warning(f"reply-shorten: could not read the remainder: {e}")
            return None

    def take_latest_for_channel(self, channel_id: str) -> str | None:
        """The remainder of the most recent shortened message in a channel."""
        pointer = self.dir / f"channel-{channel_id}.txt"
        try:
            if not pointer.is_file():
                return None
            message_id = pointer.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        text = self.take(message_id)
        if text is not None:
            pointer.unlink(missing_ok=True)
        return text

    def _prune(self) -> None:
        """Drop remainders nobody asked for. Old first, then oldest over the cap."""
        try:
            files = sorted(self.dir.glob("*.md"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        now = time.time()
        for path in files:
            try:
                if now - path.stat().st_mtime > self.ttl:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
        try:
            files = sorted(self.dir.glob("*.md"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for path in files[:max(0, len(files) - self.max_files)]:
            path.unlink(missing_ok=True)


# Text an owner types to get the rest. Deliberately short and deliberately
# exact: a message that merely CONTAINS "more" is usually a real question.
MORE_WORDS = frozenset({
    "more", "more.", "more please", "go on", "expand", "elaborate",
    "full", "full version", "rest", "the rest", "details",
})


def wants_more(text: str) -> bool:
    return (text or "").strip().lower().rstrip("!?") in MORE_WORDS


class ReplyShortener:
    """Config plus the two decisions: should this be cut, and where."""

    def __init__(self, config: dict | None = None, store_dir: str | Path | None = None):
        cfg = (config or {}).get("shorten") or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.threshold = int(cfg.get("threshold_chars", DEFAULT_THRESHOLD))
        self.emoji = str(cfg.get("emoji", "🔍"))
        self.store = OverflowStore(store_dir or ".", ttl_hours=float(
            cfg.get("ttl_hours", 72.0)))

    def shorten(self, content: str) -> tuple[str, str] | None:
        """(head_with_footer, rest), or None to send the reply unchanged."""
        if not self.enabled:
            return None
        parts = split_reply(content, self.threshold)
        if parts is None:
            return None
        head, rest = parts
        return f"{head}\n\n{footer(rest)}", rest
