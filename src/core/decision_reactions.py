"""Discover explicit go/no-go prompts for Discord's one-tap reactions.

This is presentation only. A bot's seed is never a user's decision, and the
existing reaction handlers still decide who may approve what. Do not infer a
request from emoji in a status report, quotation, or code example.
"""

import re

_ACTION = re.compile(
    r"^(?:(?:please|vänligen)\s+)?"
    r"(?:react|tap|click|reagera|tryck|klicka|markera|approve|godkänn)\b", re.I,
)
_GO_NO_GO = re.compile(r"^go\s*/\s*no[ -]?go\s*[:?]", re.I)
_PAIRS = (("✅", "🔴"), ("✅", "❌"), ("🟢", "🔴"))
_YES = r"(?:go|approve|yes|godkänn|ja)"
_NO = r"(?:no[ -]?go|reject|deny|stop|hold|no|avslå|nej)"


def decision_reactions(content: str) -> tuple[str, ...]:
    """Return the requested binary pair, or nothing for absent/ambiguous asks.

    Supported prompts are an imperative with an explicit pair, a paired
    approval/rejection legend, or a Go/no-go question/heading (defaults to
    ✅/🔴). Formal cards can keep their explicit ✅/❌ vocabulary.
    """
    if content.lstrip().startswith("📨"):
        return ()  # An inter-agent relay is not a new request to the reader.
    lines = []
    fence = None
    for line in content.splitlines():
        if line.startswith(("    ", "\t")) and fence is None:
            lines.append("")  # Markdown's indented code form.
            continue
        line = line.strip()
        marker = re.match(r"(`{3,}|~{3,})", line)
        if marker:
            run = marker[1]
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
            lines.append("")
            continue
        if fence or line.startswith(">") or line.startswith(('"', "“", "‘")):
            lines.append("")
            continue
        # Formatting is not content. Inline examples cannot create controls.
        line = re.sub(r"`+[^`]*`+", "", line)
        line = re.sub(r"^<@!?\d+>\s*", "", line)
        line = re.sub(r"^[#*\-\s]+", "", line).replace("**", "").replace("__", "")
        lines.append(line)

    matches = set()
    for index, line in enumerate(lines):
        pairs = [pair for pair in _PAIRS if all(emoji in line for emoji in pair)]
        if _ACTION.match(line) and len(pairs) == 1:
            matches.add(pairs[0])
        if _GO_NO_GO.match(line):
            # Do not invent a pair when the author supplied conflicting ones.
            if len(pairs) == 1:
                matches.add(pairs[0])
            elif not any(emoji in line for pair in _PAIRS for emoji in pair):
                matches.add(("✅", "🔴"))
        legend = line + "\n" + (lines[index + 1] if index + 1 < len(lines) else "")
        for yes, no in _PAIRS:
            if re.match(
                rf"^{yes}\s*[:=]?\s*{_YES}\b[^\n]*[\n/|·,;]\s*"
                rf"{no}\s*[:=]?\s*{_NO}\b", legend, re.I,
            ):
                matches.add((yes, no))
    return next(iter(matches)) if len(matches) == 1 else ()
