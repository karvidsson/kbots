"""Bounded rendering for the existing owner-ask morning DM."""

from src.core.hitl_display import literal

SOURCES = ("Asks", "HITL approvals", "Goals")
ROWS_PER_SOURCE = 3
SUMMARY_WIDTH = 58
MAX_CHARACTERS = 1900


def jump(guild, channel, message="") -> str:
    parts = [str(guild), str(channel)]
    if message:
        parts.append(str(message))
    if not all(p.isascii() and p.isdecimal() and len(p) <= 20 for p in parts):
        return ""
    return "https://discord.com/channels/" + "/".join(parts)


def age(created, now):
    hours = max(0, int((now - created) // 3600))
    return f"{hours // 24}d" if hours >= 24 else f"{hours}h"


def compose(sources: dict, now: float, summaries: bool) -> str:
    lines, links = ["```", "Waiting on you"], []
    for name in SOURCES:
        rows = sources[name]
        if rows is None:
            lines.append(f"{name}: unavailable")
            continue
        if not rows:
            continue
        lines.append(f"{name} ({len(rows)}):")
        for row in rows[:ROWS_PER_SOURCE]:
            url = row["url"]
            ref = ""
            if url:
                links.append(f"[{len(links) + 1}](<{url}>)")
                ref = f"[{len(links)}] "
            text = literal(row["text"], 58)
            lines.append(f"{ref}{age(row['created_at'], now):>3} {text}")
            summary = literal(row.get("summary") or "", SUMMARY_WIDTH)
            if summaries and summary:
                lines.append("    " + summary)
        if len(rows) > ROWS_PER_SOURCE:
            lines.append(f"+ {len(rows) - ROWS_PER_SOURCE} more")
    lines.append("```")
    if links:
        lines.append("Open: " + " ".join(links))
    return "\n".join(lines)


def render(sources: dict, now: float) -> str:
    """None means unavailable, [] means confirmed empty. At most nine rows.

    Each row carries one line saying what the decision is about, since a goal
    title or a tool name alone does not tell the owner what he is deciding.
    That line is the first thing dropped when the message would not fit: the
    list of what is waiting has to arrive whole.
    """
    if all(sources[name] == [] for name in SOURCES):
        return ""
    full = compose(sources, now, True)
    if len(full.encode("utf-16-le")) // 2 <= MAX_CHARACTERS:
        return full
    return compose(sources, now, False)
