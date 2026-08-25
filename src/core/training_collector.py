"""Training-data collection — append-only capture of agent turns for later fine-tuning.

Records each completed turn as {input (assembled prompt), response, full tool-call
trace, metadata, outcome} to ``<path>/turns.jsonl``, and discrete human 👍/👎 rewards
to ``<path>/rewards.jsonl`` (joined to turns by reply_message_id at export time). The
tool-call trace is recovered from Claude Code's per-session transcript
(``session_transcript_path``) — the complete, faithful record of every tool_use +
tool_result + assistant text.

Mirrors src/core/audit.py: threading.Lock (called from async core + sync contexts),
line-buffered append, atexit close, every write wrapped so a failure NEVER breaks a
turn. Secrets are redacted (reuses audit._redact). Opt-in (default off) because it
stores full conversation content locally.
"""

import atexit
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.core.audit import _redact, scrub_value
from src.llm.claude_code import session_transcript_path

logger = logging.getLogger(__name__)

_MAX_FIELD = 4000       # truncate long tool inputs/outputs/text blocks
_MAX_INPUT = 20000      # truncate the assembled prompt / response
_MAX_STEPS = 200        # cap tool steps captured per turn
# Turns to scan back through when attributing a reward to an agent. A reaction
# lands on something recent; reading the whole file to answer a thumbs-up would
# cost more than the signal is worth.
_REWARD_LOOKBACK = 500


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(obj, limit=_MAX_FIELD):
    """Recursively cap long strings so a huge file-write input can't bloat a record."""
    if isinstance(obj, str):
        return obj[:limit] + "…" if len(obj) > limit else obj
    if isinstance(obj, dict):
        return {k: _truncate(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate(v, limit) for v in obj[:50]]
    return obj


def _tools_schema(tools) -> list[dict]:
    """Serialize the ToolDefs offered to the model this turn into OpenAI function
    schemas — the same shape the local provider sends at inference. Recorded so an
    exported example can carry the available-tool menu the model conditioned on
    (train/inference parity), which is what makes tool-use fine-tunes learn. Best-
    effort: any failure yields [] rather than breaking the turn."""
    if not tools:
        return []
    try:
        from src.llm.openai_compat import _to_openai_tools
        return _to_openai_tools(tools)[:_MAX_STEPS]
    except Exception as e:
        logger.debug(f"tools_schema serialize failed: {e}")
        return []


def _count_lines(path: Path) -> int:
    """Rows in a JSONL file. Counted rather than loaded — turns.jsonl reaches
    tens of megabytes and this runs from a chat command."""
    try:
        with open(path, "rb") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def training_status(path: str | Path, include_tool_trace: bool | None = None) -> dict:
    """What is actually on disk for a training directory.

    Reports the RESOLVED absolute path, because the configured value is
    routinely relative (`data_dir: ./data`) and therefore resolves against the
    service's working directory rather than anywhere a person would look. Three
    people hunting for turns.jsonl by hand, one of them failing entirely, is
    what this exists to stop.

    Safe to call when collection is off — it describes the directory the config
    points at, whether or not anything has ever written there.
    """
    d = Path(path).expanduser().resolve()
    turns, rewards = d / "turns.jsonl", d / "rewards.jsonl"
    judgments = d / "judgments.jsonl"
    return {
        "dir": str(d),
        "dir_exists": d.is_dir(),
        "turns": _count_lines(turns) if turns.exists() else 0,
        "turns_bytes": turns.stat().st_size if turns.exists() else 0,
        "turns_mtime": (datetime.fromtimestamp(turns.stat().st_mtime, timezone.utc)
                        .isoformat(timespec="seconds") if turns.exists() else None),
        # A missing rewards file is the normal state before anyone reacts, and
        # it is indistinguishable from a broken reward path unless we say which.
        "rewards_file_exists": rewards.exists(),
        "rewards": _count_lines(rewards) if rewards.exists() else 0,
        "judgments_file_exists": judgments.exists(),
        "judgments": _count_lines(judgments) if judgments.exists() else 0,
        "include_tool_trace": include_tool_trace,
    }


class TrainingCollector:
    """Append-only per-turn training-data writer (turns.jsonl + rewards.jsonl)."""

    def __init__(self, path: str | Path, include_tool_trace: bool = True):
        self._dir = Path(path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._turns_path = self._dir / "turns.jsonl"
        self._rewards_path = self._dir / "rewards.jsonl"
        self._include_tool_trace = include_tool_trace
        self._lock = threading.Lock()
        self._files: dict[str, object] = {}
        # cli_session_id -> transcript lines already consumed (per-turn slicing)
        self._offsets: dict[str, int] = {}
        atexit.register(self.close)

    def status(self) -> dict:
        """What this collector has actually written. See training_status()."""
        return training_status(self._dir, self._include_tool_trace)

    def _write(self, path: Path, entry: dict) -> None:
        try:
            with self._lock:
                f = self._files.get(str(path))
                if f is None or f.closed:
                    f = open(path, "a", buffering=1)
                    self._files[str(path)] = f
                    try:
                        path.chmod(0o600)  # full transcripts — keep owner-only
                    except OSError:
                        pass
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:  # never let logging break a turn
            logger.debug(f"training write failed: {e}")

    def record_turn(self, *, agent_id, session, message, user_content, response,
                    project_dir=None, reply_message_id=None, recalled_lessons=None,
                    tools_available=None) -> None:
        """Record one completed agent turn. Never raises."""
        try:
            cli_sid = getattr(session, "cli_session_id", None)
            tools = self._extract_tools(project_dir, cli_sid) if self._include_tool_trace else []
            entry = {
                "turn_id": uuid.uuid4().hex,
                "ts": _iso_now(),
                "agent": agent_id,
                "session_id": getattr(session, "id", None),
                "cli_session_id": cli_sid,
                "connector": getattr(message, "connector", None),
                "channel_id": getattr(message, "channel_id", None),
                "user_id": getattr(message, "user_id", None),
                "skill": getattr(message, "skill", None),
                "reply_message_id": str(reply_message_id) if reply_message_id else None,
                "input": scrub_value(user_content or "")[:_MAX_INPUT],
                "response": {
                    "content": scrub_value(getattr(response, "content", "") or "")[:_MAX_INPUT],
                    "model": getattr(response, "model", None),
                    "tokens_used": getattr(response, "tokens_used", None),
                    "stop_reason": getattr(response, "stop_reason", None),
                },
                "recalled_lessons": list(recalled_lessons or []),
                "tools_available": _tools_schema(tools_available),
                "tools": tools,
                "outcome": self._derive_outcome(response, tools),
            }
            self._write(self._turns_path, entry)
        except Exception as e:
            logger.debug(f"record_turn failed: {e}")

    def record_reward(self, reply_message_id, agent_id, signal, user_id=None) -> None:
        """Record a discrete human reward (👍/👎) keyed by reply_message_id.

        The caller resolves the agent through feedback_map, which is only
        written for replies that recalled a lesson: 32 of 1163 turns on the
        live store. Every other reward landed with agent=null. The export path
        survives that, because it joins to the turn on reply_message_id, but
        the file is unreadable on its own and no per-agent reward count is
        possible without redoing the join by hand. The turn record already
        holds the answer, so look it up rather than storing a null.
        """
        if not reply_message_id:
            return
        if not agent_id:
            agent_id = self._agent_for_reply(str(reply_message_id))
        self._write(self._rewards_path, {
            "ts": _iso_now(),
            "reply_message_id": str(reply_message_id),
            "agent": agent_id,
            "signal": signal,          # "up" | "down"
            "user_id": user_id,
        })

    def _agent_for_reply(self, reply_message_id: str) -> str | None:
        """Which agent sent this reply, from the tail of turns.jsonl.

        Scans backwards over a bounded window: a reaction lands on something
        recent, and reading a 40MB file forwards to answer a thumbs-up would
        cost more than the signal is worth.
        """
        try:
            with open(self._turns_path, errors="replace") as fh:
                tail = fh.readlines()[-_REWARD_LOOKBACK:]
        except OSError:
            return None
        for line in reversed(tail):
            try:
                turn = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if str(turn.get("reply_message_id") or "") == reply_message_id:
                return turn.get("agent")
        return None

    def _extract_tools(self, project_dir, cli_sid) -> list[dict]:
        """Parse this turn's tool_use / tool_result / assistant-text steps from the
        Claude Code transcript, slicing only the lines new since the previous turn."""
        if not (project_dir and cli_sid):
            return []
        path = session_transcript_path(project_dir, cli_sid)
        if not path.exists():
            return []
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return []
        start = self._offsets.get(cli_sid, 0)
        self._offsets[cli_sid] = len(lines)
        steps: list[dict] = []
        for ln in lines[start:]:
            if not ln.strip():
                continue
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                continue
            content = (ev.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "tool_use":
                    steps.append({"type": "tool_use", "name": block.get("name"),
                                  "input": _truncate(_redact(block.get("input") or {}))})
                elif bt == "tool_result":
                    out = block.get("content")
                    if isinstance(out, list):
                        out = " ".join(str(b.get("text", "")) for b in out if isinstance(b, dict))
                    steps.append({"type": "tool_result", "is_error": bool(block.get("is_error")),
                                  "output": scrub_value(str(out) if out is not None else "")[:_MAX_FIELD]})
                elif bt == "text" and (block.get("text") or "").strip():
                    steps.append({"type": "text", "text": scrub_value(block["text"])[:_MAX_FIELD]})
                if len(steps) >= _MAX_STEPS:
                    return steps
        return steps

    def _derive_outcome(self, response, tools) -> dict:
        stop = getattr(response, "stop_reason", None)
        return {
            "stop_reason": stop,
            "degraded": stop in ("error", "usage_limit", "auth_error"),
            "tool_calls": sum(1 for t in tools if t.get("type") == "tool_use"),
            "tool_errors": sum(1 for t in tools if t.get("type") == "tool_result" and t.get("is_error")),
        }

    def close(self) -> None:
        with self._lock:
            for f in self._files.values():
                try:
                    if f and not f.closed:
                        f.close()
                except Exception:
                    pass
            self._files.clear()
