"""Marking the goals feature's own posts so they do not cost agent turns.

A goal channel's participants hear everything posted there, which is what
makes the room work: the user speaks and everyone in the goal is woken. The
feature's own confirmations ("X added to goal Y") went out through the same
door, so a single nomination card woke every participant and spent a turn
each on a message none of them could act on.

The discriminator has to travel ON the message. An id registry written after
the send loses a race it cannot win — a message id does not exist until
`channel.send` returns, by which point the gateway echo is already on its way
to the other clients, so any notice that loses that race still costs the
turns. It would also be intermittent, which is the worst kind of green test.

So every notice carries a leading marker and the inbound gate reads it off
the content. A webhook's `webhook_id` would be a field Discord sets rather
than a convention of ours, which is better in principle, but it needs Manage
Webhooks on every goal channel plus webhook lifecycle to match the channel's,
and it buys nothing here that one constant and two producers do not.

The marker is U+2063 INVISIBLE SEPARATOR: it has no width, survives Discord's
round trip, and is not something prose produces by accident. It goes at the
FRONT so that a notice long enough to be chunked still carries it on the
chunk the gate sees first.
"""

MARKER = "⁣"


def mark(text: str) -> str:
    """Tag `text` as the goals feature talking, not an agent."""
    if not text or text.startswith(MARKER):
        return text
    return f"{MARKER}{text}"


def is_system_notice(content: str | None) -> bool:
    """True for a post the goals feature made itself.

    Callers use this to skip a turn, so it must not guess: only the marker
    counts, never the shape of the text.
    """
    return bool(content) and content.startswith(MARKER)
