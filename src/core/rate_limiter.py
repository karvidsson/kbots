"""Rate limiter — per-tool, per-agent sliding window.

Counters are in-memory (reset on restart — acceptable for rate limiting).
Uses deque with maxlen for automatic old-entry eviction.
"""

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)


class RateLimiter:
    """Sliding window rate limiter for tool calls."""

    def __init__(self, config: dict):
        self.config = config
        self.mode = config.get("mode", "enforce")  # enforce | log
        self.defaults = config.get("defaults", {"max_per_hour": 50})
        self.tool_limits = config.get("tools", {})
        self.agent_overrides = config.get("agent_overrides", {})

        # Counters: (agent_id, tool_name) -> deque of timestamps
        self._counters: dict[tuple[str, str], deque] = {}

    def check(self, agent_id: str, tool_name: str) -> dict:
        """Check if a tool call is within rate limits.

        Returns: {"allowed": bool, "reason": str, "count": int, "limit": int, "window": str}
        """
        limits = self._get_limits(agent_id, tool_name)
        if not limits:
            return {"allowed": True, "reason": "no limits configured"}

        now = time.time()
        key = (agent_id, tool_name)

        if key not in self._counters:
            self._counters[key] = deque(maxlen=1000)  # cap counter size

        counter = self._counters[key]

        # Check each window
        for window_name, (seconds, max_count) in limits.items():
            cutoff = now - seconds
            recent = sum(1 for ts in counter if ts > cutoff)

            if recent >= max_count:
                reason = (
                    f"Rate limit exceeded: {tool_name} ({recent}/{max_count} per {window_name})"
                )
                if self.mode == "enforce":
                    logger.warning(f"[rate_limit] {agent_id}/{tool_name}: {reason}")
                    return {
                        "allowed": False,
                        "reason": reason,
                        "count": recent,
                        "limit": max_count,
                        "window": window_name,
                    }
                else:
                    # Log mode — allow but warn
                    logger.info(f"[rate_limit:log] {agent_id}/{tool_name}: {reason}")

        return {"allowed": True, "reason": "within limits"}

    def record(self, agent_id: str, tool_name: str) -> None:
        """Record a tool call for rate limiting."""
        key = (agent_id, tool_name)
        if key not in self._counters:
            self._counters[key] = deque(maxlen=1000)
        self._counters[key].append(time.time())

    def _get_limits(self, agent_id: str, tool_name: str) -> dict[str, tuple[int, int]]:
        """Get rate limits for an agent+tool combo.

        Returns dict of window_name -> (seconds, max_count).
        Agent overrides take precedence over tool-specific, which take precedence over defaults.
        """
        limits = {}

        # Start with defaults
        if "max_per_minute" in self.defaults:
            limits["minute"] = (60, self.defaults["max_per_minute"])
        if "max_per_hour" in self.defaults:
            limits["hour"] = (3600, self.defaults["max_per_hour"])
        if "max_per_day" in self.defaults:
            limits["day"] = (86400, self.defaults["max_per_day"])

        # Tool-specific overrides
        tool_cfg = self._match_tool_config(tool_name)
        if tool_cfg:
            if "max_per_minute" in tool_cfg:
                limits["minute"] = (60, tool_cfg["max_per_minute"])
            if "max_per_hour" in tool_cfg:
                limits["hour"] = (3600, tool_cfg["max_per_hour"])
            if "max_per_day" in tool_cfg:
                limits["day"] = (86400, tool_cfg["max_per_day"])

        # Agent overrides
        agent_cfg = self.agent_overrides.get(agent_id, {})
        agent_tool_cfg = agent_cfg.get(tool_name, {})
        if agent_tool_cfg:
            if "max_per_minute" in agent_tool_cfg:
                limits["minute"] = (60, agent_tool_cfg["max_per_minute"])
            if "max_per_hour" in agent_tool_cfg:
                limits["hour"] = (3600, agent_tool_cfg["max_per_hour"])
            if "max_per_day" in agent_tool_cfg:
                limits["day"] = (86400, agent_tool_cfg["max_per_day"])

        return limits

    def _match_tool_config(self, tool_name: str) -> dict | None:
        """Match a tool name against configured tool limits, supporting wildcards."""
        # Exact match first
        if tool_name in self.tool_limits:
            return self.tool_limits[tool_name]
        # Wildcard match
        for pattern, cfg in self.tool_limits.items():
            if pattern.endswith("*") and tool_name.startswith(pattern[:-1]):
                return cfg
        return None

    def get_stats(self, agent_id: str | None = None) -> dict:
        """Get current rate limit stats."""
        stats = {}
        now = time.time()
        for (aid, tool), counter in self._counters.items():
            if agent_id and aid != agent_id:
                continue
            hour_count = sum(1 for ts in counter if ts > now - 3600)
            if hour_count > 0:
                stats[f"{aid}/{tool}"] = {"last_hour": hour_count, "total": len(counter)}
        return stats
