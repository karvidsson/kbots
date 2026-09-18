"""Persistent owner-only fix buttons, bound to committed incident cards."""

import asyncio

import discord

from src.core.alert_channels import AlertError
from src.core.alert_errors import log_failure
from src.core.alert_fix_store import declared_drill


class FixControls:
    def __init__(self, alerts):
        self.alerts, self.store, self.jobs = alerts, alerts.store, alerts.fixer.jobs

    @staticmethod
    def drill(source, receipt):
        return declared_drill(source, receipt)

    def receipt(self, identifier):
        row = self.store.db.execute("SELECT * FROM receipts WHERE id=?", (identifier,)).fetchone()
        return self.store._receipt(row) if row else None

    def view(self, source, receipt):
        if source["config"].get("auto_fix_pr") is True or self.drill(source, receipt):
            return None
        current = self.receipt(receipt["id"]) or receipt
        job = self.jobs.for_receipt(current)
        view = discord.ui.View(timeout=None)
        button = discord.ui.Button(
            label="Fix it",
            style=discord.ButtonStyle.primary,
            custom_id="alert-fix:" + source["id"] + ":" + receipt["issue_id"],
            disabled=current["state"] != "complete" or bool(job and job["state"] in {"pending", "running"}),
        )

        async def clicked(interaction):
            await self.click(source["id"], receipt["id"], receipt["issue_id"], interaction)

        button.callback = clicked
        view.add_item(button)
        return view

    def restore(self, account):
        client = self.alerts.connector.bots[account].client
        for row in self.store.db.execute(
            "SELECT r.* FROM receipts r JOIN sources s ON s.id=r.source_id AND s.revision=r.revision "
            "WHERE s.account=? AND s.state='active' AND r.state='complete' AND r.result_message IS NOT NULL",
            (account,),
        ).fetchall():
            receipt = self.store._receipt(row)
            source = self.store.get(receipt["source_id"])
            if view := self.view(source, receipt):
                client.add_view(view, message_id=int(receipt["result_message"]))

    def validate(self, source_id, receipt_id, issue_id, interaction):
        source, receipt = self.store.get(source_id), self.receipt(receipt_id)
        if not source or interaction.user.bot or str(interaction.user.id) != source["user_id"]:
            raise AlertError("Only the person who set up this registration can use Fix it.")
        bot = self.alerts.connector.bots.get(source["account"])
        message = interaction.message
        if (
            not bot
            or interaction.client is not bot.client
            or not receipt
            or source["state"] != "active"
            or receipt["source_id"] != source_id
            or receipt["revision"] != source["revision"]
            or receipt["issue_id"] != issue_id
            or receipt["state"] != "complete"
            or not receipt["result_message"]
            or str(message.id) != receipt["result_message"]
            or message.author.id != bot.client.user.id
            or getattr(message, "webhook_id", None)
            or str(interaction.channel_id) != source["channel_id"]
            or str(interaction.guild_id) != source["guild_id"]
        ):
            raise AlertError("This incident card is no longer available for a fix.")
        if self.drill(source, receipt):
            raise AlertError("Drills and setup checks do not open fix PRs.")
        return source, receipt

    async def click(self, source_id, receipt_id, issue_id, interaction):
        try:
            self.validate(source_id, receipt_id, issue_id, interaction)
        except AlertError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.alerts.locks.setdefault(source_id, asyncio.Lock()):
                source, receipt = self.validate(source_id, receipt_id, issue_id, interaction)
                with self.store.transaction():
                    job = self.jobs.enqueue(source, receipt, manual=True)
            if pr := job["result"].get("pr"):
                response = "An issue PR already exists: " + pr["url"]
            elif job["state"] in {"pending", "running"}:
                response = "Writing fix. This issue has one queued or running repair."
            else:
                response = "No PR: " + job["result"].get("reason", "fix unavailable")
            await self.alerts.fixer.notices()
        except Exception as error:
            log_failure(error, "fix button")
            response = str(error) if isinstance(error, AlertError) else "Fix request could not be confirmed."
        await interaction.followup.send(response, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def setting(self, bot, interaction, auto_fix_pr, source_id):
        candidates = [
            self.store.get(r[0])
            for r in self.store.db.execute(
                "SELECT id FROM sources WHERE account=? AND user_id=? AND state='active'",
                (bot.account_name, str(interaction.user.id)),
            )
        ]
        if source_id:
            candidates = [s for s in candidates if source_id in {s["id"], s["channel_id"], s["config"].get("app")}]
        if interaction.user.bot or len(candidates) != 1 or type(auto_fix_pr) is not bool:
            await interaction.response.send_message(
                "Choose one of your active alert registrations in source_id.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        source = candidates[0]
        async with self.alerts.locks.setdefault(source["id"], asyncio.Lock()):
            source = self.store.get(source["id"])
            if not source or source["state"] != "active" or source["user_id"] != str(interaction.user.id):
                await interaction.followup.send("The registration changed; try again.", ephemeral=True)
                return
            with self.store.transaction():
                source = self.store.update(source["id"], config={**source["config"], "auto_fix_pr": auto_fix_pr})
                for row in self.store.db.execute(
                    "SELECT * FROM receipts WHERE source_id=? AND revision=? AND state='complete'",
                    (source["id"], source["revision"]),
                ).fetchall():
                    self.jobs.enqueue(source, self.store._receipt(row))
                self.jobs.refresh_cards(source["id"])
        await self.alerts.fixer.notices()
        await interaction.followup.send(
            "Automatic fix PRs are on for eligible incidents."
            if auto_fix_pr
            else "Automatic fix PRs are off. Use Fix it on an incident card. A running repair continues.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
