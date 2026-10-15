"""Owner-entered secrets: card, owner-only form, vault write, and the tool path."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from src.connectors.discord_owner_secrets import DiscordOwnerSecrets, SecretModal, card
from src.core.base import MessageDelivery, ToolContext
from src.core.internal_api import InternalAPI
from src.core.owner_secrets import SecretStore, validate
from src.tools.owner_secrets import ask_owner_secret

OWNER, OTHER, BOT = 1000000000000000001, 1000000000000000002, 1000000000000000003
CHANNEL, GUILD = 1000000000000000004, 1000000000000000005
SECRET = "correct horse battery staple"


class Message:
    def __init__(self, channel, number, content=None, embed=None, view=None, **kwargs):
        self.id, self.channel = number, channel
        self.author = SimpleNamespace(id=BOT, bot=True)
        self.content, self.embeds, self.view = content or "", [embed] if embed else [], view

    async def edit(self, **kwargs):
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]] if kwargs["embed"] else []
        if "content" in kwargs:
            self.content = kwargs["content"] or ""
        self.view = kwargs.get("view", self.view)


class Channel:
    def __init__(self):
        self.id = CHANNEL
        self.guild = SimpleNamespace(id=GUILD, me=SimpleNamespace(id=BOT))
        self.messages = []

    def permissions_for(self, member):
        return SimpleNamespace(embed_links=True)

    async def send(self, content=None, **kwargs):
        message = Message(self, 3000 + len(self.messages), content, **kwargs)
        self.messages.append(message)
        return message

    async def fetch_message(self, message_id):
        return next(m for m in self.messages if m.id == message_id)


class Vault:
    def __init__(self):
        self.data = {}

    def set(self, key, value):
        self.data[key] = value


@pytest.fixture
def f(tmp_path):
    channel = Channel()
    client = SimpleNamespace(user=SimpleNamespace(id=BOT), get_channel=Mock(return_value=channel),
                             fetch_channel=AsyncMock(return_value=channel), add_view=Mock())
    bot = SimpleNamespace(account_name="example", client=client)
    connector = SimpleNamespace(bots={"example": bot}, vault=Vault())
    manager = SimpleNamespace(
        agent_configs={"sample": {}},
        _resolve_home_channel=AsyncMock(return_value=("discord", str(CHANNEL), "example")),
        handle_message=AsyncMock(return_value=MessageDelivery(True)),
    )
    config = {"admin_users": {"discord": [str(OWNER)]}}
    service = DiscordOwnerSecrets(connector, manager, config, tmp_path / "secrets.db")
    connector._owner_secrets = manager._owner_secrets = service
    yield SimpleNamespace(service=service, store=service.store, channel=channel, bot=bot,
                          connector=connector, manager=manager)
    service.store.close()


def interaction(f, user=OWNER, **changes):
    values = dict(
        user=SimpleNamespace(id=user, bot=False), client=f.bot.client, channel_id=CHANNEL, guild_id=GUILD,
        message=f.channel.messages[0] if f.channel.messages else None,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock(),
                                 is_done=Mock(return_value=False)),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    values.update(changes)
    return SimpleNamespace(**values)


async def request(f, key="kvm_lab", **kw):
    return await f.service.request("sample", key, kw.get("label", "KVM password for lab"),
                                   kw.get("purpose", "To log in to the lab KVM."), kw.get("request_key", ""))


async def drain(f):
    for task in list(f.service.tasks):
        await task


def everything_visible(f):
    """All text the channel, the store and the agent could ever see."""
    parts = []
    for m in f.channel.messages:
        parts.append(m.content)
        for e in m.embeds:
            parts += [e.title or "", e.description or ""] + [x.value for x in e.fields]
    parts += [json.dumps(r) for r in f.store.rows(("queued", "posting", "open", "saved", "declined", "stale"))]
    parts += [c.args[1].content for c in f.manager.handle_message.call_args_list]
    return "\n".join(parts)


# ── validation ────────────────────────────────────────────────────────

def test_only_allowed_prefixes_can_be_requested():
    assert validate("kvm_lab", "x", "y")["key"] == "kvm_lab"
    for bad in ("discord-token", "secrets/github-token", "KVM_LAB", "kvm lab", "", "kvm_" + "a" * 100):
        with pytest.raises(ValueError):
            validate(bad, "x", "y")
    assert validate("cam_door", "x", "y", prefixes=("cam_",))["key"] == "cam_door"


def test_label_and_purpose_required():
    with pytest.raises(ValueError):
        validate("kvm_lab", "", "why")
    with pytest.raises(ValueError):
        validate("kvm_lab", "what", " ")


async def test_disallowed_key_posts_nothing(f):
    with pytest.raises(ValueError):
        await request(f, key="discord-token")
    assert f.channel.messages == []


# ── card and form ─────────────────────────────────────────────────────

async def test_request_posts_one_amber_card_with_owner_controls(f):
    result = await request(f)
    assert result["state"] == "open" and len(f.channel.messages) == 1
    message = f.channel.messages[0]
    assert message.embeds[0].title == "sample needs: KVM password for lab"
    ids = [item.custom_id for item in message.view.children]
    assert ids == [f"owner-secret:{result['id']}:1:enter", f"owner-secret:{result['id']}:1:decline"]


async def test_same_key_reuses_the_open_request(f):
    first = await request(f)
    second = await request(f)
    assert first["id"] == second["id"] and len(f.channel.messages) == 1


async def test_owner_enter_opens_a_modal_and_saves_to_vault(f):
    result = await request(f)
    i = interaction(f)
    await f.service.click(result["id"], i, decline=False)
    modal = i.response.send_modal.call_args.args[0]
    assert isinstance(modal, SecretModal)
    await f.service.submit(result["id"], modal.revision, SECRET + "\n", interaction(f))
    await drain(f)
    assert f.connector.vault.data == {"kvm_lab": SECRET}
    row = f.store.get(result["id"])
    assert row["state"] == "saved" and row["done_by"] == str(OWNER)
    assert f.channel.messages[0].embeds[0].title == "Saved: KVM password for lab"
    assert f.channel.messages[0].view is None
    note = f.manager.handle_message.call_args.args[1].content
    assert "kvm_lab" in note and "saved" in note


async def test_the_value_appears_nowhere_but_the_vault(f):
    result = await request(f)
    await f.service.submit(result["id"], 1, SECRET, interaction(f))
    await drain(f)
    assert SECRET not in everything_visible(f)


async def test_someone_else_cannot_open_or_submit(f):
    result = await request(f)
    i = interaction(f, user=OTHER)
    await f.service.click(result["id"], i, decline=False)
    i.response.send_modal.assert_not_called()
    assert "Only the owner" in i.response.send_message.call_args.args[0]
    await f.service.submit(result["id"], 1, SECRET, interaction(f, user=OTHER))
    assert f.connector.vault.data == {} and f.store.get(result["id"])["state"] == "open"


async def test_click_on_another_message_is_refused(f):
    result = await request(f)
    stranger = SimpleNamespace(id=999, author=SimpleNamespace(id=BOT))
    i = interaction(f, message=stranger)
    await f.service.click(result["id"], i, decline=False)
    i.response.send_modal.assert_not_called()


async def test_stale_revision_form_does_not_write(f):
    result = await request(f)
    await f.service.submit(result["id"], 1, "first", interaction(f))
    await f.service.submit(result["id"], 1, "second", interaction(f))   # replayed old form
    assert f.connector.vault.data == {"kvm_lab": "first"}


async def test_decline_stores_nothing_and_tells_the_agent(f):
    result = await request(f)
    await f.service.click(result["id"], interaction(f), decline=True)
    await drain(f)
    assert f.connector.vault.data == {} and f.store.get(result["id"])["state"] == "declined"
    assert "declined" in f.manager.handle_message.call_args.args[1].content


async def test_expired_request_cannot_be_answered(f):
    result = await request(f)
    f.store.db.execute("UPDATE owner_secrets SET stale_at=? WHERE id=?", (time.time() - 1, result["id"]))
    await f.service.submit(result["id"], 1, SECRET, interaction(f))
    assert f.connector.vault.data == {}
    assert f.service.store.expire() == 1
    assert card(f.store.get(result["id"]))[0].title.startswith("Expired")


async def test_open_cards_are_restored_after_restart(f):
    await request(f)
    await f.service.start(f.bot)
    f.bot.client.add_view.assert_called_once()


# ── tool path (MCP subprocess has no manager) ─────────────────────────

async def test_tool_through_internal_api(f, monkeypatch):
    api = InternalAPI(f.manager)
    await api.start()
    monkeypatch.setenv("KBOTS_INTERNAL_API", api.env["KBOTS_INTERNAL_API"])
    monkeypatch.setenv("KBOTS_INTERNAL_TOKEN", api.token)
    try:
        ctx = ToolContext(agent_id="sample")
        out = json.loads(await ask_owner_secret(ctx, "kvm_lab", "KVM password for lab", "To log in."))
        assert out["state"] == "open" and out["key"] == "kvm_lab"
        bad = json.loads(await ask_owner_secret(ctx, "discord-token", "Bot token", "Because."))
        assert "must start with" in bad["error"]
        async with aiohttp.ClientSession() as session:
            async with session.post(api.env["KBOTS_INTERNAL_API"] + "/owner-secret", json={}) as response:
                assert response.status == 401
            async with session.post(api.env["KBOTS_INTERNAL_API"] + "/owner-secret",
                                    json={"agent_id": "sample", "key": "kvm_x", "label": "x", "purpose": "y",
                                          "value": "smuggled"},
                                    headers={"Authorization": "Bearer " + api.token}) as response:
                assert response.status == 400          # no way to pass a value through the API
    finally:
        await api.stop()


def test_store_finish_requires_recipient_and_revision(tmp_path):
    store = SecretStore(tmp_path / "s.db")
    row = store.create(agent_id="a", recipient_id="1", account="x", channel_id="2", guild_id="3", bot_id="4",
                       fields=validate("kvm_a", "l", "p"))
    store.update(row["id"], state="open")
    assert not store.finish(row["id"], state="saved", actor="9", revision=1)
    assert not store.finish(row["id"], state="saved", actor="1", revision=7)
    assert store.finish(row["id"], state="saved", actor="1", revision=1)
    assert not store.finish(row["id"], state="saved", actor="1", revision=2)
    store.close()
