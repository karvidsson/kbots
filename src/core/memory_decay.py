"""The thing that actually runs memory decay.

`SQLiteMemory.decay()` has existed and been complete since the store was
written. Nothing called it. `defaults.memory.decay_enabled` has existed as a
config key, is surfaced by the settings manager, and was read by nothing in the
engine. `scripts/memory-decay.sh` implements the same lifecycle in SQL and says
it runs "daily at 03:00 UTC via kbots-memory-decay.timer", which is a systemd
unit that no macOS install has and no installer creates.

Three halves of one mechanism, none of them wired to the others. Measured on
the live store on 2026-08-22: 237 memories, 236 of them still at the birth
confidence of 0.70, oldest created seven weeks earlier, zero archived, and no
decay row in the changelog. It had never run once.

This is the missing part: an in-process daily task, gated on the config key
that already exists, using the store the engine itself opened. One
implementation, so there is nothing left to drift.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_H = 24.0


class MemoryDecay:
    """Daily decay pass over the memory store."""

    def __init__(self, memory, config: dict | None = None):
        cfg = config or {}
        self.memory = memory
        self.enabled = bool(cfg.get("decay_enabled", False))
        decay_cfg = cfg.get("decay") or {}
        self.interval = float(decay_cfg.get("interval_hours", DEFAULT_INTERVAL_H)) * 3600
        self.rate = float(decay_cfg.get("rate", 0.0108))
        self.threshold = float(decay_cfg.get("archive_threshold", 0.05))
        # Deleting is not the same decision as archiving, so it is not the same
        # switch. Off unless a deployment says otherwise, in as many words.
        self.purge = bool(decay_cfg.get("purge_archived", False))
        self.purge_after_days = int(decay_cfg.get("purge_after_days", 90))

    async def tick(self) -> dict:
        result = await self.memory.decay(
            decay_rate=self.rate, archive_threshold=self.threshold,
            purge=self.purge, purge_after_days=self.purge_after_days)
        if result.get("archived") or result.get("purged"):
            # Archiving hides a memory from every read. That is worth a line
            # even on a quiet day, because the alternative is noticing months
            # later that something the fleet used to know is gone.
            logger.info(f"memory-decay: {result['decayed']} decayed, "
                        f"{result['archived']} archived, {result['purged']} purged")
        else:
            logger.debug(f"memory-decay: {result['decayed']} decayed")
        return result

    async def run(self) -> None:
        immune = ", ".join(self.memory.IMMUNE_CATEGORIES)
        logger.info(
            f"Memory decay: ON (every {self.interval / 3600:g}h, rate {self.rate}/day, "
            f"archive below {self.threshold}, purge={'ON' if self.purge else 'OFF'}, "
            f"immune categories: {immune})")
        while True:
            try:
                await self.tick()
            except Exception as e:
                logger.error(f"memory-decay tick failed: {e}", exc_info=True)
            await asyncio.sleep(self.interval)
