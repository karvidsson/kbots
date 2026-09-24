"""Private, read-only spend report on each bot's command tree."""

import io
import logging

import discord
from discord import app_commands

from src.core.spend import render_spend

logger = logging.getLogger(__name__)


def register_spend(client):
    @client.tree.command(name="spend", description="Show recorded agent spend (default: 7 days)")
    @app_commands.describe(days="Look back 1 to 365 days")
    async def spend(interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = 7):
        await interaction.response.defer(ephemeral=True)
        manager = getattr(client.connector, "_agent_manager", None)
        ledger = getattr(getattr(manager, "storage", None), "spend", None)
        if not ledger:
            await interaction.followup.send("Spend ledger is unavailable.", ephemeral=True)
            return
        own = not client._is_admin(interaction.user.id)
        try:
            rows = await ledger.rows(days, requester_id=str(interaction.user.id) if own else None)
            report = render_spend(rows, days, own=own)
        except Exception as exc:
            logger.warning("Spend report unavailable (%s)", type(exc).__name__)
            await interaction.followup.send("Spend ledger is unavailable.", ephemeral=True)
            return
        if len(report.encode("utf-16-le")) // 2 <= 2000:
            await interaction.followup.send(report, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            # One private reply, complete arithmetic, no silently omitted rows.
            await interaction.followup.send(
                "Full spend report attached. USD is a list-price equivalent, not a subscription bill.",
                file=discord.File(io.BytesIO(report.encode()), filename=f"spend-{days}-days.txt"),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
