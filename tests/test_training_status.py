"""Training-data status: what is on disk, and where.

Two people hunting for turns.jsonl by hand and one failing to find it at all is
what this reports on. The configured `data_dir` is commonly relative, so it
resolves against the service's working directory rather than anywhere a person
would think to look — the resolved path is the whole point.

The other half is telling "nobody has reacted yet" apart from "the reward path
is broken". They are indistinguishable from the exporter, which prints a clean
zero for both.
"""

import json

from src.core.training_collector import TrainingCollector, training_status


def test_reports_the_resolved_absolute_path(tmp_path, monkeypatch):
    (tmp_path / "training").mkdir()
    monkeypatch.chdir(tmp_path)
    st = training_status("./training")
    assert st["dir"] == str((tmp_path / "training").resolve())
    assert st["dir"].startswith("/"), "a relative path is what caused the hunt"
    assert st["dir_exists"] is True


def test_missing_directory_is_reported_not_created(tmp_path):
    st = training_status(tmp_path / "nope")
    assert st["dir_exists"] is False
    assert not (tmp_path / "nope").exists(), "status must not have side effects"
    assert st["turns"] == 0


def test_absent_rewards_file_is_distinguishable_from_zero_rewards(tmp_path):
    """The distinction the exporter cannot make."""
    st = training_status(tmp_path)
    assert st["rewards_file_exists"] is False
    assert st["rewards"] == 0

    (tmp_path / "rewards.jsonl").write_text("")
    st = training_status(tmp_path)
    assert st["rewards_file_exists"] is True, "an empty file is not a missing one"
    assert st["rewards"] == 0


def test_counts_turns_and_rewards(tmp_path):
    (tmp_path / "turns.jsonl").write_text(
        "".join(json.dumps({"turn_id": i}) + "\n" for i in range(7)))
    (tmp_path / "rewards.jsonl").write_text(
        json.dumps({"signal": "up"}) + "\n" + json.dumps({"signal": "down"}) + "\n")
    st = training_status(tmp_path)
    assert st["turns"] == 7
    assert st["rewards"] == 2
    assert st["turns_bytes"] > 0
    assert st["turns_mtime"] is not None


def test_blank_lines_do_not_inflate_the_count(tmp_path):
    (tmp_path / "turns.jsonl").write_text('{"a":1}\n\n\n{"a":2}\n')
    assert training_status(tmp_path)["turns"] == 2


def test_collector_reports_its_own_directory(tmp_path):
    tc = TrainingCollector(tmp_path / "t", include_tool_trace=False)
    tc.record_reward("m1", "atlas", "up", "u1")
    st = tc.status()
    assert st["dir"] == str((tmp_path / "t").resolve())
    assert st["rewards"] == 1
    assert st["rewards_file_exists"] is True
    assert st["include_tool_trace"] is False
