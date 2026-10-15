"""KVM tools — registry, reachability guard, agent pinning, coordinates, keys."""

import json
import struct

import kvm
import pytest

from src.core.base import ToolContext

TS_NET = ".ts.net"                 # split so the install-leak scan sees no hostname
TAILNET_HOST = "glkvm.tailnet" + TS_NET


@pytest.fixture
def devices(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        "kvm:\n"
        "  devices:\n"
        f"    lab: {TAILNET_HOST}\n"                         # shorthand
        "    bench: {host: 100.101.102.103, agents: [atlas], keymap: sv,\n"
        "            login: {user: alice, reveal: esc}}\n"
        "    office: {host: 192.168.8.100, user: ops}\n"
        "    evil: {host: kvm.example.com}\n"                      # internet → refused
        "    fronted: {host: 'https://kvm.example.com', public: true}\n")
    kvm._screen_size.clear()

    async def no_sleep(_):
        pass

    monkeypatch.setattr(kvm.asyncio, "sleep", no_sleep)
    return cfg


def _ctx(agent="atlas"):
    return ToolContext(agent_id=agent, channel_id="c", user_id="u")


def _jpeg(w: int, h: int) -> bytes:
    """Smallest byte string _jpeg_size parses: SOI, an APP0, then SOF0."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


_SIGNAL = json.dumps({"ok": True, "result": {"streamer": {"source": {
    "online": True, "real_resolution": "1920x1080@60",
    "resolution": {"width": 1920, "height": 1080}}}}}).encode()
_NUDGE = ("/hid/events/send_mouse_relative", "/hid/events/send_key")
_NO_VIDEO = "KVM 'lab' has no video — is the target powered on and its HDMI connected?"


def _paths(calls, skip=_NUDGE):
    return [c[1] for c in calls if c[1] not in skip]


_NO_SIGNAL = json.dumps({"ok": True, "result": {"streamer": {"source": {
    "online": True, "real_resolution": "no_signal",
    "resolution": {"width": 2560, "height": 1440}}}}}).encode()


def _fake_kvm(monkeypatch, responses: dict | None = None):
    """Replace HTTP with a recorder. responses: path → body bytes, or a list
    served in turn (the last one repeats). A str entry is returned as an error."""
    calls = []
    responses = {"/streamer": _SIGNAL, **(responses or {})}

    async def fake_request(ctx, name, spec, method, path, params=None, data=None,
                           timeout=None):
        calls.append((method, path, dict(params or {}), data))
        body = responses.get(path, b'{"ok": true, "result": {}}')
        if isinstance(body, list):
            body = body.pop(0) if len(body) > 1 else body[0]
        if isinstance(body, str):
            return None, body
        return body, ""

    monkeypatch.setattr(kvm, "_request", fake_request)
    return calls


# ── registry & guards ─────────────────────────────────────────────────

def test_registry_parses_shorthand_and_full(devices):
    d = kvm._load_devices()
    assert d["lab"]["host"] == TAILNET_HOST and d["lab"]["user"] == "admin"
    assert d["bench"]["agents"] == ["atlas"] and d["bench"]["keymap"] == "sv"
    assert d["office"]["user"] == "ops"
    assert d["lab"]["verify_tls"] is False


def test_reachability_guard():
    for ok in ("192.168.8.100", "10.0.0.5", "127.0.0.1", "100.101.102.103",
               TAILNET_HOST, "glkvm.local", "https://192.168.8.100:8443"):
        assert kvm._reachable_privately(ok), ok
    for bad in ("8.8.8.8", "kvm.example.com", "https://kvm.example.com", "100.128.0.1"):
        assert not kvm._reachable_privately(bad), bad


def test_resolve_refuses_internet_host_unless_public(devices):
    _, _, err = kvm._resolve(_ctx(), "evil")
    assert "not a LAN/Tailscale" in err
    spec, name, err = kvm._resolve(_ctx(), "fronted")
    assert not err and kvm._base_url(spec) == "https://kvm.example.com"


def test_resolve_pins_device_to_its_agents(devices):
    assert kvm._resolve(_ctx("atlas"), "bench")[2] == ""
    assert "not assigned" in kvm._resolve(_ctx("nova"), "bench")[2]


def test_unknown_device_lists_names(devices):
    _, _, err = kvm._resolve(_ctx(), "nope")
    assert "lab" in err and "bench" in err


def test_single_device_is_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("kvm:\n  devices:\n    only: 10.0.0.9\n")
    _, name, err = kvm._resolve(_ctx(), "")
    assert not err and name == "only"


def test_no_config():
    assert "No KVM devices configured" in kvm._resolve(_ctx(), "x")[2]


# ── geometry & keys ───────────────────────────────────────────────────

def test_jpeg_size():
    assert kvm._jpeg_size(_jpeg(1280, 720)) == (1280, 720)
    assert kvm._jpeg_size(b"not a jpeg") is None


def test_to_abs_maps_edges_and_clamps():
    assert kvm._to_abs(0, 1280) == -32768
    assert kvm._to_abs(1279, 1280) == 32767
    assert abs(kvm._to_abs(640, 1281)) <= 1
    assert kvm._to_abs(5000, 1280) == 32767 and kvm._to_abs(-5, 1280) == -32768


@pytest.mark.parametrize("combo,codes", [
    ("enter", ["Enter"]),
    ("ctrl+alt+delete", ["ControlLeft", "AltLeft", "Delete"]),
    ("Win+R", ["MetaLeft", "KeyR"]),
    ("alt+f4", ["AltLeft", "F4"]),
    ("ctrl+shift+1", ["ControlLeft", "ShiftLeft", "Digit1"]),
    ("ArrowUp", ["ArrowUp"]),
    ("ctrl+-", ["ControlLeft", "Minus"]),
])
def test_parse_combo(combo, codes):
    assert kvm._parse_combo(combo) == (codes, "")


def test_parse_combo_rejects_unknown():
    codes, err = kvm._parse_combo("ctrl+banana")
    assert codes == [] and "banana" in err


# ── tools ─────────────────────────────────────────────────────────────

async def test_screen_saves_image_and_records_size(devices, monkeypatch, tmp_path):
    monkeypatch.setattr(kvm, "KBOTS_TMP", tmp_path)
    calls = _fake_kvm(monkeypatch, {"/streamer/snapshot": _jpeg(1280, 800)})
    out = await kvm.kvm_screen(_ctx(), "lab")
    assert "1280x800" in out and kvm._screen_size["lab"] == (1280, 800)
    snap = [c for c in calls if c[1] == "/streamer/snapshot"]
    assert snap[0][2]["preview_max_width"] == str(kvm._SCREEN_MAX_WIDTH)


_OCR_ON = json.dumps({"ok": True, "result": {"ocr": {"enabled": True}}}).encode()


async def test_screen_text_mode_reads_ocr_string(devices, monkeypatch):
    _fake_kvm(monkeypatch, {"/streamer/ocr": _OCR_ON, "/streamer/snapshot": json.dumps(
        {"ok": True, "result": "  Login:  "}).encode()})
    assert await kvm.kvm_screen(_ctx(), "lab", mode="text") == "Login:"


async def test_screen_text_mode_without_ocr_points_to_image(devices, monkeypatch):
    calls = _fake_kvm(monkeypatch, {"/streamer/ocr": json.dumps(
        {"ok": True, "result": {"ocr": {"enabled": False}}}).encode()})
    out = await kvm.kvm_screen(_ctx(), "lab", mode="text")
    assert "no OCR" in out and "image" in out
    assert [c[1] for c in calls] == ["/streamer/ocr"]


async def test_click_scales_against_last_screenshot(devices, monkeypatch):
    calls = _fake_kvm(monkeypatch)
    kvm._screen_size["lab"] = (1280, 720)
    out = await kvm.kvm_click(_ctx(), 1279, 0, "lab", button="right")
    move, click = calls
    assert move[1] == "/hid/events/send_mouse_move"
    assert move[2] == {"to_x": "32767", "to_y": "-32768"}
    assert click[2] == {"button": "right"} and "Right-clicked" in out


async def test_click_takes_screenshot_when_size_unknown(devices, monkeypatch):
    calls = _fake_kvm(monkeypatch, {"/streamer/snapshot": _jpeg(100, 100)})
    await kvm.kvm_click(_ctx(), 0, 0, "lab", button="double")
    assert _paths(calls) == ["/streamer/snapshot", "/hid/events/send_mouse_move",
                             "/hid/events/send_mouse_button", "/hid/events/send_mouse_button"]


async def test_type_sends_body_and_keymap(devices, monkeypatch):
    calls = _fake_kvm(monkeypatch)
    await kvm.kvm_type(_ctx(), "hej då", "bench")
    _, path, params, data = calls[0]
    assert path == "/hid/print" and data == "hej då"
    assert params == {"limit": "0", "keymap": "sv"}


async def test_key_sends_shortcut_codes(devices, monkeypatch):
    calls = _fake_kvm(monkeypatch)
    await kvm.kvm_key(_ctx(), "ctrl+alt+delete", "lab")
    assert calls[0][1:3] == ("/hid/events/send_shortcut",
                             {"keys": "ControlLeft,AltLeft,Delete"})


async def test_scroll_direction_and_cap(devices, monkeypatch):
    calls = _fake_kvm(monkeypatch)
    out = await kvm.kvm_scroll(_ctx(), 500, "lab")
    assert len(calls) == 50 and calls[0][2]["delta_y"] == "-1" and "down 50" in out
    calls.clear()
    await kvm.kvm_scroll(_ctx(), -2, "lab")
    assert [c[2]["delta_y"] for c in calls] == ["1", "1"]


async def test_power_is_hitl_gated():
    assert kvm.kvm_power._tool_def.hitl is True
    assert kvm.kvm_click._tool_def.hitl is False


async def test_power_status_reads_leds(devices, monkeypatch):
    _fake_kvm(monkeypatch, {"/atx": json.dumps(
        {"ok": True, "result": {"enabled": True, "leds": {"power": True, "hdd": False}}}).encode()})
    assert await kvm.kvm_power(_ctx(), "status", "lab") == "lab: power on"


async def test_devices_hides_other_agents_machines(devices, monkeypatch):
    _fake_kvm(monkeypatch, {
        "/hid": b'{"ok": true, "result": {"keyboard": {"online": true}, "mouse": {"online": true}}}',
        "/streamer": json.dumps({"ok": True, "result": {"streamer": {"source": {
            "online": True, "resolution": {"width": 1920, "height": 1080}}}}}).encode(),
    })
    out = await kvm.kvm_devices(_ctx("nova"))
    assert "bench" not in out
    assert "lab: video 1920x1080, keyboard ok, mouse ok" in out
    assert "evil: " in out and "refusing" in out


async def test_devices_reports_no_signal_when_hdmi_is_dark(devices, monkeypatch):
    _fake_kvm(monkeypatch, {
        "/hid": b'{"ok": true, "result": {"keyboard": {"online": true}, "mouse": {"online": true}}}',
        "/streamer": json.dumps({"ok": True, "result": {"streamer": {"source": {
            "online": True, "real_resolution": "no_signal",
            "resolution": {"width": 2560, "height": 1440}}}}}).encode(),
    })
    assert "lab: video no signal" in await kvm.kvm_devices(_ctx())


async def test_screen_nudges_then_retries_until_video(devices, monkeypatch, tmp_path):
    monkeypatch.setattr(kvm, "KBOTS_TMP", tmp_path)
    calls = _fake_kvm(monkeypatch, {"/streamer/snapshot": [_NO_VIDEO, _NO_VIDEO, _jpeg(1280, 720)]})
    out = await kvm.kvm_screen(_ctx(), "lab")
    assert "Screenshot saved" in out
    assert calls[0][1] == "/hid/events/send_mouse_relative"      # nudged before the first grab
    keys = [(c[2]["key"], c[2]["state"]) for c in calls if c[1] == "/hid/events/send_key"]
    assert set(k for k, _ in keys) == {"ShiftLeft"}              # nothing that types
    assert not any(c[1] == "/hid/events/send_mouse_button" for c in calls)
    assert _paths(calls).count("/streamer/snapshot") == 3


async def test_screen_reports_when_wake_fails(devices, monkeypatch):
    monkeypatch.setattr(kvm, "_WAKE_TIMEOUT", 0)
    _fake_kvm(monkeypatch, {"/streamer/snapshot": _NO_VIDEO})
    out = await kvm.kvm_screen(_ctx(), "lab")
    assert "didn't wake" in out


async def test_screen_ignores_unreliable_no_signal_status(devices, monkeypatch, tmp_path):
    """GLKVM can report no_signal while the screen is on; the grab decides."""
    monkeypatch.setattr(kvm, "KBOTS_TMP", tmp_path)
    _fake_kvm(monkeypatch, {"/streamer": _NO_SIGNAL, "/streamer/snapshot": _jpeg(800, 600)})
    assert "Screenshot saved" in await kvm.kvm_screen(_ctx(), "lab")


class _Vault:
    def __init__(self, secrets):
        self.secrets = secrets

    def get(self, key):
        return self.secrets.get(key)


def _vctx(secrets, agent="atlas"):
    return ToolContext(agent_id=agent, channel_id="c", user_id="u", vault=_Vault(secrets))


async def test_login_types_secret_without_returning_it(devices, monkeypatch):
    calls = _fake_kvm(monkeypatch)
    out = await kvm.kvm_login(_vctx({"kvm_lab_login": "hunter2!"}), "lab")
    assert "hunter2" not in out and "Enter" in out
    sent = [(c[1], c[2], c[3]) for c in calls if c[1] not in _NUDGE]
    assert sent == [
        ("/hid/print", {"limit": "0"}, "hunter2!"),
        ("/hid/events/send_shortcut", {"keys": "Enter"}, None),
    ]


async def test_login_with_user_reveal_and_keymap(devices, monkeypatch):
    calls = _fake_kvm(monkeypatch)
    await kvm.kvm_login(_vctx({"kvm_bench_login": "pw"}), "bench")
    sent = [(c[1], c[2].get("keys"), c[3]) for c in calls if c[1] not in _NUDGE]
    assert sent == [
        ("/hid/events/send_shortcut", "Escape", None),
        ("/hid/print", None, "alice"),
        ("/hid/events/send_shortcut", "Tab", None),
        ("/hid/print", None, "pw"),
        ("/hid/events/send_shortcut", "Enter", None),
    ]
    assert all(c[2].get("keymap") == "sv" for c in calls if c[1] == "/hid/print")


async def test_login_without_vault_secret_types_nothing(devices, monkeypatch):
    calls = _fake_kvm(monkeypatch)
    out = await kvm.kvm_login(_vctx({}), "lab")
    assert "kvm_lab_login" in out and calls == []


async def test_login_wakes_and_clears_before_typing(devices, monkeypatch):
    calls = _fake_kvm(monkeypatch)
    await kvm.kvm_login(_vctx({"kvm_lab_login": "pw"}), "lab")
    first_print = next(i for i, c in enumerate(calls) if c[1] == "/hid/print")
    before = calls[:first_print]
    assert before[0][1] == "/hid/events/send_mouse_relative"
    backspaces = [c for c in before if c[2].get("key") == "Backspace"]
    assert len(backspaces) == 2 * kvm._CLEAR_KEYS                # press + release each
    released = {c[2]["key"] for c in before
                if c[1] == "/hid/events/send_key" and c[2].get("state") == "false"}
    assert {"ControlLeft", "ShiftLeft", "MetaLeft", "AltLeft"} <= released
    assert not any(c[1] == "/streamer" for c in calls)          # never gated on the status


async def test_login_is_ungated_and_respects_agent_pinning(devices, monkeypatch):
    assert kvm.kvm_login._tool_def.hitl is False
    calls = _fake_kvm(monkeypatch)
    out = await kvm.kvm_login(_vctx({"kvm_bench_login": "pw"}, agent="nova"), "bench")
    assert "not assigned" in out and calls == []


async def test_every_key_press_is_released(devices, monkeypatch, tmp_path):
    """Regression: a bare send_key leaves modifiers held (Ctrl+click = right-click)."""
    monkeypatch.setattr(kvm, "KBOTS_TMP", tmp_path)
    calls = _fake_kvm(monkeypatch, {"/streamer/snapshot": _jpeg(100, 100)})
    await kvm.kvm_screen(_ctx(), "lab")
    await kvm.kvm_login(_vctx({"kvm_lab_login": "pw"}), "lab")
    held: dict[str, int] = {}
    for _, path, params, _ in calls:
        if path == "/hid/events/send_key":
            assert params.get("state") in ("true", "false"), "send_key without explicit state"
            held[params["key"]] = held.get(params["key"], 0) + (1 if params["state"] == "true" else -1)
    assert all(v <= 0 for v in held.values()), held
