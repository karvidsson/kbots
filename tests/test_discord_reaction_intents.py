"""Reaction events must be requested for DMs, not only for guild channels.

Regression (2026-08-19): the default intents list named `guild_reactions` but
not `dm_reactions`. Agent home channels are DMs (Discord channel type 1), and
Discord gates guild and DM reaction events on separate gateway bits — so every
reaction the owner pressed on an agent's reply was simply never delivered.

Nothing logged, because an intent you do not request produces no error, only
silence. Three features died together and looked "unimplemented" rather than
broken: 👍/👎 training rewards, 👍/👎 lesson-confidence feedback, and ✅/❌
HITL approval. Across six weeks and 120 boots, not one reaction was handled.
"""

import types

import discord

from src.connectors.discord import DEFAULT_INTENTS, DiscordBot


def _stub_connector():
    """Minimal stand-in — DiscordBot only reads `config` at construction."""
    return types.SimpleNamespace(config={})


def _intents_from(names):
    """Build intents exactly as DiscordBot does, from an explicit list."""
    i = discord.Intents.none()
    for n in names:
        setattr(i, n, True)
    return i


def test_default_intents_cover_both_reaction_scopes():
    i = _intents_from(DEFAULT_INTENTS)
    assert i.guild_reactions, "reactions in server channels"
    assert i.dm_reactions, "reactions in DMs — where agent home channels live"


def test_bot_built_without_an_intents_list_still_receives_dm_reactions():
    """The single-account path passes no list at all; it must not be weaker."""
    bot = DiscordBot(account_name="t", connector=_stub_connector(), admin_users=[])
    intents = bot.client.intents
    assert intents.dm_reactions, "fallback path must request DM reactions too"
    assert intents.dm_messages and intents.message_content, "and keep reading DMs"


def test_an_account_may_still_pin_its_own_intents():
    """An explicit list overrides the default — that behaviour is unchanged."""
    bot = DiscordBot(account_name="t", connector=_stub_connector(), admin_users=[],
                     intents_list=["guilds", "guild_messages"])
    assert not bot.client.intents.dm_reactions


def test_the_reaction_handler_is_registered():
    """Intents are half of it; the listener has to be attached as well."""
    bot = DiscordBot(account_name="t", connector=_stub_connector(), admin_users=[])
    assert bot.client.on_raw_reaction_add == bot.on_raw_reaction_add
