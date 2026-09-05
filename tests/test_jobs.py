"""Durable background jobs.

These drive real subprocesses rather than mocks. The whole point of the module
is what happens to an OS process when its launcher goes away, and a mock cannot
be wrong about that in the way the real thing was.
"""

import os
import time

import pytest

from src.core import jobs


@pytest.fixture(autouse=True)
def _isolate_jobs(tmp_path):
    """Every test gets its own data dir, so no test can see, settle or cancel a
    job belonging to the live deployment. The reply-shorten regression showed
    what happens when a suite reads production state."""
    jobs.set_data_dir(tmp_path)
    yield
    jobs.set_data_dir(None)


def _settle(job_id: str, timeout: float = 10.0) -> dict:
    """Reconcile until the job leaves running, or fail loudly."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs.reconcile()
        job = jobs.get(job_id)
        assert job is not None
        if job["status"] != jobs.RUNNING:
            return job
        time.sleep(0.05)
    pytest.fail(f"job {job_id} never settled: {jobs.get(job_id)}")


# --- the basics -------------------------------------------------------------

def test_success_records_exit_zero(tmp_path):
    job = jobs.start("atlas", "echo hello", label="Smoke Test")
    assert job["status"] == jobs.RUNNING
    assert job["label"] == "Smoke Test"
    assert job["id"].startswith("smoke-test-")

    settled = _settle(job["id"])
    assert settled["status"] == jobs.DONE
    assert settled["exit_code"] == 0
    assert settled["ended_at"] >= settled["started_at"]
    assert "hello" in jobs.tail_log(job["id"])


def test_failure_is_failed_not_crashed():
    """A command that ran and returned non-zero is `failed`. `crashed` is
    reserved for one that never got to report, and conflating them would hide
    which of the two happened."""
    job = jobs.start("atlas", "exit 3", label="boom")
    settled = _settle(job["id"])
    assert settled["status"] == jobs.FAILED
    assert settled["exit_code"] == 3


def test_stderr_lands_in_the_log():
    job = jobs.start("atlas", "echo oops >&2", label="stderr")
    _settle(job["id"])
    assert "oops" in jobs.tail_log(job["id"])


def test_explicit_exit_still_writes_the_status():
    """`exit 4` inside a brace group exits the wrapper shell before it can
    record anything, so the job reports `crashed` and the real exit code is
    lost. The wrapper uses a subshell for exactly this."""
    job = jobs.start("atlas", "exit 4", label="explicit exit")
    settled = _settle(job["id"])
    assert settled["status"] == jobs.FAILED
    assert settled["exit_code"] == 4


def test_pipeline_status_follows_shell_semantics():
    """Documenting the real behaviour rather than claiming to fix it: bash
    reports the LAST element of a pipeline, so `false | true` is a success.
    A caller who needs otherwise sets `pipefail` in its own command."""
    job = jobs.start("atlas", "false | true", label="pipeline")
    assert _settle(job["id"])["status"] == jobs.DONE
    strict = jobs.start("atlas", "set -o pipefail; false | true", label="pipefail")
    assert _settle(strict["id"])["status"] == jobs.FAILED


# --- the reason the module exists ------------------------------------------

def test_job_is_detached_from_this_process():
    """The child must not be in our process group, or a signal to the launcher's
    group takes the build with it. This is failure 1 from the field report."""
    job = jobs.start("atlas", "sleep 5", label="detached")
    try:
        assert os.getpgid(job["pid"]) != os.getpgid(os.getpid())
    finally:
        jobs.cancel(job["id"])


def test_completion_is_recognised_with_nobody_watching():
    """Start a job, let it finish while no reconcile runs, then reconcile once.

    This is the restart case: the process that was waiting is gone, and the
    outcome still has to be recoverable from disk.
    """
    job = jobs.start("atlas", "echo done", label="unwatched")
    time.sleep(1.0)  # finishes with no reconcile in between
    settled = jobs.reconcile()
    assert [j["id"] for j in settled] == [job["id"]]
    assert jobs.get(job["id"])["status"] == jobs.DONE


def test_killed_job_is_crashed_not_running():
    """SIGKILL leaves no exit file. Without the pid fallback this row would say
    running forever, which is exactly the lie the module exists to stop."""
    job = jobs.start("atlas", "sleep 30", label="killed")
    os.killpg(os.getpgid(job["pid"]), 9)
    settled = _settle(job["id"])
    assert settled["status"] == jobs.CRASHED
    assert settled["exit_code"] is None


def test_reconcile_returns_each_job_once():
    """The watcher delivers what reconcile returns, so a job reported twice is a
    duplicate notification."""
    job = jobs.start("atlas", "true", label="once")
    _settle(job["id"])
    assert jobs.reconcile() == []


def test_exit_file_beats_a_dead_pid():
    """Order of authority: a job that completed and whose pid is long gone must
    report its real exit code, not `crashed`."""
    job = jobs.start("atlas", "exit 7", label="authority")
    _settle(job["id"])
    # Re-open the case as if the watcher had never seen it.
    jobs._write("UPDATE jobs SET status = ?, exit_code = NULL WHERE id = ?",
                (jobs.RUNNING, job["id"]))
    again = _settle(job["id"])
    assert again["status"] == jobs.FAILED
    assert again["exit_code"] == 7


# --- cancel -----------------------------------------------------------------

def test_cancel_kills_the_child_not_just_the_shell():
    """A build script is a shell whose real work is a child process. Killing
    only the shell leaves the encode running and holding the output file."""
    marker = jobs.jobs_dir() / "child-still-running"
    job = jobs.start(
        "atlas",
        f"bash -c 'sleep 30; touch {marker}' & wait",
        label="group kill")
    time.sleep(0.5)
    out = jobs.cancel(job["id"])
    assert "cancelled" in out
    assert jobs.get(job["id"])["status"] == jobs.CANCELLED
    time.sleep(1.0)
    assert not marker.exists(), "grandchild survived the cancel"


def test_cancel_on_unknown_job_is_a_message_not_an_exception():
    assert "No job" in jobs.cancel("does-not-exist")


def test_cancel_twice_is_idempotent():
    job = jobs.start("atlas", "sleep 30", label="twice")
    jobs.cancel(job["id"])
    assert "already" in jobs.cancel(job["id"])


# --- querying ---------------------------------------------------------------

def test_list_is_scoped_to_the_agent():
    jobs.start("atlas", "true", label="mine")
    jobs.start("beacon", "true", label="theirs")
    mine = jobs.list_jobs("atlas")
    assert [j["label"] for j in mine] == ["mine"]
    assert len(jobs.list_jobs("")) == 2


def test_running_only_filter():
    slow = jobs.start("atlas", "sleep 30", label="slow")
    quick = jobs.start("atlas", "true", label="quick")
    _settle(quick["id"])
    try:
        running = jobs.list_jobs("atlas", running_only=True)
        assert [j["id"] for j in running] == [slow["id"]]
    finally:
        jobs.cancel(slow["id"])


def test_tail_log_is_bounded():
    job = jobs.start("atlas", "seq 1 500", label="tail")
    _settle(job["id"])
    assert len(jobs.tail_log(job["id"], 10).splitlines()) == 10
    # Asking for more than the cap must not return the whole file.
    assert len(jobs.tail_log(job["id"], 10_000).splitlines()) <= 200


def test_tail_log_on_unknown_job_is_empty_not_an_error():
    assert jobs.tail_log("nope") == ""


def test_summarise_names_the_outcome():
    job = jobs.start("atlas", "exit 2", label="summary")
    settled = _settle(job["id"])
    line = jobs.summarise(settled)
    assert job["id"] in line
    assert "failed" in line
    assert "exit 2" in line


def test_cwd_is_honoured(tmp_path):
    (tmp_path / "here.txt").write_text("x")
    job = jobs.start("atlas", "ls", label="cwd", cwd=str(tmp_path))
    _settle(job["id"])
    assert "here.txt" in jobs.tail_log(job["id"])


def test_notify_defaults_to_agent():
    """The default has to be the mode that removes the human from the loop;
    anything else leaves failure 4 in place."""
    job = jobs.start("atlas", "true", label="notify")
    assert jobs.get(job["id"])["notify"] == "agent"
    assert jobs.get(job["id"])["notified"] == 0


def test_ids_are_unique_for_the_same_label():
    a = jobs.start("atlas", "true", label="same")
    b = jobs.start("atlas", "true", label="same")
    assert a["id"] != b["id"]


def test_label_with_no_usable_characters_still_produces_an_id():
    job = jobs.start("atlas", "true", label="///")
    assert job["id"].startswith("job-")


# --- governance -------------------------------------------------------------

def test_job_tools_are_in_the_mcp_lockdown_set():
    """KBOTS_MCP_RESTRICT exists to remove host command execution from the stdio
    surface. job_start is host command execution, so a deployment that drops
    run_command and keeps job_start would have removed the gate and kept the
    capability, in a form that outlives the session."""
    from src.mcp_server import DANGEROUS_TOOL_NAMES, DANGEROUS_TOOL_PREFIXES

    for name in ("job_start", "job_cancel", "job_logs", "job_status", "job_list"):
        assert name in DANGEROUS_TOOL_NAMES or name.startswith(DANGEROUS_TOOL_PREFIXES), \
            f"{name} escapes the MCP lockdown"
    assert "run_command" in DANGEROUS_TOOL_NAMES  # the comparison this rests on


@pytest.mark.asyncio
async def test_job_start_refuses_credential_paths():
    """Same tripwire run_command carries. Detaching the command must not be a
    way around the permission layer it refuses to bypass."""
    from src.core.base import ToolContext
    from src.tools.jobs import job_start

    ctx = ToolContext(agent_id="atlas", user_id="u", channel_id="c")
    out = await job_start(ctx, "cat ~/.claude/settings.json", label="sneaky")
    assert "Blocked" in out
    assert jobs.list_jobs("atlas") == []
