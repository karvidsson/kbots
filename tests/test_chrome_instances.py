"""Instance resolution + endpoint-identity checks for chrome_browser.

The concepts being fenced in: (1) an agent with a `chrome_instance` config block
drives its OWN Chrome — its own port, data dir, and reservation lane — while
everyone else shares the default instance; (2) a port that answers is only
trusted when the responder is identifiably the kbots debug Chrome, so squatters
and the user's own (flag-ignoring) Chrome produce a clear refusal instead of a
confusing half-attached session.
"""

import json

from src.tools.chrome_desktop import (
    DEBUG_PORT,
    RESOURCE,
    _endpoint_file,
    _Instance,
    _instance_for,
    _verify_owner,
)


class _Mgr:
    def __init__(self, configs):
        self.agent_configs = configs


class _Ctx:
    def __init__(self, agent_id, configs=None):
        self.agent_id = agent_id
        self.agent_manager = _Mgr(configs or {})


# --- instance resolution ---

def test_default_agent_gets_shared_instance():
    inst = _instance_for(_Ctx("atlas", {"atlas": {}}))
    assert inst.port == DEBUG_PORT
    assert inst.dedicated is False
    assert inst.resource == RESOURCE


def test_chrome_instance_config_gets_dedicated_port_and_lane():
    cfg = {"milo": {"chrome_instance": {"port": 9223}}}
    inst = _instance_for(_Ctx("milo", cfg))
    assert inst.port == 9223
    assert inst.dedicated is True
    assert inst.resource == f"{RESOURCE}:9223"
    assert inst.dir.name == ".kbots-chrome-milo"   # derived from agent id


def test_chrome_instance_explicit_dir_wins(tmp_path):
    cfg = {"milo": {"chrome_instance": {"port": 9223, "dir": str(tmp_path / "c")}}}
    assert _instance_for(_Ctx("milo", cfg)).dir == tmp_path / "c"


def test_no_agent_manager_falls_back_to_shared():
    ctx = _Ctx("ghost")
    ctx.agent_manager = None
    assert _instance_for(ctx).port == DEBUG_PORT


# --- endpoint discovery file ---

def test_endpoint_file_default_vs_dedicated(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    assert _endpoint_file(DEBUG_PORT).name == "chrome-debug.json"
    assert _endpoint_file(9223).name == "chrome-debug-9223.json"
    assert _endpoint_file(9223).parent == tmp_path / "data"


# --- owner verification ---

def _write_endpoint(tmp_path, port, pid, data_dir):
    f = tmp_path / "data" / ("chrome-debug.json" if port == DEBUG_PORT
                             else f"chrome-debug-{port}.json")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"port": port, "user_data_dir": str(data_dir), "pid": pid}))


def test_owner_ok_when_endpoint_pid_holds_our_dir(tmp_path, monkeypatch):
    import src.tools.chrome_desktop as cd
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    inst = _Instance(9223, tmp_path / "chrome", dedicated=True)
    _write_endpoint(tmp_path, 9223, 4242, inst.dir)
    monkeypatch.setattr(cd, "_pid_cmdline",
                        lambda pid: f"Chrome --user-data-dir={inst.dir}" if pid == 4242 else "")
    assert _verify_owner(inst) is None


def test_owner_refused_when_pid_is_someone_else(tmp_path, monkeypatch):
    import src.tools.chrome_desktop as cd
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    inst = _Instance(9223, tmp_path / "chrome", dedicated=True)
    _write_endpoint(tmp_path, 9223, 4242, inst.dir)
    monkeypatch.setattr(cd, "_pid_cmdline", lambda pid: "some-other-process")
    # pgrep fallback also finds nothing
    monkeypatch.setattr(cd.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": ""})())
    out = _verify_owner(inst)
    assert out is not None and "Refusing to drive an unidentified browser" in out


def test_owner_fallback_accepts_presupervision_launch(tmp_path, monkeypatch):
    """No endpoint file (Chrome started before this feature): accept a live
    process carrying both our port and our data dir on its command line."""
    import src.tools.chrome_desktop as cd
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    inst = _Instance(9223, tmp_path / "chrome", dedicated=True)
    monkeypatch.setattr(
        cd.subprocess, "run",
        lambda *a, **k: type("R", (), {
            "stdout": f"123 Chrome --remote-debugging-port=9223 --user-data-dir={inst.dir}\n"})())
    assert _verify_owner(inst) is None
