"""Optional assistant-turn accounting. No prompts, responses or credentials."""

import asyncio
import json
import logging
import time
import uuid
from collections import Counter
from decimal import Decimal

import aiosqlite

from src.core.usage import TOKEN_FIELDS, count, mapping, money, table_cost, text, total

logger = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_usage (
    turn_id TEXT NOT NULL,
    call_index INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    requester_id TEXT,
    provider TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    tokens_used INTEGER,
    duration_ms INTEGER NOT NULL,
    turn_duration_ms INTEGER NOT NULL,
    provider_cost_usd TEXT,
    cost_usd TEXT,
    cost_source TEXT NOT NULL,
    model_usage TEXT NOT NULL,
    reported_usage TEXT NOT NULL,
    price_snapshot TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (turn_id, call_index)
);
CREATE INDEX IF NOT EXISTS idx_turn_usage_time ON turn_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_turn_usage_user ON turn_usage(requester_id, created_at);
CREATE TABLE IF NOT EXISTS usage_baselines (
    agent_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    cli_session_id TEXT NOT NULL,
    cost_usd TEXT,
    model_usage TEXT NOT NULL,
    session_id TEXT NOT NULL,
    message_cursor INTEGER NOT NULL,
    PRIMARY KEY (agent_id, provider, cli_session_id)
);
"""


def model_delta(current, previous):
    """Cumulative model counters are usable only if every delta is known."""
    result = {}
    for model, values in mapping(current).items():
        old = mapping(previous).get(model, dict.fromkeys(TOKEN_FIELDS, 0))
        delta = {}
        for field in TOKEN_FIELDS:
            now, before = count(mapping(values).get(field)), count(mapping(old).get(field))
            if now is None or before is None or now < before:
                return {}
            delta[field] = now - before
        result[model] = delta
    if set(mapping(previous)) - set(mapping(current)):
        return {}
    return result


class SpendLedger:
    def __init__(self, db):
        self.db = db
        self.lock = asyncio.Lock()
        self._gaps = set()

    @classmethod
    async def open(cls, path):
        db = await aiosqlite.connect(str(path))
        try:
            await db.execute("PRAGMA busy_timeout=1000")
            await db.executescript(SCHEMA)
            await db.commit()
        except BaseException:
            await db.close()
            raise
        db.row_factory = aiosqlite.Row
        return cls(db)

    async def close(self):
        await self.db.close()

    async def record(
        self,
        *,
        turn_id,
        call_index,
        session_id,
        agent_id,
        requester_id,
        provider,
        model,
        response,
        duration_ms,
        turn_duration_ms,
        prices,
    ):
        """Atomically record this call and advance its cumulative-cost baseline."""
        raw = mapping(getattr(response, "usage", None))
        usage = {k: count(raw.get(k)) for k in TOKEN_FIELDS}
        reported = money(raw.get("provider_cost_usd"))
        cost = reported
        models = {}
        cli_session = text(getattr(response, "session_id", None))
        gap_key = (agent_id, provider, session_id)
        async with self.lock:
            try:
                await self.db.execute("BEGIN IMMEDIATE")
                cursor = await self.db.execute(
                    "SELECT 1 FROM turn_usage WHERE turn_id=? AND call_index=?", (turn_id, call_index)
                )
                if await cursor.fetchone():
                    await self.db.rollback()
                    return
                if raw.get("cost_scope") == "session":
                    cursor = await self.db.execute(
                        "SELECT * FROM usage_baselines WHERE agent_id=? AND provider=? AND cli_session_id=?",
                        (agent_id, provider, cli_session),
                    )
                    prior = await cursor.fetchone()
                    fresh = raw.get("fresh_session") is True
                    baseline_cost = reported
                    baseline_models = mapping(raw.get("model_usage"))
                    old_cost = money(prior["cost_usd"]) if prior else None
                    decreased = not fresh and reported is not None and old_cost is not None and reported < old_cost
                    if decreased:
                        # A zeroed error result is not a new cumulative origin.
                        # Poison both chains, retaining the raw cost on the row.
                        baseline_cost, baseline_models = None, {}
                        prior = None
                    if prior:
                        cursor = await self.db.execute(
                            "SELECT 1 FROM messages m WHERE m.session_id=? AND m.id>? "
                            "AND m.role='assistant' AND (m.usage_call_count IS NULL OR "
                            "m.usage_call_count != (SELECT COUNT(*) FROM turn_usage u "
                            "WHERE u.turn_id=m.usage_turn_id)) LIMIT 1",
                            (prior["session_id"], prior["message_cursor"]),
                        )
                        if await cursor.fetchone() or gap_key in self._gaps:
                            prior = None
                    cost = reported if fresh else None
                    if not fresh and prior and reported is not None:
                        old_cost = money(prior["cost_usd"])
                        if old_cost is not None and reported >= old_cost:
                            cost = reported - old_cost
                    previous_models = json.loads(prior["model_usage"]) if prior else {}
                    if fresh or previous_models:
                        models = model_delta(raw.get("model_usage"), {} if fresh else previous_models)
                        if not models:
                            # Invalid/decreased model counters must not rebase a
                            # later table calculation onto zero either.
                            baseline_models = {}
                    if models:
                        usage = {k: sum(v[k] for v in models.values()) for k in TOKEN_FIELDS}
                        model = next(iter(models)) if len(models) == 1 else "multiple"
                    if cli_session:
                        # Save nulls too: a missing observation must break the
                        # chain, not move a multi-turn cost onto the next user.
                        cursor = await self.db.execute(
                            "SELECT COALESCE(MAX(id),0) FROM messages WHERE session_id=?", (session_id,)
                        )
                        message_cursor = (await cursor.fetchone())[0]
                        await self.db.execute(
                            "INSERT OR REPLACE INTO usage_baselines VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                agent_id,
                                provider,
                                cli_session,
                                str(baseline_cost) if baseline_cost is not None else None,
                                json.dumps(baseline_models),
                                session_id,
                                message_cursor,
                            ),
                        )
                else:
                    # Unparsed stdout and raised calls have neither usage nor
                    # a returned CLI id. Invalidate the stored chains for this
                    # exact engine session/provider, not other users' sessions.
                    await self.db.execute(
                        "UPDATE usage_baselines SET cost_usd=NULL, model_usage='{}' "
                        "WHERE agent_id=? AND provider=? AND session_id=?",
                        (agent_id, provider, session_id),
                    )
                    models = {model: usage} if model else {}
                source, snapshot = "provider", None
                if cost is None:
                    source = "unknown"
                    # For a resumed Claude session with no baseline, its main
                    # loop usage cannot price the unobserved subagent tree.
                    priceable = models or ({} if raw.get("cost_scope") == "session" else {model: usage})
                    amounts = [table_cost(v, provider, m, prices) for m, v in priceable.items()]
                    if amounts and all(v is not None for v in amounts):
                        cost = sum(amounts, Decimal(0))
                        source = "table"
                        snapshot = json.dumps({m: mapping(mapping(prices).get(provider)).get(m) for m in priceable})
                tokens = total(usage)
                # Preserve the provider total when token breakdown is absent
                # or incomplete. It remains unpriceable without all classes.
                legacy_total = count(getattr(response, "tokens_used", None))
                if not all(v is not None for v in usage.values()) and legacy_total is not None:
                    tokens = legacy_total
                reported_usage = {k: count(raw.get(k)) for k in TOKEN_FIELDS}
                if raw.get("cost_scope") == "session" and not models:
                    reported_usage["token_scope"] = "main_loop"
                await self.db.execute(
                    "INSERT INTO turn_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        turn_id,
                        call_index,
                        session_id,
                        agent_id,
                        requester_id,
                        provider,
                        text(model),
                        *(usage[k] for k in TOKEN_FIELDS),
                        tokens,
                        duration_ms,
                        turn_duration_ms,
                        str(reported) if reported is not None else None,
                        str(cost) if cost is not None else None,
                        source,
                        json.dumps(models),
                        json.dumps(reported_usage),
                        snapshot,
                        time.time(),
                    ),
                )
                await self.db.commit()
                self._gaps.discard(gap_key)
            except BaseException:
                self._gaps.add(gap_key)
                await self.db.rollback()
                raise

    async def rows(self, days, *, requester_id=None):
        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")
        cutoff = time.time() - days * 86400
        async with self.lock:
            sql = "SELECT * FROM turn_usage WHERE created_at >= ?"
            args = [cutoff]
            if requester_id is not None:
                sql += " AND requester_id = ?"
                args.append(str(requester_id))
            cursor = await self.db.execute(sql, args)
            rows = [dict(r) for r in await cursor.fetchall()]
            # Historical tokens have no recoverable cost or trustworthy
            # per-turn requester in shared sessions. Owner-only unknown rows.
            cursor = await self.db.execute(
                "SELECT m.id, s.agent_id, m.model, m.tokens_used FROM messages m "
                "JOIN sessions s ON s.id=m.session_id WHERE m.role='assistant' AND m.created_at >= ? "
                "AND NOT EXISTS (SELECT 1 FROM turn_usage u WHERE u.turn_id=m.usage_turn_id)"
                + ("" if requester_id is None else " AND m.usage_requester_id=?"),
                (cutoff,) if requester_id is None else (cutoff, str(requester_id)),
            )
            rows.extend(
                dict(
                    turn_id=f"legacy:{r['id']}",
                    agent_id=r["agent_id"],
                    model=r["model"],
                    tokens_used=r["tokens_used"],
                    cost_usd=None,
                    cost_source="unknown",
                    model_usage="{}",
                )
                for r in await cursor.fetchall()
            )
            # A failed optional write must not make a partially recorded turn
            # look fully priced. The message atomically retains expected calls.
            scope = "" if requester_id is None else " AND u.requester_id=?"
            cursor = await self.db.execute(
                "SELECT m.usage_turn_id, s.agent_id, m.model, m.tokens_used, SUM(u.tokens_used) AS recorded "
                "FROM messages m JOIN sessions s ON s.id=m.session_id "
                "JOIN turn_usage u ON u.turn_id=m.usage_turn_id "
                "WHERE m.created_at >= ?" + scope + " GROUP BY m.id HAVING COUNT(*) < m.usage_call_count",
                (cutoff,) if requester_id is None else (cutoff, str(requester_id)),
            )
            rows.extend(
                dict(
                    turn_id=r["usage_turn_id"],
                    agent_id=r["agent_id"],
                    model=r["model"],
                    tokens_used=max(0, (r["tokens_used"] or 0) - (r["recorded"] or 0)),
                    cost_usd=None,
                    cost_source="unknown",
                    model_usage="{}",
                )
                for r in await cursor.fetchall()
            )
            return rows


class TurnMeter:
    """Capture every outer provider round; all optional accounting is best effort."""

    def __init__(self, manager, session_id, agent_id, requester_id):
        self.manager = manager
        self.session_id, self.agent_id = session_id, agent_id
        self.requester_id = str(requester_id) if requester_id is not None else None
        self.id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.calls = 0
        self.tokens = None
        self.provider = "unknown"

    async def complete(self, llm, *args, **kwargs):
        self.calls += 1
        self.provider = next((n for n, p in self.manager.llm_providers.items() if p is llm), "unknown")
        started = time.monotonic()
        response = None
        try:
            response = await llm.complete(*args, **kwargs)
            return response
        finally:
            # Do not catch cancellation of the turn. Provider errors are still
            # recorded as unknown; accounting errors cannot replace the error.
            try:
                tokens = count(getattr(response, "tokens_used", None))
                if tokens is not None:
                    self.tokens = (self.tokens or 0) + tokens
                ledger = getattr(self.manager.storage, "spend", None)
                if ledger:
                    await ledger.record(
                        turn_id=self.id,
                        call_index=self.calls,
                        session_id=self.session_id,
                        agent_id=self.agent_id,
                        requester_id=self.requester_id,
                        provider=self.provider,
                        model=getattr(response, "model", None) or kwargs.get("model"),
                        response=response,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        turn_duration_ms=int((time.monotonic() - self.started) * 1000),
                        prices=mapping(self.manager.defaults.get("spend")).get("prices", {}),
                    )
            except Exception as exc:
                logger.warning("Spend capture unavailable (%s)", type(exc).__name__)


def usd(amount):
    if 0 < amount < Decimal("0.0001"):
        return "<$0.0001"
    return f"${amount:.4f}"


def label(value, width):
    # Keep untrusted provider model names inside the code block, one line.
    value = "".join(c if c.isascii() and (c.isalnum() or c in "._/-") else "_" for c in str(value or "unknown"))
    return value if len(value) <= width else value[: width - 1] + "~"


def render_spend(rows, days, *, own=False):
    agents = {}
    sources = Counter()
    for row in rows:
        agent = agents.setdefault(
            row["agent_id"],
            dict(
                turns=set(), unknown=set(), tokens=0, tokens_missing=False, cost=Decimal(0), known=0, models=Counter()
            ),
        )
        turn = row["turn_id"]
        agent["turns"].add(turn)
        measured = count(row.get("tokens_used"))
        token_scope = json.loads(row.get("reported_usage") or "{}").get("token_scope")
        agent["tokens_missing"] |= measured is None or token_scope == "main_loop"
        tokens = measured or 0
        agent["tokens"] += tokens
        amount = money(row.get("cost_usd"))
        if amount is None:
            agent["unknown"].add(turn)
        else:
            agent["cost"] += amount
            agent["known"] += 1
        sources[row.get("cost_source", "unknown")] += 1
        models = json.loads(row.get("model_usage") or "{}")
        if models:
            for model, usage in models.items():
                agent["models"][model] += total(usage) or 0
        else:
            agent["models"][row.get("model") or "unknown"] += tokens
    heading = f"Last {days} days · {'your initiated turns' if own else 'all agents'}"
    lines = [f"{'Agent':<20} {'Turns':>6} {'Tokens':>12} {'List-price USD':>19}  Busiest model"]
    ordered = sorted(agents.items(), key=lambda v: (v[1]["known"] == 0, -v[1]["cost"], -v[1]["tokens"], v[0]))
    for name, values in ordered:
        amount = usd(values["cost"]) if values["known"] else "unknown"
        if values["known"] and values["unknown"]:
            amount += " + ?"
        model = sorted(values["models"].items(), key=lambda v: (-v[1], v[0]))[0][0]
        token_label = f"{values['tokens']:,}" + (" + ?" if values["tokens_missing"] else "")
        lines.append(
            f"{label(name, 20):<20} {len(values['turns']):>6} {token_label:>12} {amount:>19}  {label(model, 28)}"
        )
    cost = sum((a["cost"] for a in agents.values()), Decimal(0))
    unknown = sum(len(a["unknown"]) for a in agents.values())
    known = sum(a["known"] for a in agents.values())
    amount = usd(cost) if known or not rows else "unknown"
    if known and unknown:
        amount += " + ?"
    token_label = f"{sum(a['tokens'] for a in agents.values()):,}"
    if any(a["tokens_missing"] for a in agents.values()):
        token_label += " + ?"
    lines.append(f"{'TOTAL':<20} {sum(len(a['turns']) for a in agents.values()):>6} {token_label:>12} {amount:>19}")
    return (
        heading + "\n```text\n" + "\n".join(lines) + "\n```\n"
        f"Unknown-cost turns: {unknown}.\n"
        f"Cost sources: provider {sources['provider']}, configured table {sources['table']}, "
        f"unknown {sources['unknown']}.\n"
        "USD is a list-price equivalent, not a subscription bill.\n"
        "Recorded assistant turns only; background jobs and unreported usage are excluded."
        + (
            "\nToken + ? includes Claude turns whose subagent tokens are unconfirmed."
            if any(json.loads(r.get("reported_usage") or "{}").get("token_scope") == "main_loop" for r in rows)
            else ""
        )
    )
