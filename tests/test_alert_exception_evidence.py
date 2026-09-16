"""Bound the sample query and prove compiled frames select the actual handler."""

import copy
import json
import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from extras.posthog.alerts import PostHogAdapter, sample_drill_status, sampled_exception
from src.core.alert_channels import AlertError, AlertStore
from src.core.alert_diagnosis import source_evidence
from tests.test_posthog_alerts import FakeResponse, FakeSession


@pytest.fixture
def adapter(tmp_path):
    store = AlertStore(tmp_path / "state")
    value = PostHogAdapter(SimpleNamespace(get=lambda _: "synthetic-key-with-no-real-permissions"), store)
    value.test_config = {
        "host": "https://eu.posthog.com",
        "project": "123",
        "api_key": "secrets/test-key",
        "triggers": ["created"],
    }
    yield value
    store.close()


def response(source=".output/server/chunks/routes/api/debug/boom.get.mjs"):
    return {
        "results": [
            {
                "uuid": "00000000-0000-4000-8000-000000000001",
                "distinct_id": "private-person",
                "timestamp": "unused",
                "properties": {
                    "$session_id": "private-session",
                    "$current_url": "private-navigation",
                    "$exception_list": [
                        {
                            "type": "Error",
                            "value": "Expected drill throw",
                            "mechanism": {"secret": "private"},
                            "stacktrace": {
                                "frames": [
                                    {"source": "node_modules/library/index.js", "line": 4, "in_app": False},
                                    {
                                        "source": source,
                                        "resolved_name": "handler",
                                        "line": 58,
                                        "in_app": True,
                                        "code_variables": {"token": "private-variable"},
                                        "junk_drawer": {"private": "data"},
                                    },
                                ]
                            },
                        }
                    ],
                    "$exception_releases": [
                        {
                            "version": "v1.2.3",
                            "private": "data",
                            "metadata": {"git": {"commit_id": "a" * 40, "branch": "private-branch"}},
                        }
                    ],
                },
            }
        ],
        "hasMore": True,
        "limit": 1,
        "offset": 0,
    }


async def test_sample_post_is_fixed_read_scope_and_uses_no_provisioning_bypass(adapter):
    data = response()
    session = FakeSession(FakeResponse(200, [json.dumps(data).encode()]))
    adapter.session_factory = lambda: session
    issue = str(uuid.uuid4())
    result = await adapter._request(
        adapter.test_config, "POST", "error_tracking/query/issue_events/", payload=adapter.sample_request(issue)
    )
    assert result == data
    args, options = session.calls[0]
    assert args == ("POST", "https://eu.posthog.com/api/projects/123/error_tracking/query/issue_events/")
    assert options["json"] == {
        "issueId": issue,
        "limit": 1,
        "onlyAppFrames": True,
        "filterTestAccounts": False,
        "include": ["exception", "stacktrace", "release"],
        "dateRange": options["json"]["dateRange"],
    }
    assert options["allow_redirects"] is False


@pytest.mark.parametrize(
    "change",
    [
        "variables",
        "environment",
        "navigation",
        "correlation",
        "limit",
        "bool_limit",
        "all_frames",
        "numeric_frames",
        "numeric_filter",
        "extra",
        "wrong_id",
        "wrong_path",
        "query_suffix",
        "get",
        "patch",
    ],
)
async def test_no_other_incident_post_body_or_endpoint_is_admitted(adapter, change):
    body = adapter.sample_request(str(uuid.uuid4()))
    path, method = "error_tracking/query/issue_events/", "POST"
    if change in {"variables", "environment", "navigation", "correlation"}:
        body["include"].append("code_variables" if change == "variables" else change)
    elif change == "limit":
        body["limit"] = 2
    elif change == "bool_limit":
        body["limit"] = True
    elif change == "all_frames":
        body["onlyAppFrames"] = False
    elif change == "numeric_frames":
        body["onlyAppFrames"] = 1
    elif change == "numeric_filter":
        body["filterTestAccounts"] = 0
    elif change == "extra":
        body["searchQuery"] = "anything"
    elif change == "wrong_id":
        body["issueId"] = "../other"
    elif change == "wrong_path":
        path = "query/"
    elif change == "query_suffix":
        path += "?unrestricted=true"
    else:
        method = change.upper()
    adapter.session_factory = lambda: pytest.fail("invalid query must not open a session")
    with pytest.raises(AlertError):
        await adapter._request(adapter.test_config, method, path, payload=body)


def test_sample_projection_excludes_locals_people_sessions_and_urls():
    raw = response("https://example.com/.output/server/chunks/routes/api/debug/boom.get.mjs?token=private-token")
    result = sampled_exception(raw)
    text = json.dumps(result)
    assert "private" not in text
    assert result["releases"] == [{"version": "v1.2.3", "commit_id": "a" * 40}]
    assert result["exceptions"] == [
        {
            "type": "Error",
            "value": "Expected drill throw",
            "frames": [
                {
                    "source": "/.output/server/chunks/routes/api/debug/boom.get.mjs",
                    "resolved_name": "handler",
                    "line": 58,
                }
            ],
        }
    ]
    assert "not necessarily the triggering event" in result["status"]


def test_sample_projection_bounds_chains_text_and_frames():
    raw = response()
    exception = raw["results"][0]["properties"]["$exception_list"][0]
    exception["value"] = "x" * 5000
    exception["stacktrace"]["frames"] = [copy.deepcopy(exception["stacktrace"]["frames"][-1]) for _ in range(150)]
    raw["results"][0]["properties"]["$exception_list"] = [exception] * 20
    result = sampled_exception(raw)
    assert len(result["exceptions"]) == 3
    assert max(len(e["value"]) for e in result["exceptions"]) == 1200
    assert sum(len(e["frames"]) for e in result["exceptions"]) == 24


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"results": None},
        {"results": [None]},
        {"results": [{"properties": "bad"}]},
        {"results": [response()["results"][0]] * 2},
    ],
)
def test_invalid_sample_cannot_be_presented_as_verified_evidence(data):
    with pytest.raises(AlertError):
        sampled_exception(data)


async def test_issue_fetch_returns_only_projected_sample(adapter):
    issue = str(uuid.uuid4())
    adapter._request = AsyncMock(side_effect=[{"id": issue, "name": "Error"}, response(), response()])
    source = {"config": adapter.test_config}
    result = await adapter.issue(source, issue)
    assert result["sample"]["exceptions"][0]["frames"][0]["source"].endswith("boom.get.mjs")
    assert "private" not in json.dumps(result)
    assert len(adapter._request.call_args_list) == 3
    query = adapter._request.call_args_list[1].kwargs["payload"]
    assert adapter._request.call_args.kwargs == {
        "payload": adapter.sample_request(issue, date_range=query["dateRange"], drill=True)
    }
    assert result["sample"]["drill_status"] == "drill"
    assert "uuid" not in json.dumps(result) and "distinct_id" not in json.dumps(result)


def tracked_repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, value in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)
    for command in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=Fixture", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
    ):
        subprocess.run(["git", "-C", str(repo), *command], check=True)
    return repo


def test_compiled_boom_frame_beats_hundreds_of_unrelated_error_matches(tmp_path):
    files = {f"components/A{i:03}.vue": "const Error = 'unrelated client code'" for i in range(310)}
    files["server/api/debug/boom.get.ts"] = "export default () => { throw new Error('deliberate drill') }"
    repo = tracked_repo(tmp_path, files)
    issue = {"name": "Error", "sample": sampled_exception(response())}
    evidence = source_evidence(str(repo), issue)
    assert [s["path"] for s in evidence["snippets"]] == ["server/api/debug/boom.get.ts"]
    assert evidence["selection"] == "in-app frame paths"
    assert "compiled frame lines may differ" in evidence["frame_matches"][0]["mapping"]


@pytest.mark.parametrize(
    "frame",
    [
        "/app/server/api/debug/boom.get.ts",
        "dist/server/api/debug/boom.get.js",
        ".output/server/chunks/routes/api/debug/boom.get.mjs",
    ],
)
def test_direct_and_build_paths_resolve_to_tracked_source(tmp_path, frame):
    repo = tracked_repo(tmp_path, {"server/api/debug/boom.get.ts": "throw Error('drill')"})
    evidence = source_evidence(str(repo), {"name": "Error", "sample": sampled_exception(response(frame))})
    assert evidence["snippets"][0]["path"] == "server/api/debug/boom.get.ts"


def test_ambiguous_frame_never_picks_one_of_two_clones_of_the_path(tmp_path):
    repo = tracked_repo(tmp_path, {"one/api/debug/boom.get.ts": "one", "two/api/debug/boom.get.ts": "two"})
    evidence = source_evidence(str(repo), {"name": "Error", "sample": sampled_exception(response())})
    assert not evidence["frame_matches"] and not evidence["snippets"]
    assert evidence["unresolved_frames"][0]["reason"] == "ambiguous"


def test_frames_never_read_untracked_symlink_or_parent_escape(tmp_path):
    repo = tracked_repo(tmp_path, {"server/api/debug/boom.get.ts": "tracked"})
    (tmp_path / "private.ts").write_text("DO NOT READ")
    (repo / "leak.ts").symlink_to(tmp_path / "private.ts")
    subprocess.run(["git", "-C", str(repo), "add", "leak.ts"], check=True)
    (repo / "untracked.ts").write_text("DO NOT READ")
    for path in ("leak.ts", "untracked.ts", "../private.ts"):
        evidence = source_evidence(str(repo), {"name": "Absent", "sample": sampled_exception(response(path))})
        assert "DO NOT READ" not in json.dumps(evidence)
        assert not evidence["snippets"]


@pytest.mark.parametrize(
    "sample_kind,filtered_kind,expected",
    [
        ("present", "same", "drill"),
        ("present", "empty", "unmarked"),
        ("empty", "empty", "unknown"),
        ("empty", "same", "unknown"),
        ("present", "different", "unknown"),
        ("present", "invalid", "unknown"),
        ("invalid", "same", "unknown"),
        ("present", "incomplete_empty", "unknown"),
    ],
)
def test_sample_classification_never_confuses_issue_membership_with_event_identity(
    sample_kind, filtered_kind, expected
):
    def data(kind):
        if kind in {"empty", "incomplete_empty"}:
            return {"results": [], "hasMore": kind == "incomplete_empty"}
        value = response()
        if kind == "different":
            value["results"][0]["uuid"] = str(uuid.uuid4())
        elif kind == "invalid":
            value["results"][0]["uuid"] = "not-a-uuid"
        return value

    assert sample_drill_status(data(sample_kind), data(filtered_kind)) == expected


@pytest.mark.parametrize("bad", [{}, {"results": None}, {"results": [None]}, {"results": [{}, {}]}])
def test_malformed_filter_response_is_unknown(bad):
    assert sample_drill_status(response(), bad) == "unknown"


@pytest.mark.parametrize("drill", [False, True])
async def test_both_exact_sample_queries_share_the_fixed_absolute_window(adapter, drill):
    from datetime import datetime, timedelta

    body = adapter.sample_request(str(uuid.uuid4()), drill=drill)
    window = body["dateRange"]
    dates = [datetime.fromisoformat(window[name]) for name in ("date_from", "date_to")]
    assert dates[1] - dates[0] == timedelta(days=7)
    session = FakeSession(FakeResponse(200, [b'{"results":[]}']))
    adapter.session_factory = lambda: session
    await adapter._request(adapter.test_config, "POST", "error_tracking/query/issue_events/", payload=body)
    assert session.calls[0][1]["json"] == body
    if drill:
        assert body["filterGroup"] == [{"key": "test", "value": ["true"], "operator": "exact", "type": "event"}]
    else:
        assert "filterGroup" not in body


@pytest.mark.parametrize(
    "change",
    [
        "other_key",
        "false_value",
        "bool_value",
        "operator",
        "hogql",
        "extra_filter",
        "empty_filter",
        "extra_filter_field",
        "relative_date",
        "missing_end",
        "wide_window",
        "stale_window",
        "invalid_date",
        "extra_date_field",
        "numeric_date",
    ],
)
async def test_drill_query_admits_no_arbitrary_filter_or_time_range(adapter, change):
    from datetime import datetime, timedelta

    body = adapter.sample_request(str(uuid.uuid4()), drill=True)
    clause = body["filterGroup"][0]
    if change == "other_key":
        clause["key"] = "email"
    elif change == "false_value":
        clause["value"] = ["false"]
    elif change == "bool_value":
        clause["value"] = [True]
    elif change == "operator":
        clause["operator"] = "is_not"
    elif change == "hogql":
        clause["type"] = "hogql"
    elif change == "extra_filter":
        body["filterGroup"].append(dict(clause))
    elif change == "empty_filter":
        body["filterGroup"] = []
    elif change == "extra_filter_field":
        clause["other"] = "field"
    elif change == "relative_date":
        body["dateRange"]["date_from"] = "-7d"
    elif change == "missing_end":
        del body["dateRange"]["date_to"]
    elif change == "invalid_date":
        body["dateRange"]["date_to"] = "2026-99-99T00:00:00Z"
    elif change == "extra_date_field":
        body["dateRange"]["other"] = "date"
    elif change == "numeric_date":
        body["dateRange"]["date_to"] = 1
    else:
        names = ("date_from",) if change == "wide_window" else ("date_from", "date_to")
        for name in names:
            value = datetime.fromisoformat(body["dateRange"][name]) - timedelta(days=1)
            body["dateRange"][name] = value.strftime("%Y-%m-%dT%H:%M:%SZ")
    adapter.session_factory = lambda: pytest.fail("Rejected query opened a network session")
    with pytest.raises(AlertError):
        await adapter._request(adapter.test_config, "POST", "error_tracking/query/issue_events/", payload=body)


async def test_two_queries_use_same_cutoff_even_when_clock_moves(adapter, monkeypatch):
    import extras.posthog.alerts as module

    now = module.time.time()
    clock = [now]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    issue = str(uuid.uuid4())
    calls = []

    async def request(config, method, path, **kwargs):
        calls.append((method, path, kwargs))
        clock[0] += 2
        if method == "GET":
            return {"id": issue, "name": "Error"}
        return response()

    adapter._request = request
    result = await adapter.issue({"config": adapter.test_config}, issue)
    first, second = (call[2]["payload"] for call in calls[1:])
    assert first["dateRange"] == second["dateRange"]
    assert result["sample"]["drill_status"] == "drill"
    assert "00000000-0000-4000-8000-000000000001" not in json.dumps(result)


@pytest.mark.parametrize(
    "sample_status,trigger_drill,prefix",
    [
        ("drill", False, "The sampled exception is a declared drill."),
        ("unmarked", False, "The sampled exception has no declared drill marker."),
        ("unknown", False, "Drill status of the sampled exception is unknown."),
        ("unknown", True, "Deliberate drill."),
    ],
)
async def test_worker_preserves_sample_scope_through_model_and_restart(
    tmp_path, monkeypatch, sample_status, trigger_drill, prefix
):
    from src.core import alert_diagnosis
    from src.core.base import LLMResponse
    from tests.test_alert_lifecycle import Harness
    from tests.test_alert_setup_ux import MemoryChannel

    h = Harness(tmp_path)
    room = MemoryChannel(401, guild=h.guild)
    h.bot.client.fetch_channel = AsyncMock(return_value=room)
    try:
        source = h.source()
        source = h.store.update(source["id"], config={**source["config"], "repo": str(tmp_path)})
        h.store.receive(
            source,
            event_id=str(uuid.uuid4()),
            issue_id=str(uuid.uuid4()),
            kind="$error_tracking_issue_created",
            message_id="600",
            drill=trigger_drill,
        )
        provider = SimpleNamespace(
            supports_tool_free=True, complete=AsyncMock(return_value=LLMResponse(content="Observed reporting path."))
        )
        manager = SimpleNamespace(
            agent_configs={"worker": {"llm": {"model": "fixture"}}},
            defaults={},
            storage=None,
            _apply_provider_override=lambda *args: None,
            _get_agent_llm=lambda *args: provider,
            _effective_model=lambda *args: "fixture",
            active_turns=0,
        )
        monkeypatch.setattr(alert_diagnosis, "source_evidence", lambda *args: {"snippets": []})
        adapter = SimpleNamespace(
            issue=AsyncMock(return_value={"name": "Error", "sample": {"drill_status": sample_status}})
        )
        worker = alert_diagnosis.AlertWorker(h.store, {"posthog": adapter}, manager, h.alerts.transport, str(tmp_path))
        await worker.once()
        issue = json.loads(provider.complete.call_args.args[0][1].content)["issue"]
        assert issue["sample"]["drill_status"] == sample_status
        assert ("Deliberate drill:" in issue["alert_context"]) == trigger_drill
        assert "test" not in issue and "setup_test" not in issue
        assert provider.complete.call_args.kwargs["tool_free"] is True
        h.store.close()
        h.store = AlertStore(tmp_path)
        h.alerts.transport.store = h.store
        receipt = h.store.ready()[0]
        assert receipt["sample_drill_status"] == sample_status
        assert receipt["result"].startswith(prefix)
        await h.alerts.transport.report(h.store.get(source["id"]), receipt)
        assert len(room.messages) == 1 and prefix in room.messages[0].content
    finally:
        h.store.close()
