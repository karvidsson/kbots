# Closing a goal

`goal_set status=done` posts a summary with ✅ and ❌. Silence leaves the room
open and gives no approval. The closing summary is stored in `goals.db`.

Only the entitled human may decide. Selection uses `goals.escalation_user`
first, then `waiting_on_you.owner_id`, then the goal's human `created_by`,
then the sole Discord admin for goals created by agents. The selected ID must
still be a configured Discord admin. Multiple admins with an agent-created
goal need an explicit owner setting; the connector never picks an arbitrary
admin. Goal kickoff and nomination rules are unchanged.

A reaction must match the current stored message, its channel and the configured
Discord guild. The connector fetches the user and message, checks the message's
actual bot author against the receiving bot account, rejects bots and webhook
messages, and matches the visible content against the stored summary, ignoring
trailing whitespace that Discord trims on send. This also supports cards posted
before the handler existed, without trusting an agent's current account mapping
or copying a card to a different room. The post-read authority check guards future
in-memory configuration replacement; admins currently load once at startup, so
it does not provide live revocation.

## ✅ Reached

The verdict is recorded once, with the authenticated message, user, guild,
channel and bot account. The full stored summary is sent as `goal-summary.txt`
to that human's DM, including any portion too long for the original card. The
successful send's message and channel IDs are saved in the database. Only then
are remaining open tasks dropped with reason `goal closed` and the dedicated
room deleted. Anchored rooms and rooms shared with another goal are retained.
If the goal is reopened during the DM send, cleanup stops and retains the room.

The DM shows the cleanup outcome. `goal_status <id>` includes the summary,
verdict, follow-up state and the DM jump link. The DM receipt is private to the
recipient; the goal record remains available through the existing goal tools.

A blocked DM or unconfirmed upload retains the room. A failed deletion leaves
the verdict recorded and updates the closing card (or sends a fallback notice)
and the DM archive with the failure. If Discord also refuses those notices,
the failure remains visible in `goal_status` and the engine log. A failed edit
of the final DM status does not undo a successful removal or discard its saved
summary attachment. If Discord reports successful removal but the completion
write fails, the saved DM and engine log explicitly say removal was reported by
Discord and the completion record could not be updated. That notice is attempted
even if the database remains unavailable; no message is sent into the removed
room. If storage recovers, the failure note is also saved. Otherwise the existing
journal can still say `removing`; use the saved DM and log for the observed outcome.

## ❌ Not reached

The verdict and return to `executing` are one database transaction. The human
reaction resets the goal's turn counter. The original card is edited to ask
what is missing, and ordinary messages in the room reach the goal's owning
agent. There is no synthetic approval message and no agent turn just to repeat
that question. The room and open tasks are retained.

The next completion posts a new card with a fresh undecided verdict. Previous
cards and verdicts remain in the event/delivery history and cannot decide the
new completion. A normal manual reopen keeps its existing behavior.

## Delivery and interruption

Verdict recording is serialized across engine and MCP processes with a SQLite
transaction. Duplicate reactions, opposite reactions after a verdict and
reactions on superseded cards are consumed without another Discord action or
a generic agent wake. The existing HITL gates are unchanged.

Discord and SQLite cannot be committed atomically. The durable follow-up row
starts `pending`, becomes `saved` after the external summary receipt, then
`removing` before deletion, and `complete` only after success. Handled failures
and cancellations become `failed`. A hard process death can leave `pending`,
`saved` or `removing`; `goal_status` explicitly says the follow-up or removal
is not confirmed. Inspect the room and saved DM before manual cleanup. Reactions
never automatically replay uncertain sends or removals. The summary and verdict
remain stored through restart, including earlier completion attempts.

The additive migration creates one delivery table and lookup indexes. It does
not modify existing goal records, accept a verdict, send messages or remove
channels by itself.
