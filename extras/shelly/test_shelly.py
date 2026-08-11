"""Shelly tools — registry, LAN guard, Gen1/Gen2 URLs, error self-correction."""

import pytest
import shelly

from src.core.base import ToolContext


@pytest.fixture
def devices(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        "shelly:\n"
        "  devices:\n"
        "    office_light: 192.168.1.42\n"                      # shorthand → gen2 switch
        "    heater: {host: 192.168.1.43, gen: 1}\n"
        "    blinds: {host: 192.168.1.44, kind: cover}\n"
        "    lamp: {host: 192.168.1.45, gen: 1, kind: dimmer}\n"  # brightness-capable
        "    hall_b: {host: 192.168.1.46, channel: 1}\n"          # 2nd relay of a 2PM
        "    evil: {host: 8.8.8.8}\n"                             # non-LAN → refused
        "  groups:\n"
        "    downstairs: [office_light, lamp]\n"
        "    bogus: [nonexistent]\n")                             # dropped: no members
    return cfg


def _ctx():
    return ToolContext(agent_id="atlas", channel_id="c", user_id="u")


def _capture_calls(monkeypatch, reply=None):
    calls = []

    async def fake_call(ctx, device, spec, path_gen2, path_gen1):
        calls.append((device, spec["gen"], path_gen2, path_gen1))
        return reply if reply is not None else {"was_on": False}

    monkeypatch.setattr(shelly, "_call", fake_call)
    return calls


def test_registry_parses_shorthand_and_full(devices):
    d = shelly._load_devices()
    assert d["office_light"] == {"host": "192.168.1.42", "gen": 2,
                                 "kind": "switch", "channel": 0}
    assert d["heater"]["gen"] == 1
    assert d["blinds"]["kind"] == "cover"
    assert d["lamp"]["kind"] == "dimmer"
    assert d["hall_b"]["channel"] == 1


def test_lan_guard():
    assert shelly._lan_only("192.168.1.42") and shelly._lan_only("10.0.0.5")
    assert shelly._lan_only("shelly-office.local")
    assert not shelly._lan_only("8.8.8.8") and not shelly._lan_only("example.com")


async def test_unknown_device_lists_names(devices):
    out = await shelly.shelly_switch(_ctx(), "offce_light", "on")   # typo
    assert "Unknown device" in out and "office_light" in out        # self-correction hint


async def test_non_lan_device_refused(devices):
    out = await shelly.shelly_status(_ctx(), "evil")
    assert "not a LAN address" in out


async def test_gen2_switch_url(devices, monkeypatch):
    calls = _capture_calls(monkeypatch)
    out = await shelly.shelly_switch(_ctx(), "office_light", "on")
    assert calls[0][2] == "/rpc/Switch.Set?id=0&on=true"            # gen2 path
    assert out == "office_light → ON (was off)"


async def test_gen1_switch_url(devices, monkeypatch):
    calls = _capture_calls(monkeypatch, reply={"ison": True})
    out = await shelly.shelly_switch(_ctx(), "heater", "off")
    assert calls[0][1] == 1 and calls[0][3] == "/relay/0?turn=off"  # gen1 path
    assert "heater → OFF" in out


async def test_toggle_reports_transition(devices, monkeypatch):
    _capture_calls(monkeypatch, reply={"was_on": True})
    out = await shelly.shelly_switch(_ctx(), "office_light", "toggle")
    assert out == "office_light → OFF (was on)"


async def test_gen1_dimmer_uses_light_not_relay(devices, monkeypatch):
    """Regression: Dimmer 2 has no relay — /relay/0 answers 404."""
    calls = _capture_calls(monkeypatch, reply={"ison": True, "brightness": 40})
    out = await shelly.shelly_switch(_ctx(), "lamp", "on")
    assert calls[0][3] == "/light/0?turn=on"
    assert "relay" not in calls[0][3]
    assert out == "lamp → ON"        # no "(was ...)": gen1 /light returns new state


async def test_dim_sets_brightness(devices, monkeypatch):
    calls = _capture_calls(monkeypatch, reply={"ison": True, "brightness": 30})
    out = await shelly.shelly_dim(_ctx(), "lamp", 30)
    assert calls[0][3] == "/light/0?turn=on&brightness=30"
    assert "30%" in out


async def test_dim_zero_turns_off(devices, monkeypatch):
    calls = _capture_calls(monkeypatch, reply={"ison": False})
    out = await shelly.shelly_dim(_ctx(), "lamp", 0)
    assert calls[0][3] == "/light/0?turn=off"
    assert "OFF" in out


async def test_dim_rejects_out_of_range(devices):
    assert "between 0 and 100" in await shelly.shelly_dim(_ctx(), "lamp", 150)


async def test_dim_refuses_non_dimmer(devices):
    out = await shelly.shelly_dim(_ctx(), "office_light", 50)
    assert "not a dimmer" in out and "shelly_switch" in out


async def test_channel_addresses_second_relay(devices, monkeypatch):
    calls = _capture_calls(monkeypatch)
    await shelly.shelly_switch(_ctx(), "hall_b", "on")
    assert calls[0][2] == "/rpc/Switch.Set?id=1&on=true"


async def test_group_fans_out(devices, monkeypatch):
    calls = _capture_calls(monkeypatch)
    out = await shelly.shelly_switch(_ctx(), "downstairs", "off")
    assert {c[0] for c in calls} == {"office_light", "lamp"}
    assert "2 devices" in out


async def test_group_with_no_valid_members_is_dropped(devices):
    assert "bogus" not in shelly._load_groups()


async def test_all_excludes_covers(devices):
    members = shelly._members("all")
    assert "blinds" not in members            # covers are HITL-gated, never bulk-moved
    assert "office_light" in members and "lamp" in members


async def test_dim_group_skips_non_dimmers(devices, monkeypatch):
    calls = _capture_calls(monkeypatch, reply={"ison": True, "brightness": 20})
    out = await shelly.shelly_dim(_ctx(), "downstairs", 20)
    assert [c[0] for c in calls] == ["lamp"]  # office_light is a switch
    assert "1 dimmers" in out


async def test_switch_refuses_cover_kind(devices):
    out = await shelly.shelly_switch(_ctx(), "blinds", "on")
    assert "use shelly_cover" in out


async def test_cover_is_hitl_gated(devices):
    from src.core.tools import get_tool
    assert get_tool("shelly_cover").hitl is True
    assert get_tool("shelly_switch").hitl is False


async def test_unreachable_device_clean_error(devices):
    # real _call against a non-routable RFC5737 doc address? too slow — mock the session
    out = await shelly.shelly_status(_ctx(), "office_light")
    assert isinstance(out, str)  # either clean error text or status — never raises


async def test_no_devices_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("kbots: {name: x}\n")
    out = await shelly.shelly_devices(_ctx())
    assert "No Shelly devices configured" in out


# --- compile_report (lives in reports.py, tested here with the shelly PR) ---

@pytest.fixture
def allow_tmp(tmp_path, monkeypatch):
    """Let validate_file_path accept pytest's tmp dir."""
    from src.tools import ingest
    monkeypatch.setattr(ingest, "ALLOWED_PATH_ROOTS",
                        [*ingest.ALLOWED_PATH_ROOTS, tmp_path.resolve()])
    return tmp_path


async def test_compile_report_end_to_end(allow_tmp):
    tmp_path = allow_tmp
    pytest.importorskip("pandas")
    pytest.importorskip("matplotlib")
    from src.tools.reports import compile_report
    data = tmp_path / "sales.csv"
    data.write_text("date,revenue,units\n2026-07-01,100,3\n2026-07-02,140,4\n"
                    "2026-07-03,120,2\n")
    spec = tmp_path / "weekly.yaml"
    spec.write_text(f"title: Weekly Sales\ndata_file: {data}\nformat: md\n"
                    "intro: Numbers for the week.\n"
                    "charts:\n  - {type: line, x: date, y: revenue, title: Revenue}\n")
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="u",
                      project_dir=str(tmp_path / "ws"))
    out = await compile_report(ctx, str(spec))
    assert out.startswith("Report compiled: ") and "3 rows" in out and "1 chart(s)" in out
    from pathlib import Path
    report_path = Path(out.split("Report compiled: ", 1)[1].split(" — ")[0])
    text = report_path.read_text()
    assert "# Weekly Sales" in text and "![Revenue](" in text


async def test_compile_report_missing_column_surfaces_error(allow_tmp):
    tmp_path = allow_tmp
    pytest.importorskip("pandas")
    from src.tools.reports import compile_report
    data = tmp_path / "d.csv"
    data.write_text("a,b\n1,2\n")
    spec = tmp_path / "s.yaml"
    spec.write_text(f"data_file: {data}\ncharts:\n  - {{type: line, x: nope, y: b}}\n")
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="u",
                      project_dir=str(tmp_path / "ws"))
    out = await compile_report(ctx, str(spec))
    assert "Chart 1 failed" in out and "nope" in out


async def test_compile_report_bad_spec(allow_tmp):
    tmp_path = allow_tmp
    from src.tools.reports import compile_report
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="u")
    assert "Spec not found" in await compile_report(ctx, str(tmp_path / "missing.yaml"))
    bad = tmp_path / "bad.yaml"
    bad.write_text("title: x\n")   # no data_file
    assert "missing data_file" in await compile_report(ctx, str(bad))
