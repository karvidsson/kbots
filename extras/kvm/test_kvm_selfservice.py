"""Self-service KVM setup: nothing is usable until the owner's tap is on record.

Owner decisions go through the real engine store (AskStore), so these tests
check the tool against the schema the engine actually writes.
"""

import json

import kvm
import pytest

from src.core.base import ToolContext
from src.core.owner_asks import AskStore

OWNER = "1000000000000000001"
CFG = {"remind_after": 3600, "stale_after": 86400}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    (tmp_path / "config").mkdir()
    data = tmp_path / "data"
    (tmp_path / "config" / "config.yaml").write_text(
        f"kbots:\n  data_dir: {data}\n"
        "kvm:\n  devices:\n    office: {host: 192.168.8.100}\n")
    store = AskStore(data / "owner-asks.db")
    asks, secrets = [], []

    async def fake_ask_owner(ctx, question, default, options=None, context="", request_key=""):
        row = store.create(agent_id=ctx.agent_id, recipient_id=OWNER, account="a", channel_id="1",
                           guild_id="2", bot_id="3", cfg=CFG,
                           payload={"question": question, "default": default, "options": options,
                                    "context": context, "request_key": request_key})
        store.update(row["id"], state="open", message_id=str(len(asks) + 10))
        asks.append(row["id"])
        return json.dumps({"id": row["id"], "state": "open"})

    async def fake_ask_secret(ctx, key, label, purpose):
        secrets.append(key)
        return ""

    async def kvm_answers(host):
        return True

    import src.tools.owner_asks
    monkeypatch.setattr(src.tools.owner_asks, "ask_owner", fake_ask_owner)
    monkeypatch.setattr(kvm, "_ask_secret", fake_ask_secret)
    monkeypatch.setattr(kvm, "_probe", kvm_answers)
    yield type("Env", (), {"store": store, "asks": asks, "secrets": secrets})
    store.close()


def _ctx(agent="atlas", vault=None):
    return ToolContext(agent_id=agent, channel_id="c", user_id="u", vault=vault)


def answer(env, choice, actor=OWNER):
    return env.store.finish(env.asks[-1], answer=choice, actor=actor)


# ── adding a device ───────────────────────────────────────────────────

async def test_added_device_is_unusable_until_the_owner_taps_add(env):
    out = await kvm.kvm_add(_ctx(), "lab", "glkvm.tail1234.ts.net", keymap="sv")
    assert "approve" in out and env.secrets == ["kvm_lab"]
    assert "waiting for the owner" in kvm._resolve(_ctx(), "lab")[2] or "Unknown KVM" in kvm._resolve(_ctx(), "lab")[2]
    assert answer(env, "Add")
    spec, name, err = kvm._resolve(_ctx(), "lab")
    assert not err and name == "lab" and spec["keymap"] == "sv"
    assert spec["agents"] == ["atlas"] and spec["source"] == "registered"


async def test_other_agents_cannot_use_an_added_device(env):
    await kvm.kvm_add(_ctx(), "lab", "glkvm.tail1234.ts.net")
    answer(env, "Add")
    assert "not assigned" in kvm._resolve(_ctx("nova"), "lab")[2]


async def test_declined_add_removes_the_device(env):
    await kvm.kvm_add(_ctx(), "lab", "glkvm.tail1234.ts.net")
    answer(env, "Don't")
    assert "Unknown KVM" in kvm._resolve(_ctx(), "lab")[2]
    assert "approve" in await kvm.kvm_add(_ctx(), "lab", "glkvm.tail1234.ts.net")    # can try again


async def test_answer_from_anyone_but_the_owner_is_never_recorded(env):
    await kvm.kvm_add(_ctx(), "lab", "glkvm.tail1234.ts.net")
    assert not answer(env, "Add", actor="999")                  # the engine refuses it
    assert "Unknown KVM" in kvm._resolve(_ctx(), "lab")[2]


async def test_agent_cannot_fake_an_approval_in_kvm_db(env):
    await kvm.kvm_add(_ctx(), "lab", "glkvm.tail1234.ts.net")
    db = kvm._db()
    db.execute("UPDATE pending SET ask_id='forged'")             # point at an ask that doesn't exist
    db.close()
    assert "Unknown KVM" in kvm._resolve(_ctx(), "lab")[2]


async def test_add_refuses_internet_hosts_and_bad_names(env):
    assert "not a LAN or Tailscale" in await kvm.kvm_add(_ctx(), "lab", "kvm.example.com")
    assert "lowercase" in await kvm.kvm_add(_ctx(), "Lab!", "glkvm.tail1234.ts.net")
    assert "config.yaml" in await kvm.kvm_add(_ctx(), "office", "glkvm.tail1234.ts.net")
    assert env.asks == []


async def test_add_twice_does_not_post_a_second_card(env):
    await kvm.kvm_add(_ctx(), "lab", "glkvm.tail1234.ts.net")
    assert "already waiting" in await kvm.kvm_add(_ctx(), "lab", "glkvm.tail1234.ts.net")
    assert len(env.asks) == 1


async def test_unreachable_kvm_posts_nothing(env, monkeypatch):
    async def nothing(host):
        return False
    monkeypatch.setattr(kvm, "_probe", nothing)
    assert "No KVM answers" in await kvm.kvm_add(_ctx(), "lab", "glkvm.tail1234.ts.net")
    assert env.asks == []


# ── rights ────────────────────────────────────────────────────────────

async def _added(env):
    await kvm.kvm_add(_ctx(), "lab", "glkvm.tail1234.ts.net")
    answer(env, "Add")


async def test_allow_waits_for_the_owner_then_grants(env):
    await _added(env)
    assert "Allow" in await kvm.kvm_allow(_ctx(), "lab", "nova")
    assert "not assigned" in kvm._resolve(_ctx("nova"), "lab")[2]
    answer(env, "Allow")
    assert kvm._resolve(_ctx("nova"), "lab")[2] == ""


async def test_revoke_is_immediate(env):
    await _added(env)
    assert "no longer" in await kvm.kvm_revoke(_ctx(), "lab", "atlas")
    assert "not assigned" in kvm._resolve(_ctx(), "lab")[2]


async def test_sign_in_and_power_start_off(env):
    await _added(env)
    assert "Sign-in is off" in await kvm.kvm_login(_ctx(), "lab")
    assert "Power control is off" in await kvm.kvm_power(_ctx(), "off", "lab")


async def test_turning_login_on_needs_the_owner_and_asks_for_the_password(env):
    await _added(env)
    out = await kvm.kvm_settings(_ctx(), "lab", "login", "on")
    assert "password" in out and env.secrets[-1] == "kvm_lab_login"
    assert not kvm._resolve(_ctx(), "lab")[0]["allow_login"]
    answer(env, "Turn on")
    assert kvm._resolve(_ctx(), "lab")[0]["allow_login"]
    assert "off" in await kvm.kvm_settings(_ctx(), "lab", "login", "off")      # off is immediate
    assert not kvm._resolve(_ctx(), "lab")[0]["allow_login"]


async def test_settings_need_access_to_the_device(env):
    await _added(env)
    assert "not assigned" in await kvm.kvm_settings(_ctx("nova"), "lab", "power", "on")


async def test_config_devices_keep_their_old_behaviour(env):
    spec, _, err = kvm._resolve(_ctx("anyone"), "office")
    assert not err and spec["allow_login"] and spec["allow_power"]


async def test_devices_lists_pending_decisions(env, monkeypatch):
    async def quiet(*a, **k):
        return {}, ""
    monkeypatch.setattr(kvm, "_json", quiet)
    await kvm.kvm_add(_ctx(), "lab", "glkvm.tail1234.ts.net")
    assert "lab: waiting for the owner (to be added)" in await kvm.kvm_devices(_ctx())


async def test_an_answer_to_a_different_card_does_not_count(env):
    await kvm.kvm_add(_ctx(), "lab", "glkvm.tail1234.ts.net")
    other = env.store.create(agent_id="atlas", recipient_id=OWNER, account="a", channel_id="1", guild_id="2",
                             bot_id="3", cfg=CFG, payload={"question": "Order lunch?", "default": "No lunch.",
                                                           "options": ["Add", "Don't"], "context": "",
                                                           "request_key": "lunch"})
    env.store.update(other["id"], state="open", message_id="99")
    env.store.finish(other["id"], answer="Add", actor=OWNER)
    db = kvm._db()
    db.execute("UPDATE pending SET ask_id=?", (other["id"],))   # swap in the unrelated approval
    db.close()
    assert "Unknown KVM" in kvm._resolve(_ctx(), "lab")[2]
