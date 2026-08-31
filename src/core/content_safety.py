"""Content safety pipeline — sanitization, injection scoring, behavioral monitoring.

Three layers, all alert-only (never block):
1. Sanitization — strip invisible chars, HTML, data URIs
2. Injection scoring — 0-10 score on external content
3. Behavioral monitoring — watch agent action patterns

Alerts go to audit log and optionally to a Discord channel.
"""

import html
import logging
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# === Layer 1: Sanitization ===

#: The categories a sanitize pass can remove, in the order the pass applies
#: them. Split out because the total was never a usable signal: on any real web
#: page `markup` and `whitespace` dominate by orders of magnitude, so a single
#: "chars removed" figure says "this was HTML" and nothing else. The two that
#: carry security signal are `invisible` and `base64`; a reader who is told only
#: the total cannot tell 1,656 chars of ordinary markup from a payload, and will
#: reasonably assume the worst about a number that is only ever large.
SANITIZE_CATEGORIES = ("invisible", "script_style", "markup", "base64", "whitespace")

#: Categories that indicate deliberately concealed content rather than the
#: ordinary shape of a web page. Zero-width and bidi characters are how text is
#: hidden from a human reader while staying in the model's context; an oversized
#: base64 blob is how a payload is smuggled past a pattern scan.
SANITIZE_SIGNAL_CATEGORIES = ("invisible", "base64")


@dataclass
class SanitizeReport:
    """What a sanitize pass removed, by category, with samples.

    `text` is the sanitized output, so a caller never needs to run the pass
    twice, and `sanitize()` itself is defined in terms of this — the count and
    the content it describes cannot drift apart.
    """

    text: str
    removed: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total_removed(self) -> int:
        return sum(self.removed.values())

    @property
    def signal_removed(self) -> int:
        """Chars removed by the categories that indicate concealment."""
        return sum(self.removed.get(c, 0) for c in SANITIZE_SIGNAL_CATEGORIES)

    def summary(self) -> str:
        """One line, largest category first, only what was actually removed."""
        parts = [f"{n} {c}" for c, n in
                 sorted(self.removed.items(), key=lambda kv: -kv[1]) if n]
        return ", ".join(parts) if parts else "nothing"


def _invisible_chars(text: str) -> str:
    """The Cf/Cc/Co characters a pass strips, minus the whitespace it keeps."""
    return "".join(
        c for c in text
        if unicodedata.category(c) in ("Cf", "Cc", "Co") and c not in ("\n", "\r", "\t")
    )


def sanitize_report(text: str, max_samples: int = 12,
                    sample_chars: int = 200) -> SanitizeReport:
    """Sanitize, and account for what each step removed.

    Samples are collected only for the categories that carry signal. Markup and
    whitespace are counted but never retained: they are the bulk of the volume,
    they are of no interest to a reader, and holding megabytes of stripped HTML
    to answer a reaction nobody will send is a cost with no return.
    """
    if not text:
        return SanitizeReport(text=text or "")

    removed: dict[str, int] = {}
    samples: dict[str, list[str]] = {}

    def _note(category: str, count: int) -> None:
        if count > 0:
            removed[category] = removed.get(category, 0) + count

    # Invisible Unicode (zero-width joiners, bidi overrides, BOM). Sampled as
    # code points: the characters are by definition unreadable, so the only
    # useful rendering of them is their names.
    hidden = _invisible_chars(text)
    if hidden:
        _note("invisible", len(hidden))
        seen: list[str] = []
        for ch in hidden:
            label = f"U+{ord(ch):04X} {unicodedata.name(ch, 'unnamed')}"
            if label not in seen:
                seen.append(label)
            if len(seen) >= max_samples:
                break
        samples["invisible"] = seen
    text = "".join(
        c for c in text
        if unicodedata.category(c) not in ("Cf", "Cc", "Co")
        or c in ("\n", "\r", "\t")
    )

    # <script>/<style> blocks, counted apart from ordinary tags: a page carrying
    # far more script than text is worth seeing, and it is invisible inside a
    # combined markup figure.
    before = len(text)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    _note("script_style", before - len(text))

    # Remaining HTML tags. On a real page this is the largest category by far
    # and means only that the fetch returned HTML.
    before = len(text)
    text = re.sub(r"<[^>]+>", " ", text)
    _note("markup", before - len(text))

    # Entity unescaping can shorten or lengthen; either way it is not removal,
    # so it is deliberately not counted.
    text = html.unescape(text)

    # Oversized base64 data URIs.
    before = len(text)
    blobs = re.findall(r"data:[a-zA-Z/+]+;base64,[A-Za-z0-9+/=]{200,}", text)
    text = re.sub(r"data:[a-zA-Z/+]+;base64,[A-Za-z0-9+/=]{200,}", "[base64-removed]", text)
    _note("base64", max(0, before - len(text)))
    if blobs:
        samples["base64"] = [b[:sample_chars] + "…" for b in blobs[:max_samples]]

    # NFC normalisation and whitespace collapsing, together: both are
    # reformatting, and neither is evidence of anything.
    before = len(text)
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    _note("whitespace", max(0, before - len(text)))

    return SanitizeReport(text=text, removed=removed, samples=samples)


def sanitize(text: str) -> str:
    """Sanitize external content before it enters LLM context.

    Applied to: search results, emails, API responses, URL content.
    NOT applied to: user messages from Discord (trusted input).
    """
    if not text:
        return text
    return sanitize_report(text).text


def format_sanitize_detail(report: SanitizeReport) -> str:
    """The stripped content, written out for a human to judge.

    Every category is listed with its count so the proportions are visible, but
    only the concealment categories get their content shown. Invisible
    characters are rendered as code points and names: they have no appearance
    by definition, so printing them would show an empty line and prove nothing.
    """
    lines = [f"Removed: {report.summary()}", ""]
    for category in SANITIZE_SIGNAL_CATEGORIES:
        count = report.removed.get(category, 0)
        if not count:
            continue
        lines.append(f"[{category}] {count} chars")
        for sample in report.samples.get(category, []):
            lines.append(f"  {sample}")
        lines.append("")
    bulk = [f"{c}={report.removed[c]}" for c in SANITIZE_CATEGORIES
            if c not in SANITIZE_SIGNAL_CATEGORIES and report.removed.get(c)]
    if bulk:
        lines.append("Not shown (ordinary page structure): " + ", ".join(bulk))
    return "\n".join(lines).strip()


# === Layer 2: Injection Scoring ===

# Patterns with their contribution to the score
INJECTION_PATTERNS = [
    # Instruction injection (high signal)
    (r"ignore\s+(all\s+)?previous\s+instructions", 4),
    (r"disregard\s+(all\s+)?prior", 4),
    (r"you\s+are\s+now\s+a", 3),
    (r"new\s+instructions?:", 3),
    (r"system\s*:\s*", 2),
    (r"<\|im_start\|>", 4),
    (r"<\|endoftext\|>", 4),
    (r"\[INST\]", 3),
    (r"</system>", 3),
    (r"<\|system\|>", 3),

    # Authority spoofing
    (r"emergency\s+override", 3),
    (r"admin\s+mode", 2),
    (r"maintenance\s+mode", 2),
    (r"debug\s+mode\s*:", 2),
    (r"as\s+an?\s+admin", 2),

    # Exfiltration
    (r"send\s+this\s+to\s+", 2),
    (r"forward\s+to\s+", 2),
    (r"post\s+in\s+channel", 2),
    (r"email\s+(this|it)\s+to", 2),

    # Delimiter attacks
    (r"---\s*BEGIN\s+SYSTEM", 3),
    (r"HUMAN\s*:", 2),
    (r"ASSISTANT\s*:", 2),
]


def score_injection(text: str, max_scan: int = 50000) -> tuple[int, list[str]]:
    """Score content for injection patterns. Returns (score 0-10, matched patterns).

    Only scans first max_scan bytes for performance.
    """
    if not text:
        return 0, []

    scan_text = text[:max_scan].lower()
    total_score = 0
    matched = []

    for pattern, score in INJECTION_PATTERNS:
        if re.search(pattern, scan_text, re.IGNORECASE):
            total_score += score
            matched.append(pattern)

    # Cap at 10
    return min(total_score, 10), matched


# === Layer 3: Behavioral Monitoring ===

class BehaviorMonitor:
    """Watches agent action patterns for anomalies.

    Tracks:
    - Tool call frequency
    - Sensitive actions after external content consumption
    - Novel tool usage
    - Rapid sensitive action sequences
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        # Per-agent tracking
        self._tool_history: dict[str, deque] = {}  # agent -> deque of (timestamp, tool_name)
        self._external_content_ts: dict[str, float] = {}  # agent -> last external content time
        self._baseline_tools: dict[str, set] = {}  # agent -> set of known tools (first 50 calls)
        self._baseline_count: dict[str, int] = {}  # agent -> total calls counted toward baseline

    def record_tool_call(self, agent_id: str, tool_name: str) -> list[dict]:
        """Record a tool call and return any alerts triggered."""
        now = time.time()
        alerts = []

        # Init tracking
        if agent_id not in self._tool_history:
            self._tool_history[agent_id] = deque(maxlen=500)
            self._baseline_tools[agent_id] = set()
            self._baseline_count[agent_id] = 0

        history = self._tool_history[agent_id]
        history.append((now, tool_name))

        # --- Check 1: Sensitive action after external content ---
        ext_ts = self._external_content_ts.get(agent_id, 0)
        is_sensitive = tool_name in _SENSITIVE_TOOLS
        if is_sensitive and (now - ext_ts) < 300:  # within 5 min
            alerts.append({
                "type": "sensitive_after_external",
                "severity": "HIGH",
                "tool": tool_name,
                "seconds_since_external": int(now - ext_ts),
            })

        # --- Check 2: Novel tool usage (after baseline) ---
        baseline = self._baseline_tools[agent_id]
        count = self._baseline_count[agent_id]
        if count < 50:
            baseline.add(tool_name)
            self._baseline_count[agent_id] = count + 1
        elif tool_name not in baseline:
            alerts.append({
                "type": "novel_tool",
                "severity": "MEDIUM",
                "tool": tool_name,
            })
            baseline.add(tool_name)  # only alert once

        # --- Check 3: Volume spike (3x rolling average in 10 min) ---
        ten_min_ago = now - 600
        recent_count = sum(1 for ts, _ in history if ts > ten_min_ago)
        if len(history) > 20:
            avg_per_10min = len(history) / (max(now - history[0][0], 600) / 600)
            if recent_count > avg_per_10min * 3 and recent_count > 10:
                alerts.append({
                    "type": "volume_spike",
                    "severity": "MEDIUM",
                    "recent_count": recent_count,
                    "average": round(avg_per_10min, 1),
                })

        # --- Check 4: Rapid sensitive actions (3+ different gated tools in 5 min) ---
        five_min_ago = now - 300
        recent_sensitive = {
            tn for ts, tn in history
            if ts > five_min_ago and tn in _SENSITIVE_TOOLS
        }
        if len(recent_sensitive) >= 3:
            alerts.append({
                "type": "rapid_sensitive",
                "severity": "HIGH",
                "tools": list(recent_sensitive),
            })

        return alerts

    def record_external_content(self, agent_id: str) -> None:
        """Record that an agent consumed external content (search results, URL, email)."""
        self._external_content_ts[agent_id] = time.time()


# Tools considered sensitive for behavioral monitoring
_SENSITIVE_TOOLS = {
    "send_email", "send_draft", "share_drive_file", "modify_drive_permissions",
    "install_mcp", "team_add", "team_remove", "team_update",
    "create_skill", "send_message",
}
