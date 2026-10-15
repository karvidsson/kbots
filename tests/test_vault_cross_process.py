"""Two processes, one vault file: the engine and each MCP tool process.

A secret saved by one must be readable by the other without a restart, and a
later write by the other must not put its stale copy back over the file.
"""

from cryptography.fernet import Fernet

from src.vault.fernet import FernetVault


def _pair(tmp_path):
    """Two vault instances sharing one file and key, as two processes would."""
    key = Fernet(Fernet.generate_key())
    path = tmp_path / "secrets.enc"
    a, b = FernetVault(path), FernetVault(path)
    for v in (a, b):
        v._fernet, v._unlocked = key, True
    a.set("seed", "1")
    return a, b


def test_secret_saved_by_one_process_is_read_by_the_other(tmp_path):
    engine, tools = _pair(tmp_path)
    assert tools.get("seed") == "1"
    engine.set("kvm_lab", "pw")
    assert tools.get("kvm_lab") == "pw"
    assert "kvm_lab" in tools.list_keys()


def test_stale_writer_does_not_erase_the_other_processes_secret(tmp_path):
    engine, tools = _pair(tmp_path)
    tools.get("seed")                       # tools now holds a copy without kvm_lab
    engine.set("kvm_lab", "pw")
    tools.set("other", "x")                 # must merge, not overwrite
    assert engine.get("kvm_lab") == "pw" and engine.get("other") == "x"


def test_delete_sees_the_latest_file(tmp_path):
    engine, tools = _pair(tmp_path)
    tools.get("seed")
    engine.set("kvm_lab", "pw")
    assert tools.delete("kvm_lab") is True
    assert engine.get("kvm_lab") is None


def test_unreadable_change_keeps_the_copy_we_have(tmp_path):
    engine, _ = _pair(tmp_path)
    (tmp_path / "secrets.enc").write_bytes(b"not a fernet token")
    assert engine.get("seed") == "1"
