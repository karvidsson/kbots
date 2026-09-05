"""Goal workstreams — durable multi-agent collaboration toward a shared goal.

A Goal is a SQLite record anchored to one Discord channel. Participating
agents (each with its own bot account) are routed into that channel
dynamically from this store — no agents.yaml edits, no restart. The store
lives in data/goals.db and is opened directly by both the engine and the
MCP tool subprocess (WAL mode), the same pattern as src/memory/sqlite.py.

Lifecycle: proposed → brainstorm → strategy → executing → done, with
paused / blocked_on_user side-states. Every write bumps last_activity_at
and appends a goal_events row — that feed is the audit trail and the
stall-detection signal.
"""

import json
import logging
import re
import sqlite3
import time
from pathlib import Path

from src.core.base import PROJECT_ROOT

logger = logging.getLogger(__name__)

DB_PATH = "data/goals.db"

# Statuses in which the goal is actively worked (turn budget applies).
ACTIVE_STATUSES = ("brainstorm", "strategy", "executing")
# Statuses in which participants stay routed to the goal channel — paused and
# blocked goals must still hear scheduler wakes and the user's replies.
ROUTED_STATUSES = ("proposed", "brainstorm", "strategy", "executing",
                   "paused", "blocked_on_user")

_TRANSITIONS: dict[str, set[str]] = {
    "proposed":        {"brainstorm", "abandoned"},
    "brainstorm":      {"strategy", "paused", "blocked_on_user", "abandoned"},
    "strategy":        {"executing", "brainstorm", "paused", "blocked_on_user", "abandoned"},
    "executing":       {"paused", "blocked_on_user", "done", "abandoned"},
    "paused":          {"brainstorm", "strategy", "executing", "blocked_on_user", "abandoned"},
    "blocked_on_user": {"brainstorm", "strategy", "executing", "paused", "abandoned"},
}

DECISION_KINDS = ("pause", "abandon", "strategy_change")
TASK_STATUSES = ("open", "doing", "done", "dropped")

_db: sqlite3.Connection | None = None
# TTL cache for the two hot-path lookups (per-message in the connector).
_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 10.0


def _get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        path = Path(DB_PATH)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(str(path), check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA busy_timeout=5000")
        _db.execute("PRAGMA synchronous=NORMAL")
        _ensure_schema(_db)
    return _db


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS goals (
            id              TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            description     TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'proposed',
            owner_agent     TEXT NOT NULL,
            connector       TEXT NOT NULL DEFAULT 'discord',
            channel_id      TEXT NOT NULL,
            created_by      TEXT NOT NULL,
            strategy        TEXT NOT NULL DEFAULT '',
            turn_budget     INTEGER NOT NULL DEFAULT 30,
            pause_reason    TEXT NOT NULL DEFAULT '',
            wake_condition  TEXT NOT NULL DEFAULT '',
            wake_ref        TEXT NOT NULL DEFAULT '',
            blocked_brief   TEXT NOT NULL DEFAULT '',
            card_message_id TEXT NOT NULL DEFAULT '',
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL,
            last_activity_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_goals_channel ON goals(channel_id, status);

        CREATE TABLE IF NOT EXISTS goal_participants (
            goal_id   TEXT NOT NULL,
            agent_id  TEXT NOT NULL,
            role      TEXT NOT NULL DEFAULT 'member',
            joined_at REAL NOT NULL,
            PRIMARY KEY (goal_id, agent_id)
        );

        CREATE TABLE IF NOT EXISTS goal_tasks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id    TEXT NOT NULL,
            title      TEXT NOT NULL,
            detail     TEXT NOT NULL DEFAULT '',
            assignee   TEXT NOT NULL DEFAULT '',
            status     TEXT NOT NULL DEFAULT 'open',
            created_by TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_goal ON goal_tasks(goal_id, status);

        CREATE TABLE IF NOT EXISTS goal_decisions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id        TEXT NOT NULL,
            kind           TEXT NOT NULL,
            proposed_by    TEXT NOT NULL,
            reason         TEXT NOT NULL,
            wake_condition TEXT NOT NULL DEFAULT '',
            status         TEXT NOT NULL DEFAULT 'open',
            window_ends_at REAL NOT NULL,
            decided_by     TEXT NOT NULL DEFAULT '',
            decided_at     REAL,
            created_at     REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS goal_votes (
            decision_id INTEGER NOT NULL,
            agent_id    TEXT NOT NULL,
            stance      TEXT NOT NULL,
            reason      TEXT NOT NULL DEFAULT '',
            ts          REAL NOT NULL,
            PRIMARY KEY (decision_id, agent_id)
        );

        CREATE TABLE IF NOT EXISTS goal_events (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id  TEXT NOT NULL,
            ts       REAL NOT NULL,
            agent_id TEXT NOT NULL,
            kind     TEXT NOT NULL,
            payload  TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_events_goal ON goal_events(goal_id, ts);
    """)
    # `anchored` = the goal is borrowing the proposer's home channel because
    # its proposer's tier could not create one. It is a fact about the channel,
    # not about the tier, so it cannot be re-derived later: the proposer may be
    # promoted, and a coordinator can also anchor when guild_id is unset.
    # Existing rows are anchored by definition — until this column existed,
    # nothing could give a goal a channel after creation.
    cols = {r[1] for r in db.execute("PRAGMA table_info(goals)")}
    if "anchored" not in cols:
        db.execute("ALTER TABLE goals ADD COLUMN anchored INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE goals SET anchored = 1 WHERE status = 'proposed'")
    db.commit()


def _invalidate_cache() -> None:
    _cache.clear()


def _cached(key: str, compute):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    value = compute()
    _cache[key] = (now, value)
    return value


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return "-".join(slug.split("-")[:4]) or "goal"


def log_event(goal_id: str, agent_id: str, kind: str, payload: str = "") -> None:
    db = _get_db()
    now = time.time()
    db.execute(
        "INSERT INTO goal_events (goal_id, ts, agent_id, kind, payload) VALUES (?,?,?,?,?)",
        (goal_id, now, agent_id, kind, payload))
    db.execute("UPDATE goals SET last_activity_at=? WHERE id=?", (now, goal_id))
    db.commit()
    _invalidate_cache()


# --- goals ---

def create_goal(title: str, description: str, owner_agent: str, channel_id: str,
                created_by: str, *, connector: str = "discord",
                turn_budget: int = 30, status: str = "proposed",
                anchored: bool = False) -> dict:
    db = _get_db()
    base = f"g-{_slugify(title)}"
    goal_id = base
    n = 2
    while db.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,)).fetchone():
        goal_id = f"{base}-{n}"
        n += 1
    now = time.time()
    db.execute(
        """INSERT INTO goals (id, title, description, status, owner_agent, connector,
           channel_id, created_by, turn_budget, anchored, created_at, updated_at,
           last_activity_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (goal_id, title.strip(), description.strip(), status, owner_agent,
         connector, str(channel_id), created_by, int(turn_budget),
         int(bool(anchored)), now, now, now))
    db.execute(
        "INSERT OR REPLACE INTO goal_participants (goal_id, agent_id, role, joined_at) "
        "VALUES (?,?,?,?)", (goal_id, owner_agent, "owner", now))
    db.commit()
    _invalidate_cache()
    log_event(goal_id, owner_agent, "created", title.strip())
    return get_goal(goal_id)  # type: ignore[return-value]


def get_goal(goal_id: str) -> dict | None:
    row = _get_db().execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
    return dict(row) if row else None


def get_goal_by_channel(channel_id: str,
                        statuses: tuple = ROUTED_STATUSES) -> dict | None:
    q = ",".join("?" * len(statuses))
    row = _get_db().execute(
        f"SELECT * FROM goals WHERE channel_id=? AND status IN ({q}) "
        f"ORDER BY updated_at DESC LIMIT 1", (str(channel_id), *statuses)).fetchone()
    return dict(row) if row else None


def list_goals(statuses: tuple | None = None) -> list[dict]:
    db = _get_db()
    if statuses:
        q = ",".join("?" * len(statuses))
        rows = db.execute(
            f"SELECT * FROM goals WHERE status IN ({q}) ORDER BY updated_at DESC",
            statuses).fetchall()
    else:
        rows = db.execute("SELECT * FROM goals ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def attach_channel(goal_id: str, actor: str, channel_id: str) -> dict:
    """Give an anchored goal its own channel, once.

    Deliberately not a field on update_goal: that is reachable from goal_set,
    and moving a live goal's channel by setting a field would strand every
    message and task already posted in the old one. This only ever fires on the
    move from borrowed to owned, and refuses afterwards.
    """
    goal = get_goal(goal_id)
    if not goal:
        raise ValueError(f"unknown goal '{goal_id}'")
    if not goal.get("anchored"):
        raise ValueError(f"goal '{goal_id}' already has its own channel")
    if not str(channel_id).strip():
        raise ValueError("channel_id is empty")
    now = time.time()
    db = _get_db()
    db.execute("UPDATE goals SET channel_id=?, anchored=0, card_message_id='', "
               "updated_at=?, last_activity_at=? WHERE id=?",
               (str(channel_id).strip(), now, now, goal_id))
    db.commit()
    _invalidate_cache()
    log_event(goal_id, actor, "channel", f"{goal['channel_id']} → {channel_id}")
    return get_goal(goal_id)  # type: ignore[return-value]


def update_goal(goal_id: str, actor: str, **fields) -> dict:
    """Patch goal fields. Status changes go through _TRANSITIONS.

    Raises ValueError on unknown goal or illegal transition.
    """
    goal = get_goal(goal_id)
    if not goal:
        raise ValueError(f"unknown goal '{goal_id}'")
    allowed = {"status", "strategy", "title", "description", "turn_budget",
               "owner_agent", "pause_reason", "wake_condition", "wake_ref",
               "blocked_brief", "card_message_id"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot set field(s): {', '.join(sorted(unknown))}")
    new_status = fields.get("status")
    if new_status and new_status != goal["status"]:
        if new_status not in _TRANSITIONS.get(goal["status"], set()):
            raise ValueError(
                f"illegal transition {goal['status']} → {new_status} "
                f"(allowed: {', '.join(sorted(_TRANSITIONS.get(goal['status'], set()))) or 'none'})")
    now = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    db = _get_db()
    db.execute(f"UPDATE goals SET {sets}, updated_at=?, last_activity_at=? WHERE id=?",
               (*fields.values(), now, now, goal_id))
    db.commit()
    _invalidate_cache()
    if new_status and new_status != goal["status"]:
        log_event(goal_id, actor, "status", f"{goal['status']} → {new_status}")
    return get_goal(goal_id)  # type: ignore[return-value]


# --- participants ---

def add_participant(goal_id: str, agent_id: str, role: str = "member") -> None:
    db = _get_db()
    db.execute(
        "INSERT OR IGNORE INTO goal_participants (goal_id, agent_id, role, joined_at) "
        "VALUES (?,?,?,?)", (goal_id, agent_id, role, time.time()))
    db.commit()
    _invalidate_cache()
    log_event(goal_id, agent_id, "joined", role)


def list_participants(goal_id: str) -> list[dict]:
    rows = _get_db().execute(
        "SELECT * FROM goal_participants WHERE goal_id=? ORDER BY joined_at",
        (goal_id,)).fetchall()
    return [dict(r) for r in rows]


# --- tasks ---

def add_task(goal_id: str, title: str, detail: str, assignee: str,
             created_by: str) -> dict:
    db = _get_db()
    now = time.time()
    cur = db.execute(
        "INSERT INTO goal_tasks (goal_id, title, detail, assignee, created_by, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (goal_id, title.strip(), detail.strip(), assignee, created_by, now, now))
    db.commit()
    _invalidate_cache()
    log_event(goal_id, created_by, "task", f"#{cur.lastrowid} added: {title.strip()}")
    return {"id": cur.lastrowid, "title": title.strip()}


def update_task(task_id: int, actor: str, *, status: str = "",
                assignee: str | None = None) -> dict:
    db = _get_db()
    row = db.execute("SELECT * FROM goal_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown task #{task_id}")
    if status and status not in TASK_STATUSES:
        raise ValueError(f"task status must be one of: {', '.join(TASK_STATUSES)}")
    fields: dict = {}
    if status:
        fields["status"] = status
    if assignee is not None:
        fields["assignee"] = assignee
    if not fields:
        return dict(row)
    sets = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE goal_tasks SET {sets}, updated_at=? WHERE id=?",
               (*fields.values(), time.time(), task_id))
    db.commit()
    _invalidate_cache()
    log_event(row["goal_id"], actor, "task",
              f"#{task_id} {status or 'assigned'}"
              + (f" → {assignee}" if assignee is not None else ""))
    return dict(db.execute("SELECT * FROM goal_tasks WHERE id=?", (task_id,)).fetchone())


def list_tasks(goal_id: str, statuses: tuple = ("open", "doing")) -> list[dict]:
    q = ",".join("?" * len(statuses))
    rows = _get_db().execute(
        f"SELECT * FROM goal_tasks WHERE goal_id=? AND status IN ({q}) ORDER BY id",
        (goal_id, *statuses)).fetchall()
    return [dict(r) for r in rows]


# --- decisions & votes ---

def create_decision(goal_id: str, kind: str, proposed_by: str, reason: str,
                    wake_condition: str, window_ends_at: float) -> dict:
    if kind not in DECISION_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(DECISION_KINDS)}")
    db = _get_db()
    cur = db.execute(
        "INSERT INTO goal_decisions (goal_id, kind, proposed_by, reason, "
        "wake_condition, window_ends_at, created_at) VALUES (?,?,?,?,?,?,?)",
        (goal_id, kind, proposed_by, reason.strip(), wake_condition,
         window_ends_at, time.time()))
    db.commit()
    _invalidate_cache()
    log_event(goal_id, proposed_by, "proposal", f"#{cur.lastrowid} {kind}: {reason.strip()}")
    return get_decision(cur.lastrowid)  # type: ignore[return-value]


def get_decision(decision_id: int) -> dict | None:
    row = _get_db().execute(
        "SELECT * FROM goal_decisions WHERE id=?", (decision_id,)).fetchone()
    return dict(row) if row else None


def open_decisions(goal_id: str) -> list[dict]:
    rows = _get_db().execute(
        "SELECT * FROM goal_decisions WHERE goal_id=? AND status='open' ORDER BY id",
        (goal_id,)).fetchall()
    return [dict(r) for r in rows]


def vote(decision_id: int, agent_id: str, stance: str, reason: str) -> dict:
    if stance not in ("support", "object"):
        raise ValueError("stance must be 'support' or 'object'")
    dec = get_decision(decision_id)
    if not dec:
        raise ValueError(f"unknown decision #{decision_id}")
    if dec["status"] != "open":
        raise ValueError(f"decision #{decision_id} is already {dec['status']}")
    db = _get_db()
    db.execute(
        "INSERT OR REPLACE INTO goal_votes (decision_id, agent_id, stance, reason, ts) "
        "VALUES (?,?,?,?,?)", (decision_id, agent_id, stance, reason.strip(), time.time()))
    db.commit()
    _invalidate_cache()
    log_event(dec["goal_id"], agent_id, "vote", f"#{decision_id} {stance}: {reason.strip()}")
    return dec


def list_votes(decision_id: int) -> list[dict]:
    rows = _get_db().execute(
        "SELECT * FROM goal_votes WHERE decision_id=? ORDER BY ts", (decision_id,)).fetchall()
    return [dict(r) for r in rows]


def decide(decision_id: int, decided_by: str, outcome: str) -> dict:
    if outcome not in ("adopted", "rejected", "withdrawn"):
        raise ValueError("outcome must be adopted, rejected, or withdrawn")
    dec = get_decision(decision_id)
    if not dec:
        raise ValueError(f"unknown decision #{decision_id}")
    if dec["status"] != "open":
        raise ValueError(f"decision #{decision_id} is already {dec['status']}")
    db = _get_db()
    db.execute(
        "UPDATE goal_decisions SET status=?, decided_by=?, decided_at=? WHERE id=?",
        (outcome, decided_by, time.time(), decision_id))
    db.commit()
    _invalidate_cache()
    log_event(dec["goal_id"], decided_by, "decision", f"#{decision_id} {outcome}")
    return get_decision(decision_id)  # type: ignore[return-value]


# --- engine-facing hot-path helpers (TTL-cached) ---

def active_goal_for_channel(channel_id: str) -> dict | None:
    """Goal actively worked in this channel — used for the turn-budget override."""
    channel_id = str(channel_id)
    return _cached(f"active:{channel_id}",
                   lambda: get_goal_by_channel(channel_id, ACTIVE_STATUSES))


def routed_participants_for_channel(channel_id: str) -> list[str]:
    """Agent ids routed into this channel because of a live (non-terminal) goal."""
    channel_id = str(channel_id)

    def compute() -> list[str]:
        goal = get_goal_by_channel(channel_id, ROUTED_STATUSES)
        if not goal:
            return []
        return [p["agent_id"] for p in list_participants(goal["id"])]

    return _cached(f"routed:{channel_id}", compute)


# --- context injection ---

_PHASE_PROTOCOL = {
    "proposed": (
        "Proposed — not yet started. A coordinator-tier agent or a human must "
        "advance it (goal_set status=brainstorm). Do not start working on it yet."),
    "brainstorm": (
        "Brainstorm phase: free discussion toward a strategy is welcome within "
        "the turn budget. Owner: when a direction has emerged, record it with "
        "goal_set strategy=... then goal_set status=strategy."),
    "strategy": (
        "Strategy phase: converge on the recorded strategy and break it into "
        "tasks with goal_task. Owner: when tasks are set, goal_set "
        "status=executing."),
    "executing": (
        "Executing phase. Speak only if @mentioned, on a task event, or with "
        "genuinely new information — otherwise reply exactly NO_REPLY. Use "
        "goal_* tools for all state changes; route questions through "
        "goal_task/goal_update rather than free chat. Summarize any ask_agent "
        "sidebar outcome back into this channel."),
    "paused": (
        "PAUSED — do not work on this goal or chat about it. Speak only to "
        "resume (goal_resume, when the wake condition is met) or when a human "
        "posts. Otherwise reply exactly NO_REPLY."),
    "blocked_on_user": (
        "BLOCKED on the user. If the user has replied: owner addresses the "
        "items and calls goal_resume; everyone else replies exactly NO_REPLY "
        "unless directly addressed. If the user has not replied, reply exactly "
        "NO_REPLY."),
}


def build_goal_context(agent_id: str, channel_id: str) -> str | None:
    """The per-turn <goal-context> block for a participating agent. Short by
    design (~350 tokens max) — built from columns, no LLM call."""
    channel_id = str(channel_id)

    def compute() -> dict | None:
        return get_goal_by_channel(channel_id, ROUTED_STATUSES)

    goal = _cached(f"ctx:{channel_id}", compute)
    if not goal:
        return None

    participants = list_participants(goal["id"])
    part_ids = {p["agent_id"] for p in participants}
    you = ("owner" if goal["owner_agent"] == agent_id
           else "member" if agent_id in part_ids
           else "not a participant (join with goal_join)")

    domains = _team_domains()
    plist = ", ".join(
        f"{p['agent_id']} ({p['role']}"
        + (f", {domains[p['agent_id']]}" if p["agent_id"] in domains else "")
        + ")"
        for p in participants)

    lines = [
        f'<goal-context id="{goal["id"]}" status="{goal["status"]}">',
        f"Goal: {goal['title']}. Owner: {goal['owner_agent']}. You are: {you}.",
    ]
    if goal["strategy"]:
        lines.append(f"Strategy: {goal['strategy'][:200]}")
    lines.append(f"Participants: {plist}")

    tasks = list_tasks(goal["id"])
    if tasks:
        shown = tasks[:5]
        tline = "; ".join(
            f"#{t['id']} {t['title'][:40]}"
            + (f" ({t['assignee']}, {t['status']})" if t["assignee"] else f" ({t['status']})")
            for t in shown)
        extra = f"; (+{len(tasks) - 5} more)" if len(tasks) > 5 else ""
        lines.append(f"Open tasks: {tline}{extra}")

    for dec in open_decisions(goal["id"])[:3]:
        votes = list_votes(dec["id"])
        tally = (f"{sum(1 for v in votes if v['stance'] == 'support')} support / "
                 f"{sum(1 for v in votes if v['stance'] == 'object')} object")
        ends = time.strftime("%Y-%m-%d %H:%M", time.localtime(dec["window_ends_at"]))
        lines.append(
            f"Open decision #{dec['id']}: {dec['kind'].upper()} proposed by "
            f"{dec['proposed_by']} (\"{dec['reason'][:80]}\") — {tally}, window "
            f"closes {ends}. Vote with goal_vote({dec['id']}, ...); owner closes "
            f"with goal_decide.")

    if goal["status"] == "paused":
        lines.append(f"Pause reason: {goal['pause_reason'][:150]}")
        if goal["wake_condition"]:
            lines.append(f"Wake condition: {goal['wake_condition'][:150]}")
    if goal["status"] == "blocked_on_user" and goal["blocked_brief"]:
        try:
            brief = json.loads(goal["blocked_brief"])
            need = "; ".join(brief.get("do", [])[:3])
            lines.append(f"Waiting on the user for: {need[:200]}")
        except (json.JSONDecodeError, TypeError):
            pass

    protocol = _PHASE_PROTOCOL.get(goal["status"], "")
    if goal["status"] in ACTIVE_STATUSES:
        protocol += (f" Turn budget: {goal['turn_budget']} agent turns between "
                     f"human check-ins.")
    lines.append(f"Protocol: {protocol}")
    lines.append("</goal-context>")
    return "\n".join(lines)


def _team_domains() -> dict[str, str]:
    """agent_id → domain from team.json, best-effort."""
    try:
        from src.tools.team import _load_team
        return {a.get("id", ""): a.get("domain", "")
                for a in _load_team().get("agents", []) if a.get("domain")}
    except Exception:
        return {}
